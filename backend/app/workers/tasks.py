from pathlib import Path
import logging
import uuid
from datetime import datetime, timedelta, timezone

from celery.signals import worker_ready
from redis import Redis
from sqlalchemy import select, update

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import Job, JobStatus
from app.services.paddle_service import (
    convert_to_markdown_with_details,
    get_paddle_settings,
    get_runtime_capability,
    is_paddle_available,
)
from app.services.storage import build_result_path, ensure_storage_dirs
from app.workers.celery_app import celery_app


logger = logging.getLogger(__name__)

_RECOVERY_LOCK_KEY = 'worker:recovery:startup-lock'
_STALE_RUNNING_RETRY_AFTER = timedelta(minutes=2)
_LOWER_PROFILE_RETRY_MAP = {
    'ppocrv6_medium_structurev3': 'ppocrv6_small_structurev3',
    'ppocrv6_small_structurev3': 'ppocrv6_tiny_structurev3',
    'ppocrv6_medium': 'ppocrv6_tiny',
    'ppocrv6_small': 'ppocrv6_tiny',
}


def _resolve_upload_path(job: Job, storage_folder: str | None, job_id: str) -> Path:
    uploads_root = settings.uploads_dir.resolve()
    configured = Path(job.upload_path).resolve()
    suffix = configured.suffix or Path(job.original_filename).suffix or '.pdf'

    if configured.is_relative_to(uploads_root):
        target = configured
    else:
        target = (uploads_root / (storage_folder or 'inbox') / f'{job_id}{suffix}').resolve()

    if target.exists():
        return target

    if job.upload_content is None:
        raise FileNotFoundError(f'Input file not found: {target}')

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(job.upload_content)
    return target


def _resolve_result_path(job: Job, storage_folder: str | None, job_id: str) -> Path:
    results_root = settings.results_dir.resolve()
    if isinstance(job.result_path, str):
        configured = Path(job.result_path).resolve()
        if configured.is_relative_to(results_root):
            target = configured
        else:
            target = build_result_path(storage_folder or 'single', job_id)
    else:
        target = build_result_path(storage_folder or 'single', job_id)

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _normalize_execution_page_count(details: dict, upload_path: Path) -> dict:
    normalized = {**details}
    if isinstance(normalized.get('page_count'), int):
        return normalized

    structure = normalized.get('structure') if isinstance(normalized.get('structure'), dict) else {}
    structure_page_count = structure.get('page_count') if isinstance(structure.get('page_count'), int) else None
    if structure_page_count is not None:
        normalized['page_count'] = structure_page_count
        return normalized

    # For single-file non-PDF uploads (images, office docs converted to one stream),
    # expose at least a stable page_count value for UI consistency.
    if upload_path.suffix.lower() != '.pdf':
        normalized['page_count'] = 1

    return normalized


def _try_acquire_recovery_lock() -> tuple[Redis | None, str | None]:
    """Acquire a short-lived distributed lock for startup recovery."""
    token = str(uuid.uuid4())
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    acquired = client.set(_RECOVERY_LOCK_KEY, token, nx=True, ex=120)
    if acquired:
        return client, token
    return None, None


def _release_recovery_lock(client: Redis | None, token: str | None) -> None:
    if not client or not token:
        return
    try:
        current = client.get(_RECOVERY_LOCK_KEY)
        if current == token:
            client.delete(_RECOVERY_LOCK_KEY)
    except Exception:
        # Lock has an expiry and will self-heal; no hard failure required here.
        pass


def requeue_running_jobs_after_restart() -> int:
    """Requeue jobs that were RUNNING when the worker/container died.

    This makes processing resilient across worker restarts and hard kills.
    """
    db = SessionLocal()
    to_restart: list[tuple[str, str | None, str | None, str | None, str | None]] = []
    try:
        running_jobs = db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all()
        for job in running_jobs:
            info = job.processing_info if isinstance(job.processing_info, dict) else {}
            settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}

            profile_id = settings.get('profile_id') if isinstance(settings.get('profile_id'), str) else None
            mode = settings.get('mode') if isinstance(settings.get('mode'), str) else None
            email = settings.get('email') if isinstance(settings.get('email'), str) else None
            department = settings.get('department') if isinstance(settings.get('department'), str) else None

            execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
            info['execution'] = {
                **execution,
                'status': 'requeued',
                'detail': 'Job was running during worker restart and has been requeued.',
            }

            job.processing_info = {
                **info,
                'settings': settings,
            }
            job.status = JobStatus.PENDING
            job.error_message = None
            to_restart.append((job.id, profile_id, mode, email, department))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    for job_id, profile_id, mode, email, department in to_restart:
        process_job.delay(job_id, profile_id, mode, email, department)

    return len(to_restart)


@worker_ready.connect
def _recover_jobs_on_worker_ready(sender=None, **kwargs) -> None:  # pragma: no cover
    lock_client: Redis | None = None
    lock_token: str | None = None
    try:
        lock_client, lock_token = _try_acquire_recovery_lock()
        if not lock_client:
            logger.info('Skipping startup recovery; another worker instance is handling it.')
            return
        restarted = requeue_running_jobs_after_restart()
        if restarted:
            logger.warning('Recovered %s RUNNING job(s) after worker restart', restarted)
    except Exception as exc:
        logger.exception('Failed to recover RUNNING jobs after worker restart: %s', exc)
    finally:
        _release_recovery_lock(lock_client, lock_token)


@celery_app.task(name='process_job', bind=True, acks_late=True, reject_on_worker_lost=True)
def process_job(
    self,
    job_id: str,
    profile_id: str | None = None,
    mode: str | None = None,
    email: str | None = None,
    department: str | None = None,
) -> None:
    ensure_storage_dirs()
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        # Normal claim path: only PENDING jobs should become RUNNING.
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id)
            .where(Job.status == JobStatus.PENDING)
            .values(
                status=JobStatus.RUNNING,
                error_message=None,
                updated_at=now,
            )
        )

        # Recovery path for acks_late redelivery: if a previous worker died,
        # the job may still be RUNNING in DB. Only reclaim it when stale.
        if not claimed.rowcount:
            stale_cutoff = now - _STALE_RUNNING_RETRY_AFTER
            claimed = db.execute(
                update(Job)
                .where(Job.id == job_id)
                .where(Job.status == JobStatus.RUNNING)
                .where(Job.updated_at < stale_cutoff)
                .values(
                    status=JobStatus.RUNNING,
                    error_message=None,
                    updated_at=now,
                )
            )

        if not claimed.rowcount:
            return

        db.commit()
        job = db.get(Job, job_id)
        if job is None:
            return

        delivery_info = self.request.delivery_info if isinstance(self.request.delivery_info, dict) else {}
        is_redelivered = bool(delivery_info.get('redelivered'))
        effective_profile_id = profile_id

        # Do not auto-downgrade profile after worker-loss redelivery.
        # Mark job failed with guidance so users can explicitly retry with a lower profile.
        if is_redelivered and isinstance(profile_id, str):
            suggested = _LOWER_PROFILE_RETRY_MAP.get(profile_id)
            if suggested and suggested != profile_id:
                warning_detail = (
                    f'Worker-loss redelivery detected for profile {profile_id}. '
                    f'Automatic fallback is disabled. Retry manually with lower profile {suggested}.'
                )
                job.status = JobStatus.FAILED
                job.error_message = warning_detail
                existing = job.processing_info if isinstance(job.processing_info, dict) else {}
                settings = existing.get('settings') if isinstance(existing.get('settings'), dict) else {}
                runtime = existing.get('runtime') if isinstance(existing.get('runtime'), dict) else {}
                job.processing_info = {
                    **existing,
                    'settings': {
                        **settings,
                        'requested_profile_id': profile_id,
                        'profile_id': profile_id,
                    },
                    'runtime': runtime,
                    'execution': {
                        'status': 'failed',
                        'warning': warning_detail,
                        'error': warning_detail,
                        'suggested_profile_id': suggested,
                        'profile_id': profile_id,
                    },
                }
                db.commit()
                logger.warning('Stopping redelivered job %s for manual retry: %s -> %s', job_id, profile_id, suggested)
                return

        runtime = get_paddle_settings()
        capability = get_runtime_capability()
        existing_info = job.processing_info if isinstance(job.processing_info, dict) else {}
        existing_settings = existing_info.get('settings') if isinstance(existing_info.get('settings'), dict) else {}
        execution_payload = {'status': 'running'}
        job.processing_info = {
            'settings': {
                **existing_settings,
                'default_profile': runtime.get('default_profile'),
                'requested_profile_id': profile_id,
                'profile_id': effective_profile_id,
                'timeout_seconds': runtime.get('timeout_seconds'),
                'mode': mode,
                'email': email,
                'department': department,
            },
            'runtime': capability,
            'execution': execution_payload,
        }
        db.commit()

        upload_path = _resolve_upload_path(job, existing_settings.get('storage_folder') if isinstance(existing_settings.get('storage_folder'), str) else None, job_id)
        if str(upload_path) != job.upload_path:
            job.upload_path = str(upload_path)
            db.commit()

        markdown, details = convert_to_markdown_with_details(
            str(upload_path),
            profile_id=effective_profile_id,
            metadata={
                'mode': mode or 'single',
                'email': email or '',
                'department': department or '',
            },
        )
        details = _normalize_execution_page_count(details, upload_path)
        info = job.processing_info if isinstance(job.processing_info, dict) else {}
        settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}
        storage_folder = settings.get('storage_folder') if isinstance(settings.get('storage_folder'), str) else None
        result_path = _resolve_result_path(job, storage_folder, job_id)
        result_path.write_text(markdown, encoding='utf-8')

        job.status = JobStatus.FINISHED
        job.result_path = str(result_path)
        job.result_markdown = markdown
        existing = job.processing_info if isinstance(job.processing_info, dict) else {}
        job.processing_info = {
            **existing,
            'execution': {'status': 'finished', **details},
        }
        job.error_message = None
        db.commit()
    except Exception as exc:  # pragma: no cover
        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)
            existing = job.processing_info if isinstance(job.processing_info, dict) else {}
            job.processing_info = {
                **existing,
                'execution': {'status': 'failed', 'error': str(exc)},
            }
            db.commit()
    finally:
        db.close()


@celery_app.task(name='probe_paddle')
def probe_paddle() -> dict[str, str | None]:
    if is_paddle_available():
        return {'status': 'running', 'detail': None, **get_runtime_capability()}
    return {
        'status': 'stopped',
        'detail': 'PaddleOCR package not available in worker image',
        **get_runtime_capability(),
    }
