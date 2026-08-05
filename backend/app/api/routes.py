import uuid
from datetime import date, datetime, time, timezone
import io
from pathlib import Path
import shutil
from urllib.parse import quote
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from redis import Redis
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, defer

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.database.session import get_db
from app.models.models import Collection, Job, JobArtifact, JobMarkdownVersion, JobStatus, Tag, User, UserRole
from app.schemas.jobs import (
    ContainerState,
    CollectionCreateRequest,
    CollectionResponse,
    CollectionStartRequest,
    CollectionStartResponse,
    DashboardStatsResponse,
    FolderActionRequest,
    FolderActionResponse,
    MarkdownBrowserResponse,
    MarkdownFileEntry,
    JobListResponse,
    JobResponse,
    JobSaveRequest,
    JobSaveResponse,
    JobSearchResponse,
    PaddleCapabilitiesResponse,
    PaddleSettingsResponse,
    PaddleSettingsUpdate,
    PaddleStatusResponse,
    PasswordVerificationRequest,
    RuntimeCapabilityInfo,
    UploadResponse,
)
from app.schemas.import_ import JobArtifactListResponse, JobArtifactResponse
from app.services.paddle_service import (
    get_paddle_capabilities,
    get_paddle_settings,
    get_paddle_status,
    update_paddle_settings,
)
from app.services.security import enforce_rate_limit, hash_password, verify_password
from app.services.storage import build_result_path, save_upload
from app.workers.celery_app import celery_app
from app.workers.tasks import process_job

router = APIRouter(prefix='/api/v1')

UPLOAD_MODE_VALUES = {'single', 'collection'}
_JOB_LIST_PAGE_LIMIT_MAX = 500

# Job.upload_content and Job.result_markdown are blob-sized columns that most
# listing/administrative queries never read. Deferring them keeps those
# queries cheap; call sites that actually need one of the two pass a
# narrower options tuple instead.
_JOB_BLOB_DEFER_OPTIONS = (defer(Job.upload_content), defer(Job.result_markdown))
_JOB_DEFER_UPLOAD_CONTENT_ONLY = (defer(Job.upload_content),)
# JobArtifact.content is BYTEA-sized; every listing query must defer it --
# only the single-artifact content endpoint may load the blob.
_ARTIFACT_BLOB_DEFER_OPTIONS = (defer(JobArtifact.content),)
# Artifact content types allowed to render inline in the browser; everything
# else (notably SVG, which is never stored as kind='image' anyway) is served
# as an attachment download.
_ARTIFACT_INLINE_CONTENT_TYPES = frozenset({'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf'})
_LOWER_PROFILE_RETRY_MAP = {
    'ppocrv6_medium_structurev3': 'ppocrv6_small_structurev3',
    'ppocrv6_small_structurev3': 'ppocrv6_tiny_structurev3',
    'ppocrv6_medium': 'ppocrv6_tiny',
    'ppocrv6_small': 'ppocrv6_tiny',
}


def _active_process_job_ids() -> set[str]:
    try:
        inspect = celery_app.control.inspect(timeout=5.0)
        active = inspect.active() or {}
    except Exception:
        return set()

    job_ids: set[str] = set()
    for tasks in active.values():
        for task in tasks:
            if not isinstance(task, dict) or task.get('name') != 'process_job':
                continue
            args = task.get('args')
            if isinstance(args, (list, tuple)) and args and isinstance(args[0], str):
                job_ids.add(args[0])
    return job_ids


def _count_active_process_jobs() -> int:
    return len(_active_process_job_ids())


def _parse_tags(raw_tags: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for token in raw_tags.replace('\n', ',').split(','):
        cleaned = token.strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tags.append(cleaned)
    return tags


def _job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        original_filename=job.original_filename,
        status=job.status,
        tags=[tag.name for tag in job.tags],
        error_message=job.error_message,
        processing_info=job.processing_info,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _visible_job_filter(user: User):
    """SQL WHERE fragment enforcing row-level job visibility, composed into
    `_apply_job_filters`/`_job_query`/`_job_count` and applied ad hoc to the
    `/markdown-files` and `/folders/*` queries below.

    admin => None (no extra filter, sees everything).
    non-admin => owner_id == user.id, OR owner_id belongs to a user whose
    CURRENT team_id matches user.team_id (only when the caller is on a
    team). Legacy owner_id IS NULL rows are never matched here, so they
    stay admin-only until claimed via POST /auth/admin/jobs/claim-ownerless.
    """
    if user.role == UserRole.ADMIN:
        return None
    conditions = [Job.owner_id == user.id]
    if user.team_id is not None:
        teammate_ids = select(User.id).where(User.team_id == user.team_id)
        conditions.append(Job.owner_id.in_(teammate_ids))
    return or_(*conditions)


def _apply_visible_filter(query, user: User):
    visible_filter = _visible_job_filter(user)
    if visible_filter is not None:
        query = query.where(visible_filter)
    return query


def _owner_visible(db: Session, owner_id: str | None, user: User) -> bool:
    """Same visibility rule as `_visible_job_filter`, evaluated for a single
    already-loaded owner_id (job or collection) rather than as a query
    fragment. Legacy owner_id=None is admin-only.
    """
    if user.role == UserRole.ADMIN:
        return True
    if owner_id is None:
        return False
    if owner_id == user.id:
        return True
    if user.team_id is None:
        return False
    owner_team_id = db.scalar(select(User.team_id).where(User.id == owner_id))
    return owner_team_id == user.team_id


def _require_visible(db: Session, job: Job, user: User) -> None:
    """404 (not 403) for a job the caller cannot see -- avoids leaking
    cross-team/cross-user existence via status code."""
    if not _owner_visible(db, job.owner_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')


def _require_visible_collection(db: Session, collection: Collection, user: User) -> None:
    if not _owner_visible(db, collection.owner_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')


def _apply_job_filters(
    query,
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = None,
    visible_filter=None,
):
    if q:
        pattern = f'%{q.strip().lower()}%'
        query = query.where(func.lower(Job.original_filename).like(pattern))

    if tag:
        normalized_tag = tag.strip().lower()
        if normalized_tag:
            query = query.join(Job.tags).where(func.lower(Tag.name) == normalized_tag)

    if from_date:
        query = query.where(Job.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
    if to_date:
        query = query.where(Job.created_at <= datetime.combine(to_date, time.max, tzinfo=timezone.utc))
    if status_filter:
        query = query.where(Job.status == status_filter)
    if visible_filter is not None:
        query = query.where(visible_filter)

    return query


def _job_query(
    db: Session,
    user: User,
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Job]:
    query = _apply_job_filters(
        select(Job).order_by(Job.created_at.desc()).options(*_JOB_BLOB_DEFER_OPTIONS),
        q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter,
        visible_filter=_visible_job_filter(user),
    )

    # Absent limit/offset (the default) preserves the historical unbounded
    # behavior so existing frontend callers are unaffected.
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    jobs = db.scalars(query).unique().all()
    return jobs


def _job_count(
    db: Session,
    user: User,
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = None,
) -> int:
    query = _apply_job_filters(
        select(func.count(Job.id.distinct())),
        q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter,
        visible_filter=_visible_job_filter(user),
    )
    return db.scalar(query) or 0


def _check_job_password(job: Job, password: str | None) -> None:
    """Verify job password if it's protected."""
    if not job.password_hash:
        return  # No password protection
    
    if not password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Password required')
    
    if not verify_password(password, job.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid password')


def _is_import_page_job(job: Job) -> bool:
    """True for jobs that ARE an imported Confluence page (settings.mode ==
    'import'). Restarting one would wipe result_markdown -- the converted
    page, unrecoverable short of re-running the whole import -- and feed the
    stored export_view HTML into the OCR pipeline. 'import_attachment'
    children deliberately do NOT match: re-OCRing their stored bytes through
    the untouched pipeline is legitimate."""
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    return settings_info.get('mode') == 'import'


def _content_disposition(disposition: str, filename: str) -> str:
    """RFC 6266/5987 Content-Disposition: ASCII-safe quoted fallback plus a
    UTF-8 filename* parameter for everything else."""
    fallback = ''.join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else '_' for ch in filename) or 'download'
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def _resolve_markdown_path(job: Job) -> Path:
    info = dict(job.processing_info) if isinstance(job.processing_info, dict) else {}
    editor = dict(info.get('editor')) if isinstance(info.get('editor'), dict) else {}
    latest = editor.get('latest_result_path') if isinstance(editor, dict) else None
    if isinstance(latest, str):
        path = Path(latest).resolve()
        if path.exists():
            return path

    edited_dir = (settings.results_dir / 'edited').resolve()
    if edited_dir.exists():
        candidates = sorted(edited_dir.glob(f'{job.id}.v*.md'))
        if candidates:
            return candidates[-1].resolve()

    if not job.result_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Result file not found')
    return Path(job.result_path).resolve()


def _base_processing_info(
    mode: str,
    email: str,
    department: str | None,
    profile_id: str | None = None,
    collection_id: str | None = None,
    folder: str | None = None,
    subfolder: str | None = None,
) -> dict:
    payload: dict[str, object] = {
        'settings': {
            'mode': mode,
            'email': email,
            'department': department,
            'profile_id': profile_id,
            'collection_id': collection_id,
            'folder': folder,
            'subfolder': subfolder,
        }
    }
    return payload


def _sanitize_storage_path(value: str) -> str:
    cleaned_parts: list[str] = []
    for raw_part in value.replace('\\', '/').split('/'):
        part = raw_part.strip()
        if not part:
            continue
        if part in {'.', '..'}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid folder name')
        if any(character in part for character in ('\0',)):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid folder name')
        cleaned_parts.append(part)
    return '/'.join(cleaned_parts)


def _storage_folder(
    job_id: str,
    folder: str = '',
    subfolder: str = '',
) -> str:
    parts: list[str] = []
    folder_path = '/'.join(filter(None, [_sanitize_storage_path(folder), _sanitize_storage_path(subfolder)]))
    if folder_path:
        parts.extend(folder_path.split('/'))
    else:
        parts.append('inbox')
    parts.append(job_id)
    return '/'.join(parts)


def _cleanup_empty_parents(path: Path, stop_dir: Path) -> None:
    if not path.is_relative_to(stop_dir):
        return
    current = path.parent
    while current != stop_dir and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _synthetic_markdown_path(job: Job) -> str:
    """Relative path standing in for the on-disk layout `build_result_path`
    used to produce (`{folder}/{job_id}/{job_id}.md`), now derived purely
    from the job row since there is no shared volume to read a real file
    from.
    """
    return f'{_job_folder_path(job)}/{job.id}/{job.id}.md'


def _markdown_entry_from_job(job: Job) -> MarkdownFileEntry:
    path = _synthetic_markdown_path(job)
    content = job.result_markdown or ''
    return MarkdownFileEntry(
        path=path,
        filename=f'{job.id}.md',
        folder=path.rsplit('/', 1)[0],
        size_bytes=len(content.encode('utf-8')),
        updated_at=job.updated_at,
    )


def _job_folder_path(job: Job) -> str:
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    folder = settings_info.get('folder') if isinstance(settings_info.get('folder'), str) else ''
    subfolder = settings_info.get('subfolder') if isinstance(settings_info.get('subfolder'), str) else ''
    joined = '/'.join(filter(None, [_sanitize_storage_path(folder), _sanitize_storage_path(subfolder)]))
    if joined:
        return joined

    storage_folder = settings_info.get('storage_folder') if isinstance(settings_info.get('storage_folder'), str) else ''
    if not storage_folder:
        return 'inbox'
    parts = [part for part in storage_folder.split('/') if part]
    if len(parts) <= 1:
        return 'inbox'
    return '/'.join(parts[:-1])


def _delete_job_artifacts(job: Job) -> None:
    for candidate in [job.upload_path, job.result_path]:
        if candidate:
            path = Path(candidate).resolve()
            path.unlink(missing_ok=True)
            _cleanup_empty_parents(
                path,
                settings.uploads_dir.resolve() if path.is_relative_to(settings.uploads_dir.resolve()) else settings.results_dir.resolve(),
            )

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    editor = info.get('editor') if isinstance(info.get('editor'), dict) else {}
    versions = editor.get('versions') if isinstance(editor.get('versions'), list) else []
    for version in versions:
        if isinstance(version, dict) and isinstance(version.get('path'), str):
            version_path = Path(version['path']).resolve()
            version_path.unlink(missing_ok=True)
            _cleanup_empty_parents(
                version_path,
                settings.results_dir.resolve(),
            )


def _delete_job_outputs(job: Job) -> None:
    """Delete generated outputs while keeping original uploads for reprocessing."""
    if job.result_path:
        result_file = Path(job.result_path).resolve()
        result_file.unlink(missing_ok=True)
        _cleanup_empty_parents(result_file, settings.results_dir.resolve())

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    editor = info.get('editor') if isinstance(info.get('editor'), dict) else {}

    latest_path = editor.get('latest_result_path') if isinstance(editor.get('latest_result_path'), str) else None
    if latest_path:
        latest_file = Path(latest_path).resolve()
        latest_file.unlink(missing_ok=True)
        _cleanup_empty_parents(latest_file, settings.results_dir.resolve())

    versions = editor.get('versions') if isinstance(editor.get('versions'), list) else []
    for version in versions:
        if isinstance(version, dict) and isinstance(version.get('path'), str):
            version_file = Path(version['path']).resolve()
            version_file.unlink(missing_ok=True)
            _cleanup_empty_parents(version_file, settings.results_dir.resolve())

    # Clear output-related fields in DB, keep settings/tags/upload for reprocessing.
    next_info = {**info} if isinstance(info, dict) else {}
    if isinstance(next_info.get('editor'), dict):
        next_info.pop('editor', None)
    job.processing_info = next_info
    job.result_markdown = None


def _attach_tags(db: Session, job: Job, tags: list[str]) -> None:
    if not tags:
        return
    existing_tags = {tag.name: tag for tag in db.scalars(select(Tag).where(Tag.name.in_(tags))).all()}
    for tag_name in tags:
        tag_obj = existing_tags.get(tag_name)
        if tag_obj is None:
            tag_obj = Tag(name=tag_name)
            db.add(tag_obj)
            existing_tags[tag_name] = tag_obj
        if tag_obj not in job.tags:
            job.tags.append(tag_obj)


def create_job_from_upload(
    db: Session,
    file: UploadFile,
    *,
    owner_id: str,
    storage_folder: str,
    mode: str,
    email: str,
    department: str | None,
    profile_id: str | None,
    folder: str | None,
    subfolder: str | None,
    tags: list[str],
    extra_settings: dict | None = None,
    password_hash: str | None = None,
) -> Job:
    """Shared job-creation path for both the single-file (`/upload`) and
    collection (`/collections/{id}/upload`) upload handlers, previously
    inlined and duplicated in each. `storage_folder` is the full on-disk
    folder built by `_storage_folder(job_id, folder, subfolder)` -- both
    callers already compute it before invoking this, and it always ends in
    the job id, which is what's used for the DB row here so the id on disk
    and the id in the DB never disagree. Does not commit; the caller commits
    once it's done with any other work for the same request (e.g. tracking
    the job against a collection).
    """
    file_id = storage_folder.rsplit('/', 1)[-1]
    upload_path, _, upload_content, upload_size = save_upload(file, storage_folder, file_id)
    result_path = build_result_path(storage_folder, file_id)

    job = Job(
        id=file_id,
        original_filename=file.filename or 'upload',
        upload_path=upload_path,
        upload_content=upload_content,
        upload_mime_type=file.content_type,
        upload_size_bytes=upload_size,
        status=JobStatus.PENDING,
        result_path=str(result_path),
        password_hash=password_hash,
        owner_id=owner_id,
    )
    job.processing_info = _base_processing_info(
        mode=mode,
        email=email,
        department=department,
        profile_id=profile_id,
        folder=folder,
        subfolder=subfolder,
    )
    job.processing_info['settings']['storage_folder'] = storage_folder
    if extra_settings:
        job.processing_info['settings'].update(extra_settings)

    db.add(job)
    _attach_tags(db, job, tags)
    return job


def _database_size_bytes() -> int | None:
    if not settings.database_url.startswith('sqlite:'):
        return None
    database_path = settings.database_url.removeprefix('sqlite:///')
    if not database_path or database_path == ':memory:':
        return None
    path = Path(database_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return None
    return path.stat().st_size


def _estimate_database_payload_bytes(db: Session) -> int:
    upload_total = db.scalar(select(func.coalesce(func.sum(Job.upload_size_bytes), 0))) or 0
    markdown_total = db.scalar(select(func.coalesce(func.sum(func.length(Job.result_markdown)), 0))) or 0
    artifact_total = db.scalar(select(func.coalesce(func.sum(JobArtifact.size_bytes), 0))) or 0
    return int(upload_total) + int(markdown_total) + int(artifact_total)


def _resolve_database_size_bytes(db: Session) -> int:
    sqlite_size = _database_size_bytes()
    if sqlite_size is not None:
        return sqlite_size

    if settings.database_url.startswith(('postgresql://', 'postgresql+psycopg://', 'postgres://')):
        try:
            row = db.execute(text('SELECT pg_database_size(current_database())')).first()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            pass

    return _estimate_database_payload_bytes(db)


def _collection_job_ids(db: Session, collection_id: str, user: User) -> list[str]:
    """Jobs are linked to a collection via processing_info.settings.collection_id
    (there is no FK column for it), the same pattern already used for
    folder membership (`_job_folder_path`) elsewhere in this file. Scoped by
    `_apply_visible_filter` so a non-admin only ever sees/starts the subset
    of a collection's jobs they're allowed to see -- relevant mainly for
    admin-created collections a regular member later uploads into.
    """
    jobs = db.scalars(_apply_visible_filter(select(Job).options(*_JOB_BLOB_DEFER_OPTIONS), user)).all()
    ids: list[str] = []
    for job in jobs:
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
        if settings_info.get('collection_id') == collection_id:
            ids.append(job.id)
    return ids


@router.post('/collections', response_model=CollectionResponse)
def create_collection(
    request: Request, payload: CollectionCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> CollectionResponse:
    enforce_rate_limit(request)
    email = payload.email.strip()
    department = payload.department.strip()
    folder_clean = _sanitize_storage_path(payload.folder)
    subfolder_clean = _sanitize_storage_path(payload.subfolder)
    password_hash = None
    if payload.password.strip():
        password_hash = hash_password(payload.password.strip())
    collection = Collection(
        owner_id=user.id,
        email=email,
        department=department,
        folder=folder_clean,
        subfolder=subfolder_clean,
        password_hash=password_hash,
    )
    db.add(collection)
    db.commit()
    return CollectionResponse(
        collection_id=collection.id,
        email=email,
        department=department,
        folder=folder_clean,
        subfolder=subfolder_clean,
        job_ids=[],
    )


@router.get('/collections/{collection_id}', response_model=CollectionResponse)
def get_collection(collection_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> CollectionResponse:
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')
    _require_visible_collection(db, collection, user)
    return CollectionResponse(
        collection_id=collection.id,
        email=collection.email,
        department=collection.department,
        folder=collection.folder,
        subfolder=collection.subfolder,
        job_ids=_collection_job_ids(db, collection.id, user),
    )


@router.post('/collections/{collection_id}/upload', response_model=UploadResponse)
def upload_document_to_collection(
    request: Request,
    collection_id: str,
    file: UploadFile = File(...),
    folder: str = Form(''),
    subfolder: str = Form(''),
    tags: str = Form(''),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadResponse:
    # Intentionally skip per-file rate limiting here so large collection
    # uploads (100+ files) are not blocked mid-batch.
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')
    _require_visible_collection(db, collection, user)

    file_id = str(uuid.uuid4())
    folder_value = folder.strip() or collection.folder or ''
    subfolder_value = subfolder.strip() or collection.subfolder or ''
    storage_folder = _storage_folder(file_id, folder_value, subfolder_value)

    job = create_job_from_upload(
        db,
        file,
        owner_id=user.id,
        storage_folder=storage_folder,
        mode='collection',
        email=collection.email,
        department=collection.department,
        profile_id=None,
        folder=folder_value or None,
        subfolder=subfolder_value or None,
        tags=_parse_tags(tags),
        extra_settings={'collection_id': collection_id},
        password_hash=collection.password_hash,
    )
    db.commit()

    return UploadResponse(job_id=job.id, status=job.status)


@router.post('/collections/{collection_id}/start', response_model=CollectionStartResponse)
def start_collection_processing(
    collection_id: str,
    payload: CollectionStartRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CollectionStartResponse:
    collection = db.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')
    _require_visible_collection(db, collection, user)

    job_ids = _collection_job_ids(db, collection_id, user)
    if not job_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='No files uploaded to collection')

    started = 0
    for job_id in job_ids:
        job = db.get(Job, job_id)
        if job is None:
            continue
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
        settings_info['profile_id'] = payload.profile_id
        settings_info['mode'] = 'collection'
        settings_info['email'] = collection.email
        settings_info['department'] = collection.department
        settings_info['collection_id'] = collection_id
        job.processing_info = {**info, 'settings': settings_info}
        process_job.delay(
            job.id,
            payload.profile_id,
            'collection',
            collection.email,
            collection.department,
        )
        started += 1
    db.commit()

    return CollectionStartResponse(
        collection_id=collection_id,
        started_jobs=started,
        profile_id=payload.profile_id,
    )


@router.post('/upload', response_model=UploadResponse)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    profile_id: str = Form('ppocrv6_tiny'),
    email: str = Form(''),
    mode: str = Form('single'),
    folder: str = Form(''),
    subfolder: str = Form(''),
    tags: str = Form(''),
    password: str = Form(''),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadResponse:
    enforce_rate_limit(request)
    mode_clean = mode.strip().lower()
    if mode_clean not in UPLOAD_MODE_VALUES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='mode must be single or collection')
    if mode_clean != 'single':
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Use collection endpoints for collection mode')
    email_clean = email.strip()
    folder_clean = folder.strip()
    subfolder_clean = subfolder.strip()
    password_hash = None
    if password.strip():
        password_hash = hash_password(password.strip())

    file_id = str(uuid.uuid4())
    storage_folder = _storage_folder(file_id, folder_clean, subfolder_clean)

    job = create_job_from_upload(
        db,
        file,
        owner_id=user.id,
        storage_folder=storage_folder,
        mode='single',
        email=email_clean,
        department=None,
        profile_id=profile_id,
        folder=folder_clean or None,
        subfolder=subfolder_clean or None,
        tags=_parse_tags(tags),
        password_hash=password_hash,
    )
    db.commit()

    process_job.delay(file_id, profile_id, 'single', email_clean, None)
    return UploadResponse(job_id=job.id, status=job.status)


@router.post('/jobs/{job_id}/verify-password')
def verify_job_password(
    job_id: str,
    payload: PasswordVerificationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)

    if not job.password_hash:
        # No password protection, always allowed
        return {'verified': True}

    if verify_password(payload.password, job.password_hash):
        return {'verified': True}
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid password')


@router.get('/jobs/{job_id}', response_model=JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> JobResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    return _job_to_response(job)


@router.get('/jobs', response_model=JobListResponse)
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = Query(default=None, alias='status'),
    limit: int | None = Query(default=None, ge=0, le=_JOB_LIST_PAGE_LIMIT_MAX),
    offset: int | None = Query(default=None, ge=0),
) -> JobListResponse:
    jobs = _job_query(
        db, user, q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter, limit=limit, offset=offset
    )
    items = [_job_to_response(job) for job in jobs]

    # UI normalization: if workers report active process_job IDs, treat any
    # non-active RUNNING entries as queued/pending to avoid stale RUNNING noise.
    active_job_ids = _active_process_job_ids()
    if active_job_ids:
        for item in items:
            if item.status == JobStatus.RUNNING and item.id not in active_job_ids:
                item.status = JobStatus.PENDING

    return JobListResponse(items=items)


@router.get('/search', response_model=JobSearchResponse)
def search_documents(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = Query(default=None, alias='status'),
    limit: int | None = Query(default=None, ge=0, le=_JOB_LIST_PAGE_LIMIT_MAX),
    offset: int | None = Query(default=None, ge=0),
) -> JobSearchResponse:
    jobs = _job_query(
        db, user, q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter, limit=limit, offset=offset
    )
    items = [_job_to_response(job) for job in jobs]

    active_job_ids = _active_process_job_ids()
    if active_job_ids:
        for item in items:
            if item.status == JobStatus.RUNNING and item.id not in active_job_ids:
                item.status = JobStatus.PENDING

    # With pagination active, len(items) is only the page size; report the
    # true match count instead so callers can build pagination UI on it.
    if limit is not None or offset is not None:
        total = _job_count(db, user, q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter)
    else:
        total = len(items)
    return JobSearchResponse(items=items, total=total)


@router.post('/jobs/restart-pending')
def restart_pending_jobs(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)) -> dict[str, int]:
    enforce_rate_limit(request)

    # Keep truly active RUNNING tasks and only requeue excess RUNNING jobs.
    active_process_jobs = _count_active_process_jobs()
    running_jobs = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.RUNNING)
        .order_by(Job.updated_at.desc())
        .options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()
    stuck_running = running_jobs[active_process_jobs:]

    for job in stuck_running:
        existing = job.processing_info if isinstance(job.processing_info, dict) else {}
        execution = existing.get('execution') if isinstance(existing.get('execution'), dict) else {}
        job.processing_info = {
            **existing,
            'execution': {
                **execution,
                'status': 'requeued',
                'detail': 'Job was stuck in RUNNING state and has been requeued.',
            },
        }
        job.status = JobStatus.PENDING
    if stuck_running:
        db.commit()

    pending_jobs = db.scalars(
        select(Job).where(Job.status == JobStatus.PENDING).options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()
    restarted = 0
    for job in pending_jobs:
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}

        profile_id = settings_info.get('profile_id') if isinstance(settings_info.get('profile_id'), str) else None
        mode = settings_info.get('mode') if isinstance(settings_info.get('mode'), str) else None
        email = settings_info.get('email') if isinstance(settings_info.get('email'), str) else None
        department = settings_info.get('department') if isinstance(settings_info.get('department'), str) else None

        process_job.delay(job.id, profile_id, mode, email, department)
        restarted += 1

    return {
        'pending_jobs': len(pending_jobs),
        'queued_jobs': restarted,
        'recovered_running': len(stuck_running),
    }


@router.post('/jobs/{job_id}/restart')
def restart_job(job_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict[str, str]:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    if _is_import_page_job(job):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Imported pages cannot be restarted')

    # Allow requeue for stale RUNNING records, but block truly active jobs.
    active_job_ids = _active_process_job_ids()
    if job.status == JobStatus.RUNNING and job.id in active_job_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job is currently running')

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}

    profile_id = settings_info.get('profile_id') if isinstance(settings_info.get('profile_id'), str) else None
    mode = settings_info.get('mode') if isinstance(settings_info.get('mode'), str) else None
    email = settings_info.get('email') if isinstance(settings_info.get('email'), str) else None
    department = settings_info.get('department') if isinstance(settings_info.get('department'), str) else None

    _delete_job_outputs(job)
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
    job.processing_info = {
        **info,
        'execution': {
            **execution,
            'status': 'requeued',
            'detail': 'Job was manually restarted from the jobs list.',
        },
    }
    job.status = JobStatus.PENDING
    job.error_message = None
    db.commit()

    process_job.delay(job.id, profile_id, mode, email, department)

    return {
        'job_id': job.id,
        'status': 'queued',
    }


@router.post('/jobs/{job_id}/retry-lower-profile')
def retry_job_with_lower_profile(
    job_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, str]:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    if _is_import_page_job(job):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Imported pages cannot be restarted')

    active_job_ids = _active_process_job_ids()
    if job.status == JobStatus.RUNNING and job.id in active_job_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job is currently running')

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}

    current_profile = (
        settings_info.get('profile_id') if isinstance(settings_info.get('profile_id'), str) else None
    ) or (
        settings_info.get('requested_profile_id') if isinstance(settings_info.get('requested_profile_id'), str) else None
    )
    if not current_profile:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job has no profile configured')

    lower_profile = _LOWER_PROFILE_RETRY_MAP.get(current_profile)
    if not lower_profile:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'No lower profile available for {current_profile}',
        )

    mode = settings_info.get('mode') if isinstance(settings_info.get('mode'), str) else None
    email = settings_info.get('email') if isinstance(settings_info.get('email'), str) else None
    department = settings_info.get('department') if isinstance(settings_info.get('department'), str) else None

    _delete_job_outputs(job)

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}

    job.processing_info = {
        **info,
        'settings': {
            **settings,
            'previous_profile_id': current_profile,
            'requested_profile_id': lower_profile,
            'profile_id': lower_profile,
        },
        'execution': {
            **execution,
            'status': 'requeued',
            'detail': f'Job retried manually with lower profile {lower_profile} (previous: {current_profile}).',
        },
    }
    job.status = JobStatus.PENDING
    job.error_message = None
    db.commit()

    process_job.delay(job.id, lower_profile, mode, email, department)
    return {
        'job_id': job.id,
        'status': 'queued',
        'profile_id': lower_profile,
    }


@router.post('/folders/{folder_path:path}/restart')
def restart_folder(
    folder_path: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, int | str]:
    enforce_rate_limit(request)

    normalized = _sanitize_storage_path(folder_path)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder path required')

    active_job_ids = _active_process_job_ids()
    jobs = db.scalars(_apply_visible_filter(select(Job).options(*_JOB_BLOB_DEFER_OPTIONS), user)).all()
    folder_jobs = [
        job
        for job in jobs
        if (fp := _job_folder_path(job)) == normalized or fp.startswith(f'{normalized}/')
    ]

    restarted = 0
    skipped_import_jobs = 0
    for job in folder_jobs:
        # Imported pages are skipped and reported rather than failing the
        # whole folder: restarting them would bulk-wipe converted markdown.
        if _is_import_page_job(job):
            skipped_import_jobs += 1
            continue
        if job.status == JobStatus.RUNNING and job.id in active_job_ids:
            continue

        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
        profile_id = settings_info.get('profile_id') if isinstance(settings_info.get('profile_id'), str) else None
        mode = settings_info.get('mode') if isinstance(settings_info.get('mode'), str) else None
        email = settings_info.get('email') if isinstance(settings_info.get('email'), str) else None
        department = settings_info.get('department') if isinstance(settings_info.get('department'), str) else None

        _delete_job_outputs(job)

        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
        job.processing_info = {
            **info,
            'execution': {
                **execution,
                'status': 'requeued',
                'detail': 'Job was manually restarted from the folder action.',
            },
        }
        job.status = JobStatus.PENDING
        job.error_message = None
        process_job.delay(job.id, profile_id, mode, email, department)
        restarted += 1

    db.commit()
    return {'path': normalized, 'restarted_jobs': restarted, 'skipped_import_jobs': skipped_import_jobs}


@router.get('/stats', response_model=DashboardStatsResponse)
def dashboard_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> DashboardStatsResponse:
    processed_documents = db.scalar(
        _apply_visible_filter(select(func.count()).select_from(Job).where(Job.status == JobStatus.FINISHED), user)
    ) or 0
    failed_documents = db.scalar(
        _apply_visible_filter(select(func.count()).select_from(Job).where(Job.status == JobStatus.FAILED), user)
    ) or 0
    finished_jobs = db.scalars(
        _apply_visible_filter(select(Job).where(Job.status == JobStatus.FINISHED).options(*_JOB_BLOB_DEFER_OPTIONS), user)
    ).all()
    processed_pages = 0
    for job in finished_jobs:
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
        page_count = execution.get('page_count')
        if not isinstance(page_count, int):
            structure = execution.get('structure') if isinstance(execution.get('structure'), dict) else {}
            page_count = structure.get('page_count')
        if isinstance(page_count, int):
            processed_pages += page_count

    return DashboardStatsResponse(
        processed_documents=processed_documents,
        processed_pages=processed_pages,
        errors=failed_documents,
        database_size_bytes=_resolve_database_size_bytes(db),
    )


@router.get('/jobs/{job_id}/download')
def download_markdown(
    job_id: str,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    if job.status != JobStatus.FINISHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job not finished')

    # Login + visibility above are the real gate now; a per-job password (if
    # set) is only an extra defense-in-depth check for a caller who is
    # already authorized to see this job -- it never grants access on its
    # own.
    _check_job_password(job, password)

    filename = f'{job_id}.md'

    # DB-first: with no shared volume between backend and worker, the
    # database is the source of truth. Disk lookup is a legacy fallback for
    # rows written before result_markdown existed (NULL column).
    if job.result_markdown is not None:
        return Response(
            content=job.result_markdown,
            media_type='text/markdown',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    result_path = _resolve_markdown_path(job)
    if not result_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Result file not found')

    return FileResponse(result_path, media_type='text/markdown', filename=filename)


@router.get('/jobs/{job_id}/preview')
def preview_markdown(
    job_id: str,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlainTextResponse:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Preview not available')
    _require_visible(db, job, user)

    _check_job_password(job, password)

    if job.result_markdown:
        return PlainTextResponse(job.result_markdown)
    path = _resolve_markdown_path(job)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Preview not available')
    return PlainTextResponse(path.read_text(encoding='utf-8'))


@router.get('/jobs/{job_id}/artifacts', response_model=JobArtifactListResponse)
def list_job_artifacts(
    job_id: str,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobArtifactListResponse:
    enforce_rate_limit(request)

    job = db.get(Job, job_id, options=list(_JOB_BLOB_DEFER_OPTIONS))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)

    # Same gate as /preview and /download: a password-protected job must not
    # leak artifact filenames/types/sizes without the password.
    _check_job_password(job, password)

    artifacts = db.scalars(
        select(JobArtifact)
        .where(JobArtifact.job_id == job_id)
        .order_by(JobArtifact.filename)
        .options(*_ARTIFACT_BLOB_DEFER_OPTIONS)
    ).all()
    return JobArtifactListResponse(items=[JobArtifactResponse.model_validate(artifact) for artifact in artifacts])


@router.get('/jobs/{job_id}/artifacts/{artifact_id}/content')
def get_job_artifact_content(
    job_id: str,
    artifact_id: str,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    enforce_rate_limit(request)

    job = db.get(Job, job_id, options=list(_JOB_BLOB_DEFER_OPTIONS))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    _check_job_password(job, password)

    # The (artifact_id AND job_id) binding lives in the query itself: passing
    # your own visible job_id plus a foreign artifact_id must 404, never
    # return the foreign bytes (IDOR). Never load-by-id-then-check.
    artifact = db.scalar(
        select(JobArtifact).where(JobArtifact.id == artifact_id, JobArtifact.job_id == job_id)
    )
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Artifact not found')

    disposition = 'inline' if artifact.content_type in _ARTIFACT_INLINE_CONTENT_TYPES else 'attachment'
    return Response(
        content=artifact.content,
        # Our validated stored classification, never the remote's header.
        media_type=artifact.content_type,
        headers={
            'Content-Disposition': _content_disposition(disposition, artifact.filename),
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'private, max-age=3600',
        },
    )


@router.put('/jobs/{job_id}/save', response_model=JobSaveResponse)
def save_markdown(
    job_id: str,
    payload: JobSaveRequest,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JobSaveResponse:
    enforce_rate_limit(request)

    # Row lock serializes concurrent saves on the same job so the
    # max(version)+1 read below cannot race into the (job_id, version)
    # unique constraint. No-op on SQLite (single writer anyway).
    job = db.get(Job, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    if job.status != JobStatus.FINISHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job not finished')

    _check_job_password(job, password)

    content = payload.markdown.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Markdown content cannot be empty')
    if not content.startswith('---\n'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Markdown must start with YAML frontmatter')

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    editor = info.get('editor') if isinstance(info.get('editor'), dict) else {}

    # DB-first: with no shared volume between backend and worker, version
    # history is truth-sourced from job_markdown_versions rows rather than
    # from the (possibly stale, e.g. cleared by a job restart) editor
    # metadata mirrored below. This also sidesteps the (job_id, version)
    # unique constraint being violated if processing_info ever drifts from
    # the version rows already on record.
    highest_version = db.scalar(
        select(func.max(JobMarkdownVersion.version)).where(JobMarkdownVersion.job_id == job.id)
    ) or 0
    version = highest_version + 1

    now = datetime.now(timezone.utc)
    db.add(JobMarkdownVersion(job_id=job.id, version=version, content=payload.markdown, created_at=now))

    # Legacy on-disk '.v{n}.md' files are gone; 'path' keys stay in the JSON
    # shape for backward compatibility but are now always null.
    versions = list(editor.get('versions')) if isinstance(editor.get('versions'), list) else []
    versions.append({'version': version, 'path': None, 'updated_at': now.isoformat()})
    info['editor'] = {
        'version': version,
        'latest_result_path': None,
        'updated_at': now.isoformat(),
        'versions': versions,
    }
    job.processing_info = {**info}
    job.result_markdown = payload.markdown
    db.commit()

    return JobSaveResponse(
        job_id=job.id,
        version=version,
        path=None,
        updated_at=now,
    )


@router.delete('/jobs/{job_id}')
def delete_job(
    job_id: str,
    request: Request,
    password: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    enforce_rate_limit(request)

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)

    _check_job_password(job, password)

    _delete_job_artifacts(job)

    db.delete(job)
    db.commit()
    return {'status': 'deleted'}


@router.get('/paddle/status', response_model=PaddleStatusResponse)
def paddle_status(db: Session = Depends(get_db)) -> PaddleStatusResponse:
    pending_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.PENDING)) or 0
    db_running_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING)) or 0
    active_process_jobs = _count_active_process_jobs()
    running_jobs = active_process_jobs if active_process_jobs > 0 else int(db_running_jobs)

    # If DB has more RUNNING than actual active tasks, treat the delta as queued.
    if int(db_running_jobs) > running_jobs:
        pending_jobs = int(pending_jobs) + (int(db_running_jobs) - running_jobs)

    queue_total = int(pending_jobs) + int(running_jobs)

    status_name, detail, runtime_dict = get_paddle_status()
    worker_nodes: list[str] = []
    try:
        inspect = celery_app.control.inspect(timeout=5.0)
        ping_payload = inspect.ping() or {}
        worker_nodes = sorted(ping_payload.keys())
    except Exception:
        worker_nodes = []

    effective_status = status_name
    effective_detail = detail

    if status_name in {'stopped', 'failed'} and queue_total > 0:
        effective_status = 'running'
        backlog_detail = f'Worker probe is degraded, but {queue_total} queued/running job(s) remain.'
        effective_detail = f'{detail}. {backlog_detail}' if detail else backlog_detail

    runtime = RuntimeCapabilityInfo(**runtime_dict) if runtime_dict and all(
        k in runtime_dict for k in ('torch_available', 'cuda_available', 'selected_device', 'platform')
    ) else None

    database_state = 'running'
    database_detail = None
    try:
        db.execute(text('SELECT 1'))
    except Exception as exc:
        database_state = 'stopped'
        database_detail = str(exc)

    redis_state = 'running'
    redis_detail = None
    try:
        Redis.from_url(settings.redis_url, decode_responses=True).ping()
    except Exception as exc:
        redis_state = 'stopped'
        redis_detail = str(exc)

    worker_state = 'running' if worker_nodes else 'stopped'
    if queue_total > 0 and not worker_nodes:
        worker_state = 'degraded'

    containers = [
        ContainerState(name='frontend', state='unknown', detail='Reported by browser UI only'),
        ContainerState(name='backend', state='running'),
        ContainerState(name='worker', state=worker_state, detail=', '.join(worker_nodes) if worker_nodes else None),
        ContainerState(name='redis', state=redis_state, detail=redis_detail),
        ContainerState(name='database', state=database_state, detail=database_detail),
    ]

    return PaddleStatusResponse(
        status=effective_status,
        detail=effective_detail,
        runtime=runtime,
        pending_jobs=int(pending_jobs),
        running_jobs=int(running_jobs),
        queue_total=queue_total,
        running_workers=len(worker_nodes),
        worker_nodes=worker_nodes,
        containers=containers,
    )


@router.get('/paddle/settings', response_model=PaddleSettingsResponse)
def get_paddle_runtime_settings() -> PaddleSettingsResponse:
    return PaddleSettingsResponse(**get_paddle_settings())


@router.get('/paddle/capabilities', response_model=PaddleCapabilitiesResponse)
def get_paddle_capability_options() -> PaddleCapabilitiesResponse:
    return PaddleCapabilitiesResponse(**get_paddle_capabilities())


@router.put('/paddle/settings', response_model=PaddleSettingsResponse)
def update_paddle_runtime_settings(payload: PaddleSettingsUpdate, user: User = Depends(require_admin)) -> PaddleSettingsResponse:
    update_paddle_settings(
        default_profile=payload.default_profile,
        timeout_seconds=payload.timeout_seconds,
    )
    return PaddleSettingsResponse(**get_paddle_settings())


@router.get('/markdown-files', response_model=MarkdownBrowserResponse)
def list_markdown_files(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> MarkdownBrowserResponse:
    # DB-derived: no shared volume between backend and worker, so the
    # filesystem is never consulted here. Every finished job with markdown
    # on record and visible to the caller is surfaced as a synthetic tree
    # entry.
    jobs = db.scalars(
        _apply_visible_filter(
            select(Job)
            .where(Job.status == JobStatus.FINISHED, Job.result_markdown.isnot(None))
            .options(*_JOB_DEFER_UPLOAD_CONTENT_ONLY),
            user,
        )
    ).all()
    entries = sorted((_markdown_entry_from_job(job) for job in jobs), key=lambda entry: entry.path)
    return MarkdownBrowserResponse(items=entries)


@router.get('/markdown-files/{relative_path:path}')
def get_markdown_file(
    relative_path: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> PlainTextResponse:
    if not relative_path.lower().endswith('.md'):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Markdown file not found')

    # The synthetic layout always names the file after the job id, so the
    # path stem is a direct, O(1) lookup key rather than a full table scan.
    job_id = Path(relative_path).stem
    job = db.get(Job, job_id, options=[defer(Job.upload_content)])
    if (
        job is None
        or job.status != JobStatus.FINISHED
        or job.result_markdown is None
        or _synthetic_markdown_path(job) != relative_path
        or not _owner_visible(db, job.owner_id, user)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Markdown file not found')
    return PlainTextResponse(job.result_markdown)


@router.post('/folders', response_model=FolderActionResponse)
def create_folder(payload: FolderActionRequest) -> FolderActionResponse:
    folder_path = '/'.join(filter(None, [_sanitize_storage_path(payload.folder), _sanitize_storage_path(payload.subfolder)]))
    if not folder_path:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder or subfolder required')

    uploads_folder = settings.uploads_dir.resolve() / folder_path
    uploads_folder.mkdir(parents=True, exist_ok=True)
    (settings.results_dir.resolve() / folder_path).mkdir(parents=True, exist_ok=True)

    # On Mountpoint-for-S3, mkdir() is local-only until a file is written inside
    # it, so an empty folder never becomes a real prefix in S3 and stays
    # invisible to other pods. Write an empty marker file to force the prefix
    # to actually exist.
    marker = uploads_folder / '.keep'
    if not marker.exists():
        marker.write_bytes(b'')

    return FolderActionResponse(path=folder_path)


@router.get('/folders/{folder_path:path}/download')
def download_folder_markdown(
    folder_path: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> StreamingResponse:
    normalized = _sanitize_storage_path(folder_path)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder path required')

    jobs = db.scalars(
        _apply_visible_filter(
            select(Job).where(Job.status == JobStatus.FINISHED).options(*_JOB_DEFER_UPLOAD_CONTENT_ONLY), user
        )
    ).all()
    folder_jobs = [
        job
        for job in jobs
        if (fp := _job_folder_path(job)) == normalized or fp.startswith(f'{normalized}/')
    ]
    if not folder_jobs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No finished jobs found in this folder')

    archive_buffer = io.BytesIO()
    exported_files = 0
    with zipfile.ZipFile(archive_buffer, mode='w', compression=zipfile.ZIP_DEFLATED) as zip_file:
        for job in folder_jobs:
            if job.password_hash:
                continue

            job_folder = _job_folder_path(job)
            relative_folder = job_folder[len(normalized):].lstrip('/') if job_folder.startswith(normalized) else ''
            stem = Path(job.original_filename).stem.strip() or job.id
            archive_name = '/'.join(filter(None, [relative_folder, f'{stem}-{job.id}.md']))

            # DB-first: fall back to disk only for legacy rows with no
            # result_markdown (written before the column existed).
            if job.result_markdown is not None:
                zip_file.writestr(archive_name, job.result_markdown)
                exported_files += 1
                continue

            try:
                markdown_path = _resolve_markdown_path(job)
            except HTTPException:
                continue
            if not markdown_path.exists():
                continue
            zip_file.write(markdown_path, arcname=archive_name)
            exported_files += 1

    if exported_files == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No downloadable markdown files found in this folder',
        )

    archive_buffer.seek(0)
    filename = f"{normalized.replace('/', '_')}-markdown.zip"
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
    }
    return StreamingResponse(archive_buffer, media_type='application/zip', headers=headers)


@router.delete('/folders/{folder_path:path}', response_model=FolderActionResponse)
def delete_folder(
    folder_path: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FolderActionResponse:
    normalized = _sanitize_storage_path(folder_path)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder path required')

    jobs = db.scalars(_apply_visible_filter(select(Job).options(*_JOB_BLOB_DEFER_OPTIONS), user)).all()
    folder_jobs = [
        job
        for job in jobs
        if (fp := _job_folder_path(job)) == normalized or fp.startswith(f'{normalized}/')
    ]

    deleted_jobs = 0
    for job in folder_jobs:
        _delete_job_artifacts(job)
        db.delete(job)
        deleted_jobs += 1
    db.commit()

    # The physical folder on disk may still hold artifacts for jobs the
    # current caller can't see (other users/teams sharing the same folder
    # path), so only an admin -- who by definition can see everything that
    # could be in there -- is allowed to actually remove it from disk. A
    # non-admin's delete only ever removes the DB rows (and files) for jobs
    # visible to them above.
    if user.role == UserRole.ADMIN:
        shutil.rmtree((settings.uploads_dir.resolve() / normalized), ignore_errors=True)
        shutil.rmtree((settings.results_dir.resolve() / normalized), ignore_errors=True)

    return FolderActionResponse(path=normalized, deleted_jobs=deleted_jobs)
