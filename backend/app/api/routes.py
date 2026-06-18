import uuid
from datetime import date, datetime, time, timezone
import io
from pathlib import Path
import shutil
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from redis import Redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.session import get_db
from app.models.models import Job, JobStatus, Tag
from app.schemas.jobs import (
    ContainerState,
    CollectionCreateRequest,
    CollectionResponse,
    CollectionStartRequest,
    CollectionStartResponse,
    DashboardStatsResponse,
    FolderActionRequest,
    FolderActionResponse,
    HealthResponse,
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
from app.services.paddle_service import (
    get_paddle_capabilities,
    get_paddle_settings,
    get_paddle_status,
    update_paddle_settings,
)
from app.services.security import enforce_rate_limit, hash_password, verify_password
from app.services.storage import build_edited_result_path, build_result_path, save_upload
from app.workers.celery_app import celery_app
from app.workers.tasks import process_job

router = APIRouter(prefix='/api/v1')

UPLOAD_MODE_VALUES = {'single', 'collection'}
_COLLECTIONS: dict[str, dict] = {}


def _count_active_process_jobs() -> int:
    try:
        inspect = celery_app.control.inspect(timeout=5.0)
        active = inspect.active() or {}
    except Exception:
        return 0

    return sum(
        1
        for tasks in active.values()
        for task in tasks
        if isinstance(task, dict) and task.get('name') == 'process_job'
    )


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


def _job_query(
    db: Session,
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = None,
) -> list[Job]:
    query = select(Job).order_by(Job.created_at.desc())

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

    jobs = db.scalars(query).unique().all()
    return jobs


def _check_job_password(job: Job, password: str | None) -> None:
    """Verify job password if it's protected."""
    if not job.password_hash:
        return  # No password protection
    
    if not password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Password required')
    
    if not verify_password(password, job.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid password')


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


def _markdown_entry_from_path(path: Path) -> MarkdownFileEntry:
    resolved = path.resolve()
    relative = resolved.relative_to(settings.results_dir.resolve())
    stat = resolved.stat()
    return MarkdownFileEntry(
        path=str(relative).replace('\\', '/'),
        filename=resolved.name,
        folder=str(relative.parent).replace('\\', '/') if str(relative.parent) != '.' else '',
        size_bytes=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
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
    return int(upload_total) + int(markdown_total)


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


@router.post('/collections', response_model=CollectionResponse)
def create_collection(request: Request, payload: CollectionCreateRequest) -> CollectionResponse:
    enforce_rate_limit(request)
    email = payload.email.strip()
    department = payload.department.strip()
    collection_id = str(uuid.uuid4())
    folder_clean = _sanitize_storage_path(payload.folder)
    subfolder_clean = _sanitize_storage_path(payload.subfolder)
    password_hash = None
    if payload.password.strip():
        password_hash = hash_password(payload.password.strip())
    _COLLECTIONS[collection_id] = {
        'email': email,
        'department': department,
        'folder': folder_clean,
        'subfolder': subfolder_clean,
        'password_hash': password_hash,
        'job_ids': [],
    }
    return CollectionResponse(
        collection_id=collection_id,
        email=email,
        department=department,
        folder=folder_clean,
        subfolder=subfolder_clean,
        job_ids=[],
    )


@router.get('/collections/{collection_id}', response_model=CollectionResponse)
def get_collection(collection_id: str) -> CollectionResponse:
    collection = _COLLECTIONS.get(collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')
    return CollectionResponse(
        collection_id=collection_id,
        email=collection['email'],
        department=collection['department'],
        folder=collection.get('folder', ''),
        subfolder=collection.get('subfolder', ''),
        job_ids=collection['job_ids'],
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
) -> UploadResponse:
    # Intentionally skip per-file rate limiting here so large collection
    # uploads (100+ files) are not blocked mid-batch.
    collection = _COLLECTIONS.get(collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')

    file_id = str(uuid.uuid4())
    folder_value = folder.strip() or str(collection.get('folder') or '')
    subfolder_value = subfolder.strip() or str(collection.get('subfolder') or '')
    storage_folder = _storage_folder(file_id, folder_value, subfolder_value)
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
        password_hash=collection.get('password_hash'),
    )
    job.processing_info = _base_processing_info(
        mode='collection',
        email=collection['email'],
        department=collection['department'],
        collection_id=collection_id,
        folder=folder_value or None,
        subfolder=subfolder_value or None,
    )
    job.processing_info['settings']['storage_folder'] = storage_folder
    db.add(job)
    _attach_tags(db, job, _parse_tags(tags))
    db.commit()

    collection['job_ids'].append(file_id)
    return UploadResponse(job_id=job.id, status=job.status)


@router.post('/collections/{collection_id}/start', response_model=CollectionStartResponse)
def start_collection_processing(
    collection_id: str,
    payload: CollectionStartRequest,
    db: Session = Depends(get_db),
) -> CollectionStartResponse:
    collection = _COLLECTIONS.get(collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Collection not found')
    if not collection['job_ids']:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='No files uploaded to collection')

    started = 0
    for job_id in collection['job_ids']:
        job = db.get(Job, job_id)
        if job is None:
            continue
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}
        settings['profile_id'] = payload.profile_id
        settings['mode'] = 'collection'
        settings['email'] = collection['email']
        settings['department'] = collection['department']
        settings['collection_id'] = collection_id
        job.processing_info = {**info, 'settings': settings}
        process_job.delay(
            job.id,
            payload.profile_id,
            'collection',
            collection['email'],
            collection['department'],
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
    )
    job.processing_info = _base_processing_info(
        mode='single',
        email=email_clean,
        department=None,
        profile_id=profile_id,
        folder=folder_clean or None,
        subfolder=subfolder_clean or None,
    )
    job.processing_info['settings']['storage_folder'] = storage_folder
    db.add(job)
    _attach_tags(db, job, _parse_tags(tags))
    db.commit()

    process_job.delay(file_id, profile_id, 'single', email_clean, None)
    return UploadResponse(job_id=job.id, status=job.status)


@router.post('/jobs/{job_id}/verify-password')
def verify_job_password(job_id: str, payload: PasswordVerificationRequest, db: Session = Depends(get_db)) -> dict[str, bool]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    
    if not job.password_hash:
        # No password protection, always allowed
        return {'verified': True}
    
    if verify_password(payload.password, job.password_hash):
        return {'verified': True}
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid password')


@router.get('/jobs/{job_id}', response_model=JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    return _job_to_response(job)


@router.get('/jobs', response_model=JobListResponse)
def list_jobs(
    db: Session = Depends(get_db),
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = Query(default=None, alias='status'),
) -> JobListResponse:
    jobs = _job_query(db, q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter)
    return JobListResponse(items=[_job_to_response(job) for job in jobs])


@router.get('/search', response_model=JobSearchResponse)
def search_documents(
    db: Session = Depends(get_db),
    q: str | None = None,
    tag: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    status_filter: JobStatus | None = Query(default=None, alias='status'),
) -> JobSearchResponse:
    jobs = _job_query(db, q=q, tag=tag, from_date=from_date, to_date=to_date, status_filter=status_filter)
    return JobSearchResponse(items=[_job_to_response(job) for job in jobs], total=len(jobs))


@router.post('/jobs/restart-pending')
def restart_pending_jobs(request: Request, db: Session = Depends(get_db)) -> dict[str, int]:
    enforce_rate_limit(request)

    # Keep truly active RUNNING tasks and only requeue excess RUNNING jobs.
    active_process_jobs = _count_active_process_jobs()
    running_jobs = db.scalars(
        select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.updated_at.desc())
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

    pending_jobs = db.scalars(select(Job).where(Job.status == JobStatus.PENDING)).all()
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


@router.get('/stats', response_model=DashboardStatsResponse)
def dashboard_stats(db: Session = Depends(get_db)) -> DashboardStatsResponse:
    processed_documents = db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.FINISHED)) or 0
    failed_documents = db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.FAILED)) or 0
    finished_jobs = db.scalars(select(Job).where(Job.status == JobStatus.FINISHED)).all()
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
def download_markdown(job_id: str, password: str | None = None, db: Session = Depends(get_db)) -> FileResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    if job.status != JobStatus.FINISHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job not finished')

    _check_job_password(job, password)

    result_path = _resolve_markdown_path(job)
    if not result_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Result file not found')

    return FileResponse(result_path, media_type='text/markdown', filename=f'{job_id}.md')


@router.get('/jobs/{job_id}/preview')
def preview_markdown(job_id: str, password: str | None = None, db: Session = Depends(get_db)) -> PlainTextResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Preview not available')
    
    _check_job_password(job, password)
    
    if job.result_markdown:
        return PlainTextResponse(job.result_markdown)
    path = _resolve_markdown_path(job)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Preview not available')
    return PlainTextResponse(path.read_text(encoding='utf-8'))


@router.put('/jobs/{job_id}/save', response_model=JobSaveResponse)
def save_markdown(job_id: str, payload: JobSaveRequest, password: str | None = None, db: Session = Depends(get_db)) -> JobSaveResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
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
    version = int(editor.get('version') or 0) + 1
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    storage_folder = settings_info.get('storage_folder') if isinstance(settings_info.get('storage_folder'), str) else None
    edited_path = build_edited_result_path(storage_folder or job_id, job_id, version)
    edited_path.write_text(payload.markdown, encoding='utf-8')

    now = datetime.now(timezone.utc)
    versions = list(editor.get('versions')) if isinstance(editor.get('versions'), list) else []
    versions.append({'version': version, 'path': str(edited_path), 'updated_at': now.isoformat()})
    info['editor'] = {
        'version': version,
        'latest_result_path': str(edited_path),
        'updated_at': now.isoformat(),
        'versions': versions,
    }
    job.processing_info = {**info}
    job.result_markdown = payload.markdown
    db.commit()

    return JobSaveResponse(
        job_id=job.id,
        version=version,
        path=str(edited_path),
        updated_at=now,
    )


@router.delete('/jobs/{job_id}')
def delete_job(job_id: str, password: str | None = None, db: Session = Depends(get_db)) -> dict[str, str]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')

    _check_job_password(job, password)

    _delete_job_artifacts(job)

    db.delete(job)
    db.commit()
    return {'status': 'deleted'}


@router.get('/health', response_model=HealthResponse)
def healthcheck() -> HealthResponse:
    return HealthResponse(status='healthy')


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
def update_paddle_runtime_settings(payload: PaddleSettingsUpdate) -> PaddleSettingsResponse:
    update_paddle_settings(
        default_profile=payload.default_profile,
        timeout_seconds=payload.timeout_seconds,
    )
    return PaddleSettingsResponse(**get_paddle_settings())


@router.get('/markdown-files', response_model=MarkdownBrowserResponse)
def list_markdown_files() -> MarkdownBrowserResponse:
    root = settings.results_dir.resolve()
    entries: list[MarkdownFileEntry] = []
    if root.exists():
        for path in sorted(root.rglob('*.md')):
            if path.is_file():
                entries.append(_markdown_entry_from_path(path))
    return MarkdownBrowserResponse(items=entries)


@router.get('/markdown-files/{relative_path:path}')
def get_markdown_file(relative_path: str) -> PlainTextResponse:
    root = settings.results_dir.resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid markdown path')
    if not candidate.exists() or not candidate.is_file() or candidate.suffix.lower() != '.md':
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Markdown file not found')
    return PlainTextResponse(candidate.read_text(encoding='utf-8'))


@router.post('/folders', response_model=FolderActionResponse)
def create_folder(payload: FolderActionRequest) -> FolderActionResponse:
    folder_path = '/'.join(filter(None, [_sanitize_storage_path(payload.folder), _sanitize_storage_path(payload.subfolder)]))
    if not folder_path:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder or subfolder required')

    (settings.uploads_dir.resolve() / folder_path).mkdir(parents=True, exist_ok=True)
    (settings.results_dir.resolve() / folder_path).mkdir(parents=True, exist_ok=True)
    return FolderActionResponse(path=folder_path)


@router.get('/folders/{folder_path:path}/download')
def download_folder_markdown(folder_path: str, db: Session = Depends(get_db)) -> StreamingResponse:
    normalized = _sanitize_storage_path(folder_path)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder path required')

    jobs = db.scalars(select(Job).where(Job.status == JobStatus.FINISHED)).all()
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
            try:
                markdown_path = _resolve_markdown_path(job)
            except HTTPException:
                continue
            if not markdown_path.exists():
                continue

            job_folder = _job_folder_path(job)
            relative_folder = job_folder[len(normalized):].lstrip('/') if job_folder.startswith(normalized) else ''
            stem = Path(job.original_filename).stem.strip() or job.id
            archive_name = '/'.join(filter(None, [relative_folder, f'{stem}-{job.id}.md']))
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
def delete_folder(folder_path: str, db: Session = Depends(get_db)) -> FolderActionResponse:
    normalized = _sanitize_storage_path(folder_path)
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Folder path required')

    jobs = db.scalars(select(Job)).all()
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

    shutil.rmtree((settings.uploads_dir.resolve() / normalized), ignore_errors=True)
    shutil.rmtree((settings.results_dir.resolve() / normalized), ignore_errors=True)

    return FolderActionResponse(path=normalized, deleted_jobs=deleted_jobs)
