"""Confluence-import API surface: sources (owner-private connections with
write-only credentials), runs (chunked crawl executions processed by the
`import_confluence` Celery task, enqueued here by name only -- this module
never imports the worker task module).

Registered in app/main.py under the same get_current_user + origin_guard
dependencies as the main job router. Artifact serving lives in
app/api/routes.py next to the other /jobs endpoints.
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.api.deps import _aware_utc, get_current_user
from app.api.routes import _JOB_BLOB_DEFER_OPTIONS, _owner_visible, _parse_tags, _sanitize_storage_path
from app.core.config import settings
from app.database.session import get_db
from app.models.models import (
    ImportAuthType,
    ImportRun,
    ImportRunStatus,
    ImportSource,
    Job,
    JobArtifact,
    User,
    UserRole,
)
from app.schemas.import_ import (
    ImportRunCancelResponse,
    ImportRunCreateRequest,
    ImportRunDeleteResponse,
    ImportRunDetailResponse,
    ImportRunError,
    ImportRunJobSummary,
    ImportRunListResponse,
    ImportRunResponse,
    ImportSourceCreateRequest,
    ImportSourceListResponse,
    ImportSourceResponse,
    ImportSourceTestResponse,
    ImportSourceUpdateRequest,
)
from app.services.confluence import ConfluenceError, detect_server_kind, extract_page_id
# Module-object access (security.encrypt_import_credential /
# security.decrypt_import_credential) rather than from-imports: the helpers
# live in the services slice of this feature and late binding keeps them
# monkeypatchable in tests.
from app.services import security
from app.services.security import enforce_rate_limit
from app.workers.celery_app import celery_app

# Exact Celery task name contract (see app/workers/import_tasks.py); enqueued
# via celery_app.send_task so the API process never imports the worker module.
IMPORT_TASK_NAME = 'import_confluence'


def _require_import_enabled() -> None:
    # Kill-switch: with IMPORT_ENABLED=false the whole /import surface 404s
    # as if the feature does not exist.
    if not settings.import_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')


router = APIRouter(prefix='/api/v1/import', dependencies=[Depends(_require_import_enabled)])

# Sync route handlers run in the threadpool, so a threading semaphore (not the
# asyncio one) is the per-process cap on concurrent outbound test probes. It
# deliberately does not read settings lazily: the cap is fixed at import time.
_probe_semaphore = threading.BoundedSemaphore(settings.import_probe_concurrency)

# Space-key extraction from pasted space URLs (/wiki/spaces/KEY/...); page-id
# extraction lives in app.services.confluence.extract_page_id.
_SPACE_KEY_URL_PATTERN = re.compile(r'/spaces/([^/?#]+)')


# --- Shared helpers -----------------------------------------------------------

def _normalize_base_url(raw: str) -> str:
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must use http or https')
    if not parts.hostname:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must include a host')
    if parts.username or parts.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not embed credentials'
        )
    if parts.query or parts.fragment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not contain a query or fragment'
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _get_owned_source(db: Session, source_id: str, user: User) -> ImportSource:
    """Sources are strictly owner-private (a credential is a personal
    Confluence identity): any non-owner -- including admins -- gets a 404,
    never a 403, so cross-user source ids are unprobeable."""
    source = db.get(ImportSource, source_id)
    if source is None or source.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Import source not found')
    return source


def _visible_run_filter(user: User):
    # Mirrors routes._visible_job_filter: own + current-teammates + admin-all;
    # legacy NULL-owner runs stay admin-only.
    if user.role == UserRole.ADMIN:
        return None
    conditions = [ImportRun.owner_id == user.id]
    if user.team_id is not None:
        teammate_ids = select(User.id).where(User.team_id == user.team_id)
        conditions.append(ImportRun.owner_id.in_(teammate_ids))
    return or_(*conditions)


def _get_visible_run(db: Session, run_id: str, user: User) -> ImportRun:
    run = db.get(ImportRun, run_id)
    if run is None or not _owner_visible(db, run.owner_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Import run not found')
    return run


def _require_run_control(run: ImportRun, user: User) -> None:
    # Teammates may read a run but not control it (read != control).
    if user.role != UserRole.ADMIN and run.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Only the run owner or an admin can do this')


def _is_stale_active(run: ImportRun, now: datetime) -> bool:
    """A healthy chunk loop touches updated_at at least once per page, so a
    'running' run whose heartbeat is older than IMPORT_STALE_RUN_SECONDS has
    lost its worker. A 'pending' run that old never had its creation message
    processed (send_task failed / message lost -- updated_at is the creation
    time until the first chunk claim) and is equally dead; without this it
    would count against the active-run cap forever."""
    if run.status not in (ImportRunStatus.PENDING, ImportRunStatus.RUNNING):
        return False
    return _aware_utc(run.updated_at) < now - timedelta(seconds=settings.import_stale_run_seconds)


def _extract_space_key(value: str) -> str | None:
    candidate = value.strip()
    if '://' in candidate:
        match = _SPACE_KEY_URL_PATTERN.search(candidate)
        if match is None:
            return None
        candidate = match.group(1)
    if not candidate or len(candidate) > 255 or '/' in candidate or any(ch.isspace() for ch in candidate):
        return None
    return candidate


def _source_to_response(source: ImportSource) -> ImportSourceResponse:
    return ImportSourceResponse.model_validate(source)


# --- Sources ------------------------------------------------------------------

@router.post('/sources', response_model=ImportSourceResponse, status_code=status.HTTP_201_CREATED)
def create_import_source(
    request: Request,
    payload: ImportSourceCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportSourceResponse:
    enforce_rate_limit(request)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name is required')
    auth_username = payload.auth_username.strip()
    if payload.auth_type == ImportAuthType.CLOUD_BASIC and not auth_username:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='auth_username (Atlassian account email) is required for cloud_basic',
        )
    if payload.auth_type == ImportAuthType.PAT_BEARER:
        auth_username = ''

    source = ImportSource(
        owner_id=user.id,
        name=name,
        base_url=_normalize_base_url(payload.base_url),
        auth_type=payload.auth_type,
        auth_username=auth_username,
        # Write-only from here on: encrypted at rest, never logged, never in
        # any response schema.
        credential_encrypted=security.encrypt_import_credential(payload.credential),
    )
    db.add(source)
    db.commit()
    return _source_to_response(source)


@router.get('/sources', response_model=ImportSourceListResponse)
def list_import_sources(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ImportSourceListResponse:
    sources = db.scalars(
        select(ImportSource).where(ImportSource.owner_id == user.id).order_by(ImportSource.created_at.desc())
    ).all()
    return ImportSourceListResponse(items=[_source_to_response(source) for source in sources])


@router.patch('/sources/{source_id}', response_model=ImportSourceResponse)
def update_import_source(
    source_id: str,
    payload: ImportSourceUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportSourceResponse:
    enforce_rate_limit(request)
    source = _get_owned_source(db, source_id, user)

    connection_changed = False
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name cannot be empty')
        source.name = name
    if payload.base_url is not None:
        source.base_url = _normalize_base_url(payload.base_url)
        connection_changed = True
    if payload.auth_type is not None and payload.auth_type != source.auth_type:
        source.auth_type = payload.auth_type
        connection_changed = True
    if payload.auth_username is not None:
        source.auth_username = payload.auth_username.strip()
    # Write-only update contract: omitted or empty credential keeps the
    # stored one.
    if payload.credential:
        source.credential_encrypted = security.encrypt_import_credential(payload.credential)
        connection_changed = True

    if source.auth_type == ImportAuthType.CLOUD_BASIC and not source.auth_username:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='auth_username (Atlassian account email) is required for cloud_basic',
        )
    if source.auth_type == ImportAuthType.PAT_BEARER:
        source.auth_username = ''

    if connection_changed:
        # A changed URL/auth invalidates the previous detection result until
        # the next successful /test.
        source.server_kind = ''
        source.api_base_path = ''
        source.last_validated_at = None

    db.commit()
    return _source_to_response(source)


@router.delete('/sources/{source_id}')
def delete_import_source(
    source_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    enforce_rate_limit(request)
    source = _get_owned_source(db, source_id, user)
    # Runs keep their history: the ORM relationship nulls import_runs.source_id
    # on flush (sqlite never enables PRAGMA foreign_keys, so the DB-level
    # SET NULL cannot be relied on).
    db.delete(source)
    db.commit()
    return {'status': 'deleted'}


@router.post('/sources/{source_id}/test', response_model=ImportSourceTestResponse)
def test_import_source(
    source_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportSourceTestResponse:
    enforce_rate_limit(request)
    source = _get_owned_source(db, source_id, user)

    now = datetime.now(timezone.utc)
    # DB-backed cooldown: anchored on last_test_at (set by EVERY attempt), so
    # the outbound-probe floor holds even when the Redis rate limiter above
    # fails open during an outage. Claimed with a single guarded UPDATE (not
    # read-check-then-write) so N parallel requests can never all pass the
    # check before any of them commits -- exactly one claims the slot.
    cooldown_cutoff = now - timedelta(seconds=settings.import_test_cooldown_seconds)
    claimed = db.execute(
        update(ImportSource)
        .where(ImportSource.id == source.id)
        .where(or_(ImportSource.last_test_at.is_(None), ImportSource.last_test_at < cooldown_cutoff))
        .values(last_test_at=now),
        # The ORM 'evaluate' synchronizer cannot compare the naive datetime
        # on the loaded `source` against the aware cutoff (sqlite); the row
        # is re-read after commit anyway.
        execution_options={'synchronize_session': False},
    )
    db.commit()
    if not claimed.rowcount:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Connection was tested too recently; wait a few seconds and try again',
        )

    try:
        credential = security.decrypt_import_credential(source.credential_encrypted)
    except ValueError:
        return ImportSourceTestResponse(
            ok=False,
            detail='Stored credential can no longer be decrypted (SECRET_KEY changed?); the credential must be re-entered',
            server_kind=None,
        )

    if not _probe_semaphore.acquire(timeout=float(settings.import_fetch_timeout_seconds)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many concurrent connection tests; try again shortly',
        )
    try:
        server_kind, api_base_path = detect_server_kind(
            source.base_url,
            auth_type=source.auth_type,
            auth_username=source.auth_username,
            credential=credential,
            allowed_private_hosts=frozenset(settings.import_private_host_allowlist),
            timeout=float(settings.import_fetch_timeout_seconds),
            max_bytes=settings.import_fetch_max_bytes,
        )
    except ConfluenceError as exc:
        # ConfluenceError messages carry URLs/statuses, never headers or the
        # credential.
        return ImportSourceTestResponse(ok=False, detail=str(exc), server_kind=None)
    finally:
        _probe_semaphore.release()

    source.server_kind = server_kind
    source.api_base_path = api_base_path
    source.last_validated_at = datetime.now(timezone.utc)
    db.commit()
    label = 'Confluence Cloud' if server_kind == 'cloud' else 'Confluence Server/Data Center'
    return ImportSourceTestResponse(ok=True, detail=f'Connected ({label})', server_kind=server_kind)


# --- Runs ---------------------------------------------------------------------

@router.post('/runs', response_model=ImportRunResponse, status_code=status.HTTP_201_CREATED)
def create_import_run(
    request: Request,
    payload: ImportRunCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportRunResponse:
    enforce_rate_limit(request)
    # Owner check via the source helper: importing with a teammate's stored
    # credential is forbidden, and non-owned ids 404.
    source = _get_owned_source(db, payload.source_id, user)
    if not source.server_kind:
        # The worker selects the v1/v2 client by the persisted server_kind, so
        # a run can only start against a source that passed /test.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Source connection has not been tested successfully yet; test the connection first',
        )

    if payload.scope.type == 'page':
        scope_value = extract_page_id(payload.scope.value)
        if scope_value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='scope.value must be a numeric page id or a Confluence page URL containing one',
            )
    else:
        scope_value = _extract_space_key(payload.scope.value)
        if scope_value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='scope.value must be a space key or a Confluence space URL containing one',
            )

    # Server-side clamps: client values can lower the caps, never raise them.
    max_pages = min(payload.options.max_pages or settings.import_max_pages, settings.import_max_pages)
    if payload.options.max_depth is None:
        max_depth = settings.import_max_depth
    else:
        max_depth = min(payload.options.max_depth, settings.import_max_depth)

    now = datetime.now(timezone.utc)
    # Active-run cap with stale-run reaping: a wedged (worker-lost) 'running'
    # run is flipped to failed here rather than counted, so it can never lock
    # its owner out of importing.
    candidate_runs = db.scalars(
        select(ImportRun).where(
            ImportRun.owner_id == user.id,
            ImportRun.status.in_([ImportRunStatus.PENDING, ImportRunStatus.RUNNING]),
        )
    ).all()
    active_count = 0
    for candidate in candidate_runs:
        if _is_stale_active(candidate, now):
            candidate.status = ImportRunStatus.FAILED
            candidate.error_message = 'worker lost; run stalled'
            candidate.finished_at = now
            continue
        active_count += 1
    if active_count >= settings.import_max_active_runs_per_user:
        db.commit()  # persist any stale-run reaping even when refusing
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='An import run is already active; wait for it to finish or cancel it first',
        )

    options = {
        'max_pages': max_pages,
        'max_depth': max_depth,
        'include_attachments': payload.options.include_attachments,
        'ocr_attachments': payload.options.ocr_attachments,
        'ocr_profile_id': payload.options.ocr_profile_id,
        'folder': _sanitize_storage_path(payload.options.folder),
        'subfolder': _sanitize_storage_path(payload.options.subfolder),
        'tags': _parse_tags(','.join(payload.options.tags)),
        'email': payload.options.email.strip(),
    }
    # Page scope seeds the frontier directly; space scope leaves it empty --
    # run creation makes no outbound requests, so the worker resolves the
    # space homepage (resolve_space_root) on its first chunk when the
    # frontier is empty and nothing has been discovered yet.
    frontier = [[scope_value, 0]] if payload.scope.type == 'page' else []
    run = ImportRun(
        source_id=source.id,
        owner_id=user.id,
        kind='confluence',
        scope_type=payload.scope.type,
        scope_value=scope_value,
        options=options,
        state={'frontier': frontier, 'visited': {}, 'errors': []},
    )
    db.add(run)
    db.commit()

    # TOCTOU recheck: two parallel creates can each count zero active runs
    # above and both insert. After committing, only the first `cap` active
    # runs in (created_at, id) order survive; a run outside that window is
    # removed again. Because each racer's recheck runs after its own commit,
    # at least one of any two racers sees both rows here -- the cap holds
    # without DB-level locking, and the task is only enqueued for survivors.
    surviving_ids = db.scalars(
        select(ImportRun.id)
        .where(
            ImportRun.owner_id == user.id,
            ImportRun.status.in_([ImportRunStatus.PENDING, ImportRunStatus.RUNNING]),
        )
        .order_by(ImportRun.created_at.asc(), ImportRun.id.asc())
        .limit(settings.import_max_active_runs_per_user)
    ).all()
    if run.id not in surviving_ids:
        db.delete(run)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='An import run is already active; wait for it to finish or cancel it first',
        )

    celery_app.send_task(IMPORT_TASK_NAME, args=[run.id, 0])
    return ImportRunResponse.model_validate(run)


@router.get('/runs', response_model=ImportRunListResponse)
def list_import_runs(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ImportRunListResponse:
    query = select(ImportRun).order_by(ImportRun.created_at.desc())
    visible_filter = _visible_run_filter(user)
    if visible_filter is not None:
        query = query.where(visible_filter)
    runs = db.scalars(query).all()
    return ImportRunListResponse(items=[ImportRunResponse.model_validate(run) for run in runs])


@router.get('/runs/{run_id}', response_model=ImportRunDetailResponse)
def get_import_run(
    run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ImportRunDetailResponse:
    run = _get_visible_run(db, run_id, user)

    jobs = db.scalars(
        select(Job)
        .where(Job.import_run_id == run.id)
        .order_by(Job.created_at.asc())
        .options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()

    state = run.state if isinstance(run.state, dict) else {}
    raw_errors = state.get('errors') if isinstance(state.get('errors'), list) else []
    errors = [
        ImportRunError(
            page_id=str(entry.get('page_id', '')),
            title=str(entry.get('title') or ''),
            error=str(entry.get('error', '')),
        )
        for entry in raw_errors
        if isinstance(entry, dict)
    ]

    base = ImportRunResponse.model_validate(run)
    return ImportRunDetailResponse(
        **base.model_dump(),
        current_page_title=run.current_page_title,
        error_message=run.error_message,
        cancel_requested=run.cancel_requested,
        errors=errors,
        jobs=[ImportRunJobSummary(id=job.id, title=job.original_filename, status=job.status) for job in jobs],
    )


@router.post('/runs/{run_id}/cancel', response_model=ImportRunCancelResponse)
def cancel_import_run(
    run_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportRunCancelResponse:
    enforce_rate_limit(request)
    run = _get_visible_run(db, run_id, user)
    _require_run_control(run, user)

    now = datetime.now(timezone.utc)
    if run.status == ImportRunStatus.PENDING:
        # No chunk has claimed the run yet; the queued task's lease claim
        # requires status pending/running, so flipping here also neutralizes
        # the already-enqueued message.
        run.status = ImportRunStatus.CANCELLED
        run.cancel_requested = True
        run.finished_at = now
    elif run.status == ImportRunStatus.RUNNING:
        if _is_stale_active(run, now):
            # Worker lost: no chunk will ever observe cancel_requested, so
            # force-terminate directly -- users can always unstick their run.
            run.status = ImportRunStatus.CANCELLED
            run.cancel_requested = True
            run.finished_at = now
        else:
            run.cancel_requested = True  # worker flips status between pages
    elif run.status == ImportRunStatus.CANCELLED:
        pass  # idempotent
    else:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Run already finished')
    db.commit()

    return ImportRunCancelResponse(id=run.id, status=run.status, cancel_requested=run.cancel_requested)


@router.delete('/runs/{run_id}', response_model=ImportRunDeleteResponse)
def delete_import_run(
    run_id: str,
    request: Request,
    delete_jobs: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportRunDeleteResponse:
    enforce_rate_limit(request)
    run = _get_visible_run(db, run_id, user)
    _require_run_control(run, user)
    if run.status in (ImportRunStatus.PENDING, ImportRunStatus.RUNNING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Cancel the run before deleting it'
        )

    deleted_jobs = 0
    if delete_jobs:
        # Bulk-delete the artifact rows first so the ORM's delete-orphan
        # cascade never has to load their BYTEA payloads into memory; the
        # per-job ORM delete then handles markdown versions and tag links.
        job_ids = db.scalars(select(Job.id).where(Job.import_run_id == run.id)).all()
        if job_ids:
            db.execute(delete(JobArtifact).where(JobArtifact.job_id.in_(job_ids)))
        jobs = db.scalars(
            select(Job).where(Job.import_run_id == run.id).options(*_JOB_BLOB_DEFER_OPTIONS)
        ).all()
        for job in jobs:
            db.delete(job)
            deleted_jobs += 1
    else:
        # Explicit SQL instead of the DB-level SET NULL cascade: sqlite never
        # enables PRAGMA foreign_keys, so this is the one behavior identical
        # on both dialects.
        db.execute(update(Job).where(Job.import_run_id == run.id).values(import_run_id=None))

    db.delete(run)
    db.commit()
    return ImportRunDeleteResponse(id=run_id, deleted_jobs=deleted_jobs)
