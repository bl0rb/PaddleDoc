"""Mail ingestion API surface (docs/integrations/mail-ingestion.md):

- `POST /api/v1/mail/messages` -- raw RFC-822 ingest. New territory for this
  codebase: reads `request.stream()` chunk-wise with an incremental sha256
  and a 413 abort at `settings.max_mail_message_bytes` (there is no
  body-size middleware anywhere in the stack, and `await request.body()`
  would buffer unboundedly -- see the design doc's step 1). The
  multipart/form-data convenience branch cannot get a mid-stream abort
  (Starlette fully spools the body before the handler runs), so it rejects
  on the declared Content-Length up front and re-checks the actual spooled
  size afterwards -- the same weaker, post-hoc cap uploads already have.
- Retrieval: list / detail / body / raw / part-content / export.json /
  delete, all visibility-scoped via `_visible_mail_filter` (own + team +
  admin, mirroring `routes._visible_job_filter`), 404 never 403 for
  anything invisible.

Registered in app/main.py under the same get_current_user + origin_guard
dependencies as the main job router (see app/api/import_routes.py /
app/api/benchmarks.py for the identical registration pattern). No separate
kill-switch (unlike /import's IMPORT_ENABLED) -- mail ingestion makes no
outbound egress of its own to gate.
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from app.api.deps import _aware_utc, get_current_user
from app.api.routes import (
    _JOB_BLOB_DEFER_OPTIONS,
    _JOB_DEFER_UPLOAD_CONTENT_ONLY,
    _attach_tags,
    _content_disposition,
    _owner_visible,
    _parse_tags,
    _storage_folder,
)
from app.core.config import settings
from app.database.session import get_db
from app.models.models import Job, JobArtifact, JobStatus, MailMessage, User, UserRole
from app.schemas.mail import (
    MailIngestResponse,
    MailMessageDeleteResponse,
    MailMessageDetailResponse,
    MailMessageListItem,
    MailMessageListResponse,
    MailPartDetail,
    MailPartResponse,
)
from app.services.mail_ingest import MailParseError, compute_content_sha256, extract_mail_part, parse_mail_message
from app.services.paddle_service import get_paddle_settings
from app.services.security import enforce_rate_limit
from app.workers.tasks import process_job

router = APIRouter(prefix='/api/v1/mail')

_MAIL_LIST_PAGE_LIMIT_MAX = 500
# Blob discipline: MailMessage.raw_content is BYTEA-sized, same rule as
# _JOB_BLOB_DEFER_OPTIONS / _ARTIFACT_BLOB_DEFER_OPTIONS. Applied to every
# list/lookup query except /raw and /parts/{index}/content, which need the
# real bytes to serve or re-walk.
_MAIL_BLOB_DEFER_OPTIONS = (defer(MailMessage.raw_content),)
# "a few minutes" (design doc step 6) -- how stale a PENDING mail-attachment
# job must be before the detail/export poll-path backstop re-dispatches it.
# Not a settings field: this is a fixed, non-tunable safety margin against
# the ordinary commit-then-dispatch window, same spirit as tasks.py's
# hardcoded _STALE_RUNNING_RETRY_AFTER.
_STRANDED_MAIL_JOB_AFTER = timedelta(minutes=3)


# --- Visibility -----------------------------------------------------------------

def _visible_mail_filter(user: User):
    """Mirrors routes._visible_job_filter / import_routes._visible_run_filter:
    own + current-teammates + admin-all. Legacy NULL-owner rows (should not
    occur for this table) stay admin-only."""
    if user.role == UserRole.ADMIN:
        return None
    conditions = [MailMessage.owner_id == user.id]
    if user.team_id is not None:
        teammate_ids = select(User.id).where(User.team_id == user.team_id)
        conditions.append(MailMessage.owner_id.in_(teammate_ids))
    return or_(*conditions)


def _get_visible_mail_message(db: Session, message_id: str, user: User, *, defer_raw: bool = True) -> MailMessage:
    options = list(_MAIL_BLOB_DEFER_OPTIONS) if defer_raw else []
    message = db.get(MailMessage, message_id, options=options)
    if message is None or not _owner_visible(db, message.owner_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Mail message not found')
    return message


def _is_mail_dedup_race(exc: IntegrityError) -> bool:
    """True only for the UniqueConstraint(owner_id, content_sha256) backstop
    actually firing (a genuine concurrent duplicate ingest of the same
    message racing this request to commit -- design doc step 2). Any other
    IntegrityError must propagate as a real error instead of being silently
    swallowed into a misleading 409 'Duplicate message' by the caller's
    except-IntegrityError branch -- e.g. a bug elsewhere in the same
    transaction (tag creation, a future column addition) would otherwise
    masquerade as 'this message was already ingested', which it wasn't.
    SQLite's error text never names the constraint (just the column list,
    `mail_messages.owner_id, mail_messages.content_sha256`); Postgres's
    does (`uq_mail_messages_owner_id_content_sha256`) -- this checks for
    either, engine-agnostically."""
    text = str(getattr(exc, 'orig', exc)).lower()
    return 'uq_mail_messages_owner_id_content_sha256' in text or (
        'mail_messages.owner_id' in text and 'mail_messages.content_sha256' in text
    )


def _find_visible_mail_by_hash(db: Session, user: User, content_sha256: str) -> MailMessage | None:
    """Dedup lookup (design doc step 2), scoped to the caller's full
    visibility (own + team + admin) -- broader than the
    UniqueConstraint(owner_id, content_sha256), which is only the race
    backstop within one owner (see the IntegrityError handling below)."""
    query = (
        select(MailMessage)
        .where(MailMessage.content_sha256 == content_sha256)
        .order_by(MailMessage.created_at.desc())
        .options(*_MAIL_BLOB_DEFER_OPTIONS)
    )
    visible_filter = _visible_mail_filter(user)
    if visible_filter is not None:
        query = query.where(visible_filter)
    return db.scalars(query).first()


# --- Dispatch helpers -------------------------------------------------------------

def _job_dispatch_args(job: Job) -> str | None:
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    return settings_info.get('profile_id') if isinstance(settings_info.get('profile_id'), str) else None


def _redispatch_pending_mail_jobs(db: Session, mail_message_id: str) -> int:
    """Crash-window recovery on replay (design doc step 6): a duplicate POST
    means the sender never got a response, which means the API pod may have
    died between commit and the original dispatch loop, stranding PENDING
    children. Unconditional -- unlike the poll-path backstop below, replay
    is itself the recovery signal, so there is no age gate."""
    pending = db.scalars(
        select(Job)
        .where(Job.mail_message_id == mail_message_id, Job.status == JobStatus.PENDING)
        .options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()
    for job in pending:
        process_job.delay(job.id, _job_dispatch_args(job), 'mail_attachment', '', None)
    return len(pending)


def _redispatch_stranded_mail_jobs(jobs: list[Job]) -> None:
    """Belt-and-braces poll-path backstop (design doc step 6): the
    detail/export endpoints re-dispatch PENDING mail-attachment jobs older
    than a few minutes, covering the same crash window as the replay path
    for a message that never gets re-POSTed. Takes already-loaded Job rows
    (the caller needs them for the response anyway) rather than issuing a
    second query."""
    cutoff = datetime.now(timezone.utc) - _STRANDED_MAIL_JOB_AFTER
    for job in jobs:
        if job.status != JobStatus.PENDING or _aware_utc(job.updated_at) >= cutoff:
            continue
        process_job.delay(job.id, _job_dispatch_args(job), 'mail_attachment', '', None)


# --- Raw-body reading (ingest only) ------------------------------------------------

async def _read_streaming_message(request: Request) -> tuple[bytes, str]:
    """`message/rfc822` (and any other non-multipart) branch: stream the
    body chunk-wise, hashing incrementally, aborting with 413 as soon as the
    running total exceeds the cap -- see design doc step 1. There is no
    in-repo precedent for reading `request.stream()`; `save_upload` only
    chunk-reads an `UploadFile` Starlette has already fully spooled."""
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > settings.max_mail_message_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Message too large')
        hasher.update(chunk)
        chunks.append(chunk)
    return b''.join(chunks), hasher.hexdigest()


async def _read_multipart_message(request: Request) -> tuple[bytes, dict[str, str]]:
    """multipart/form-data convenience branch (curl -F, n8n form mode).
    FastAPI/Starlette fully parses and spools the body before the handler
    runs, so there is no mid-stream abort available here -- reject on the
    declared Content-Length up front (a valid upper bound: it covers the
    whole multipart body, which is always >= the embedded file's size), then
    re-check the actual spooled `file` part size afterwards. Same weaker,
    post-hoc cap `save_upload` already accepts for ordinary uploads."""
    declared_length = request.headers.get('content-length')
    if declared_length is not None:
        try:
            declared = int(declared_length)
        except ValueError:
            declared = None
        if declared is not None and declared > settings.max_mail_message_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Message too large')

    form = await request.form()
    upload = form.get('file')
    if upload is None or not hasattr(upload, 'read'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='file field is required')
    raw = await upload.read()
    if len(raw) > settings.max_mail_message_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail='Message too large')

    fields = {key: value for key, value in form.multi_items() if isinstance(value, str)}
    return raw, fields


# --- Response assembly ------------------------------------------------------------

def _mail_ingest_response(message: MailMessage, *, replayed: bool) -> MailIngestResponse:
    parts_raw = message.parts if isinstance(message.parts, list) else []
    return MailIngestResponse(
        id=message.id,
        replayed=replayed,
        content_sha256=message.content_sha256,
        rfc_message_id=message.rfc_message_id,
        subject=message.subject,
        from_address=message.from_address,
        recipients=message.recipients if isinstance(message.recipients, dict) else {},
        sent_at=message.sent_at,
        source=message.source,
        raw_size_bytes=message.raw_size_bytes,
        body_format=message.body_format,
        has_body=message.body_markdown is not None,
        parts=[MailPartResponse(**entry) for entry in parts_raw if isinstance(entry, dict)],
        created_at=message.created_at,
    )


def _mail_message_list_item(message: MailMessage) -> MailMessageListItem:
    parts_raw = message.parts if isinstance(message.parts, list) else []
    return MailMessageListItem(
        id=message.id,
        content_sha256=message.content_sha256,
        rfc_message_id=message.rfc_message_id,
        subject=message.subject,
        from_address=message.from_address,
        recipients=message.recipients if isinstance(message.recipients, dict) else {},
        sent_at=message.sent_at,
        source=message.source,
        raw_size_bytes=message.raw_size_bytes,
        body_format=message.body_format,
        has_body=message.body_markdown is not None,
        parts=[MailPartResponse(**entry) for entry in parts_raw if isinstance(entry, dict)],
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _part_detail(entry: dict, jobs_by_id: dict[str, Job]) -> MailPartDetail:
    job_id = entry.get('job_id') if isinstance(entry.get('job_id'), str) else None
    job = jobs_by_id.get(job_id) if job_id else None
    return MailPartDetail(
        index=entry.get('index', 0),
        filename=entry.get('filename', ''),
        content_type=entry.get('content_type', ''),
        size_bytes=entry.get('size_bytes', 0),
        outcome=entry.get('outcome', 'skipped'),
        job_id=job_id,
        skip_reason=entry.get('skip_reason'),
        job_status=job.status if job is not None else None,
        job_error_message=job.error_message if job is not None else None,
    )


# --- List / filter helpers ---------------------------------------------------------

def _apply_mail_filters(
    query,
    *,
    q: str | None,
    message_id: str | None,
    sha256: str | None,
    source: str | None,
    from_date: date | None,
    to_date: date | None,
    visible_filter,
):
    if q:
        pattern = f'%{q.strip().lower()}%'
        query = query.where(
            or_(func.lower(MailMessage.subject).like(pattern), func.lower(MailMessage.from_address).like(pattern))
        )
    if message_id:
        query = query.where(MailMessage.rfc_message_id == message_id)
    if sha256:
        query = query.where(MailMessage.content_sha256 == sha256)
    if source:
        query = query.where(MailMessage.source == source)
    if from_date:
        query = query.where(MailMessage.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
    if to_date:
        query = query.where(MailMessage.created_at <= datetime.combine(to_date, time.max, tzinfo=timezone.utc))
    if visible_filter is not None:
        query = query.where(visible_filter)
    return query


# --- Endpoints ------------------------------------------------------------------

@router.post('/messages', response_model=MailIngestResponse)
async def ingest_mail_message(
    request: Request,
    profile_id: str | None = Query(default=None),
    folder: str = Query(default='mail'),
    subfolder: str = Query(default=''),
    tags: str = Query(default=''),
    source: str = Query(default='api'),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Raw RFC-822 bytes (`Content-Type: message/rfc822`, the primary
    contract) or `multipart/form-data` with a single `file` part (curl -F /
    n8n form-mode convenience) -- same query parameters, accepted as form
    fields instead in the latter case. See docs/integrations/mail-ingestion.md.

    Query/form parameters:
    - profile_id: OCR profile (default: ppocrv6_tiny). Applied to all
      attachment jobs.
    - folder, subfolder: Storage path (default: 'mail').
    - tags: Comma-separated tags to apply to attachment jobs.
    - source: Origin label; 'api' for API ingest, 'upload' for UI .eml files
      (default: 'api').

    Rate limiting is intentionally skipped here, following the
    collection-bulk-upload precedent (see
    `upload_document_to_collection` in app/api/routes.py) -- a gateway
    flushing its outbox from one IP would otherwise burn the shared
    per-IP bucket. Auth + the size cap + hash dedup are the guards instead.
    """
    media_type = (request.headers.get('content-type') or '').split(';', 1)[0].strip().lower()

    if media_type == 'multipart/form-data':
        raw, form_fields = await _read_multipart_message(request)
        profile_id = form_fields.get('profile_id') or profile_id
        folder = form_fields.get('folder') or folder
        subfolder = form_fields.get('subfolder') or subfolder
        tags = form_fields.get('tags') or tags
        source = form_fields.get('source') or source
        content_sha256 = compute_content_sha256(raw)
    else:
        raw, content_sha256 = await _read_streaming_message(request)

    # Step 2 (design doc): dedup within the caller's visibility scope,
    # BEFORE parsing -- a replay never re-parses or re-stores anything.
    existing = _find_visible_mail_by_hash(db, user, content_sha256)
    if existing is not None:
        _redispatch_pending_mail_jobs(db, existing.id)
        db.commit()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_mail_ingest_response(existing, replayed=True).model_dump(mode='json'),
        )

    try:
        parsed = parse_mail_message(
            raw, content_sha256=content_sha256, ingested_by=source.strip(), ingested_at=datetime.now(timezone.utc)
        )
    except MailParseError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Unable to parse message')

    folder_clean = folder.strip() or 'mail'
    subfolder_clean = subfolder.strip()
    tag_list = _parse_tags(tags)
    effective_profile_id = (profile_id or '').strip() or str(get_paddle_settings()['default_profile'])

    message_id = str(uuid.uuid4())
    message = MailMessage(
        id=message_id,
        owner_id=user.id,
        content_sha256=content_sha256,
        rfc_message_id=parsed.envelope.rfc_message_id,
        subject=parsed.envelope.subject,
        from_address=parsed.envelope.from_address,
        recipients={'to': parsed.envelope.to, 'cc': parsed.envelope.cc},
        sent_at=parsed.envelope.sent_at,
        source=source.strip(),
        raw_content=raw,
        raw_size_bytes=len(raw),
        body_format=parsed.body.format,
        body_markdown=parsed.body.markdown,
        parts=[],
    )
    db.add(message)

    # Each supported attachment becomes an ordinary Job, following the
    # Confluence attachment-child pattern (app/workers/import_tasks.py's
    # _process_page_attachments): version-chain bypass (document_version=1,
    # previous_job_id=None, no _find_predecessor_job lookup -- message-hash
    # dedup above already blocks true duplicates), storage_folder is
    # load-bearing (the worker's _resolve_upload_path/_resolve_result_path
    # read exactly that key). One transaction with the mail_messages row.
    manifest: list[dict] = []
    created_children: list[Job] = []
    for part in parsed.parts:
        entry = part.to_dict()
        if part.outcome == 'job':
            extracted = extract_mail_part(raw, part.index)
            if extracted is None:  # pragma: no cover - defensive: same deterministic walk that produced `part`
                entry['outcome'] = 'skipped'
                entry['skip_reason'] = 'unsupported_type'
            else:
                job_id = str(uuid.uuid4())
                storage_folder = _storage_folder(job_id, folder_clean, subfolder_clean)
                suffix = Path(part.filename).suffix or mimetypes.guess_extension(part.content_type) or '.bin'
                child = Job(
                    id=job_id,
                    original_filename=part.filename,
                    # Synthetic relative path: _resolve_upload_path only
                    # needs the suffix -- it rehydrates upload_content from
                    # the DB into storage_folder itself on first touch.
                    upload_path=f'{storage_folder}/{job_id}{suffix}',
                    upload_content=extracted.content,
                    upload_mime_type=part.content_type,
                    upload_size_bytes=part.size_bytes,
                    status=JobStatus.PENDING,
                    owner_id=user.id,
                    mail_message_id=message_id,
                    content_sha256=hashlib.sha256(extracted.content).hexdigest(),
                    document_version=1,
                    previous_job_id=None,
                    processing_info={
                        'settings': {
                            'mode': 'mail_attachment',
                            'email': '',
                            'department': None,
                            'profile_id': effective_profile_id,
                            'collection_id': None,
                            'folder': folder_clean,
                            'subfolder': subfolder_clean,
                            'storage_folder': storage_folder,
                            'mail': {
                                'mail_message_id': message_id,
                                'part_index': part.index,
                                'rfc_message_id': parsed.envelope.rfc_message_id,
                            },
                        },
                    },
                )
                db.add(child)
                _attach_tags(db, child, tag_list)
                entry['job_id'] = job_id
                created_children.append(child)
        manifest.append(entry)

    message.parts = manifest

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_mail_dedup_race(exc):
            # Not the dedup backstop -- some other constraint failed (a real
            # bug). Let it propagate as a real error instead of masquerading
            # as "this message was already ingested", which it wasn't.
            raise
        # A concurrent duplicate raced this request to commit first (design
        # doc step 2): UniqueConstraint(owner_id, content_sha256) fired.
        # Roll back everything from this attempt -- message + every child
        # job, one transaction -- and fall back to the same replay path as
        # an ordinary dedup hit.
        existing = _find_visible_mail_by_hash(db, user, content_sha256)
        if existing is None:  # pragma: no cover - defensive only
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Duplicate message')
        _redispatch_pending_mail_jobs(db, existing.id)
        db.commit()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_mail_ingest_response(existing, replayed=True).model_dump(mode='json'),
        )

    # Step 6: commit before dispatch -- a crash here only strands PENDING
    # jobs, never half-ingests a message and then rejects the retry. The
    # replay path above and the detail/export poll-path backstop
    # (_redispatch_stranded_mail_jobs) both recover from exactly this window.
    for child in created_children:
        process_job.delay(child.id, _job_dispatch_args(child), 'mail_attachment', '', None)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_mail_ingest_response(message, replayed=False).model_dump(mode='json'),
    )


@router.get('/messages', response_model=MailMessageListResponse)
def list_mail_messages(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = None,
    message_id: str | None = None,
    sha256: str | None = None,
    source: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int | None = Query(default=None, ge=0, le=_MAIL_LIST_PAGE_LIMIT_MAX),
    offset: int | None = Query(default=None, ge=0),
) -> MailMessageListResponse:
    enforce_rate_limit(request)

    filter_kwargs = dict(q=q, message_id=message_id, sha256=sha256, source=source, from_date=from_date, to_date=to_date)
    visible_filter = _visible_mail_filter(user)

    query = _apply_mail_filters(
        select(MailMessage).order_by(MailMessage.created_at.desc()).options(*_MAIL_BLOB_DEFER_OPTIONS),
        visible_filter=visible_filter,
        **filter_kwargs,
    )
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    messages = db.scalars(query).all()

    total = db.scalar(_apply_mail_filters(select(func.count(MailMessage.id)), visible_filter=visible_filter, **filter_kwargs)) or 0

    return MailMessageListResponse(items=[_mail_message_list_item(message) for message in messages], total=total)


@router.get('/messages/{message_id}', response_model=MailMessageDetailResponse)
def get_mail_message(
    message_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MailMessageDetailResponse:
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user)

    manifest = message.parts if isinstance(message.parts, list) else []
    job_ids = [entry.get('job_id') for entry in manifest if isinstance(entry, dict) and isinstance(entry.get('job_id'), str)]
    jobs_by_id: dict[str, Job] = {}
    if job_ids:
        jobs = db.scalars(select(Job).where(Job.id.in_(job_ids)).options(*_JOB_BLOB_DEFER_OPTIONS)).all()
        jobs_by_id = {job.id: job for job in jobs}
        # Belt-and-braces: recover a mail-attachment job stranded by an
        # API-pod crash between commit and dispatch (design doc step 6).
        _redispatch_stranded_mail_jobs(list(jobs_by_id.values()))

    return MailMessageDetailResponse(
        id=message.id,
        content_sha256=message.content_sha256,
        rfc_message_id=message.rfc_message_id,
        subject=message.subject,
        from_address=message.from_address,
        recipients=message.recipients if isinstance(message.recipients, dict) else {},
        sent_at=message.sent_at,
        source=message.source,
        raw_size_bytes=message.raw_size_bytes,
        body_format=message.body_format,
        has_body=message.body_markdown is not None,
        parts=[_part_detail(entry, jobs_by_id) for entry in manifest if isinstance(entry, dict)],
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


@router.get('/messages/{message_id}/body')
def get_mail_message_body(
    message_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> PlainTextResponse:
    """Mirrors GET /jobs/{id}/preview: plain-text body markdown, 404 if the
    message has no body (attachment-only ingest -- a valid outcome, not an
    error, so this is the only signal a consumer needs to skip fetching it)."""
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user)
    if message.body_markdown is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Body not available')
    return PlainTextResponse(message.body_markdown)


@router.get('/messages/{message_id}/raw')
def get_mail_message_raw(
    message_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    """The original `.eml`, verbatim -- the JobArtifact content-endpoint
    header conventions (nosniff, private cache, attachment disposition).
    New surface: no endpoint before this served original upload bytes."""
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user, defer_raw=False)
    return Response(
        content=message.raw_content,
        media_type='message/rfc822',
        headers={
            'Content-Disposition': _content_disposition('attachment', f'{message.id}.eml'),
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'private, max-age=3600',
        },
    )


@router.get('/messages/{message_id}/parts/{index}/content')
def get_mail_message_part_content(
    message_id: str, index: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    """Original bytes of one part (incl. inline and skipped ones), extracted
    on the fly by re-running the same deterministic MIME-tree walk against
    `raw_content` -- no double storage. Cross-checked against the stored
    manifest before serving; index out of range or a manifest mismatch both
    404 (design doc)."""
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user, defer_raw=False)

    manifest = message.parts if isinstance(message.parts, list) else []
    manifest_entry = next((entry for entry in manifest if isinstance(entry, dict) and entry.get('index') == index), None)
    if manifest_entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Part not found')

    try:
        extracted = extract_mail_part(message.raw_content, index)
    except MailParseError:  # pragma: no cover - defensive: raw_content only ever holds bytes that already parsed at ingest
        extracted = None
    if extracted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Part not found')
    if extracted.filename != manifest_entry.get('filename') or extracted.content_type != manifest_entry.get('content_type'):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Part not found')

    return Response(
        content=extracted.content,
        media_type=extracted.content_type,
        headers={
            'Content-Disposition': _content_disposition('attachment', extracted.filename),
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'private, max-age=3600',
        },
    )


@router.get('/messages/{message_id}/export.json')
def export_mail_message_json(
    message_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    """Schema `paddledoc.mail-export/1` -- the aggregation consumers (n8n,
    Bedrock AgentCore) want: envelope + body markdown + every attachment's
    OCR markdown (DB-first, result_markdown) in one call."""
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user)

    manifest = message.parts if isinstance(message.parts, list) else []
    job_ids = [entry.get('job_id') for entry in manifest if isinstance(entry, dict) and isinstance(entry.get('job_id'), str)]
    jobs_by_id: dict[str, Job] = {}
    if job_ids:
        jobs = db.scalars(select(Job).where(Job.id.in_(job_ids)).options(*_JOB_DEFER_UPLOAD_CONTENT_ONLY)).all()
        jobs_by_id = {job.id: job for job in jobs}
        _redispatch_stranded_mail_jobs(list(jobs_by_id.values()))

    attachments: list[dict] = []
    complete = True
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        item: dict = {
            'index': entry.get('index'),
            'filename': entry.get('filename'),
            'content_type': entry.get('content_type'),
            'size_bytes': entry.get('size_bytes'),
            'outcome': entry.get('outcome'),
        }
        outcome = entry.get('outcome')
        if outcome == 'skipped':
            item['skip_reason'] = entry.get('skip_reason')
        elif outcome == 'job':
            job_id = entry.get('job_id')
            job = jobs_by_id.get(job_id) if isinstance(job_id, str) else None
            item['job_id'] = job_id
            if job is None:
                item['job_status'] = None
                complete = False
            else:
                item['job_status'] = job.status.value
                item['content_sha256'] = job.content_sha256
                if job.status == JobStatus.FINISHED:
                    item['markdown'] = job.result_markdown
                elif job.status == JobStatus.FAILED:
                    item['error_message'] = job.error_message
                else:
                    complete = False
        attachments.append(item)

    stem = (message.subject or 'message').strip()[:80] or 'message'
    filename = f'{stem}-{message.id}.json'

    payload = {
        'schema': 'paddledoc.mail-export/1',
        'message': {
            'id': message.id,
            'content_sha256': message.content_sha256,
            'rfc_message_id': message.rfc_message_id,
            'subject': message.subject,
            'from_address': message.from_address,
            'recipients': message.recipients if isinstance(message.recipients, dict) else {},
            'sent_at': message.sent_at.isoformat() if message.sent_at else None,
            'source': message.source,
            'created_at': message.created_at.isoformat(),
        },
        'body': {
            'format': message.body_format,
            'markdown': message.body_markdown,
        },
        'attachments': attachments,
        'complete': complete,
    }

    return JSONResponse(
        content=payload,
        headers={'Content-Disposition': _content_disposition('attachment', filename)},
    )


@router.delete('/messages/{message_id}', response_model=MailMessageDeleteResponse)
def delete_mail_message(
    message_id: str,
    request: Request,
    delete_jobs: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MailMessageDeleteResponse:
    """Mirrors delete_import_run in app/api/import_routes.py exactly, for
    exactly the same reason: SQLite (dev + the whole test suite) runs
    without PRAGMA foreign_keys, so the FK's ON DELETE SET NULL never fires
    there -- an explicit UPDATE is required before deleting the row. Without
    `delete_jobs`, attachment jobs keep their history with mail_message_id
    cleared; with it, jobs (and their artifact blob rows, bulk-deleted
    without loading the blobs) are removed too."""
    enforce_rate_limit(request)
    message = _get_visible_mail_message(db, message_id, user)

    deleted_jobs = 0
    if delete_jobs:
        job_ids = db.scalars(select(Job.id).where(Job.mail_message_id == message.id)).all()
        if job_ids:
            db.execute(delete(JobArtifact).where(JobArtifact.job_id.in_(job_ids)))
        jobs = db.scalars(
            select(Job).where(Job.mail_message_id == message.id).options(*_JOB_BLOB_DEFER_OPTIONS)
        ).all()
        for job in jobs:
            db.delete(job)
            deleted_jobs += 1
    else:
        db.execute(update(Job).where(Job.mail_message_id == message.id).values(mail_message_id=None))

    db.delete(message)
    db.commit()
    return MailMessageDeleteResponse(id=message_id, deleted_jobs=deleted_jobs)
