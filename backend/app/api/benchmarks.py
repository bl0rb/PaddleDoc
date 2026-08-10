"""Benchmark runs: one uploaded document processed by 2-7 variants (VL
connections and/or a single OCR profile) so their output can be compared
side by side, plus the read-only VL-connections list for authenticated
users. Admin VL-connection CRUD lives in app/api/auth.py, next to the
OIDC-provider admin block it's modeled on (see that module for why).

Mirrors app/api/import_routes.py's structure: module docstring, helpers at
top, endpoints below. Registered in app/main.py under the same
get_current_user + origin_guard dependencies as the main job router and the
Confluence import router -- no separate kill-switch.
"""

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.routes import (
    _JOB_BLOB_DEFER_OPTIONS,
    _JOB_DEFER_UPLOAD_CONTENT_ONLY,
    _content_disposition,
    _delete_job_artifacts,
    _owner_visible,
    _storage_folder,
    create_job_from_upload,
    DuplicateUploadError,  # noqa: F401 -- not expected to fire (benchmark_run_id skips the dedup check entirely), kept importable in case a future call site here ever needs it.
)
from app.database.session import get_db
from app.models.models import BenchmarkRun, Job, JobStatus, User, UserRole, VlConnection
from app.schemas.benchmarks import (
    BenchmarkDeleteResponse,
    BenchmarkReportResponse,
    BenchmarkReportSummary,
    BenchmarkRunDetailResponse,
    BenchmarkRunListResponse,
    BenchmarkRunOwner,
    BenchmarkRunStatus,
    BenchmarkRunSummaryResponse,
    BenchmarkVariantMetrics,
    BenchmarkVariantResponse,
    VlConnectionPublic,
    VlConnectionPublicListResponse,
)
from app.services.paddle_service import get_paddle_capabilities
from app.services.security import enforce_rate_limit
from app.workers.tasks import process_job

router = APIRouter(prefix='/api/v1', tags=['benchmarks'])

_TERMINAL_JOB_STATUSES = (JobStatus.FINISHED, JobStatus.FAILED)
# Rank used to pick the highest-quality FINISHED variant in a report/export
# ('A' beats 'B' beats 'C'); see app/services/quality_gate.py for how a
# job's own quality_gate.grade is computed.
_QUALITY_GRADE_RANK = {'A': 3, 'B': 2, 'C': 1}
_MAX_VL_CONNECTIONS_PER_RUN = 6
_MIN_VARIANTS = 2


# --- Shared helpers -----------------------------------------------------------

def _visible_benchmark_filter(user: User):
    # Mirrors import_routes._visible_run_filter / routes._visible_job_filter:
    # own + current-teammates + admin-all; legacy NULL-owner runs (should
    # not occur for new rows) stay admin-only.
    if user.role == UserRole.ADMIN:
        return None
    conditions = [BenchmarkRun.owner_id == user.id]
    if user.team_id is not None:
        teammate_ids = select(User.id).where(User.team_id == user.team_id)
        conditions.append(BenchmarkRun.owner_id.in_(teammate_ids))
    return or_(*conditions)


def _require_visible_benchmark(db: Session, run_id: str, user: User) -> BenchmarkRun:
    run = db.get(BenchmarkRun, run_id)
    if run is None or not _owner_visible(db, run.owner_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Benchmark not found')
    return run


def _require_benchmark_control(run: BenchmarkRun, user: User) -> None:
    # Teammates may read a run (see _visible_benchmark_filter) but not
    # control it -- read != control, same as import_routes._require_run_control.
    if user.role != UserRole.ADMIN and run.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail='Only the run owner or an admin can do this'
        )


def _derive_benchmark_status(job_statuses: list[JobStatus]) -> BenchmarkRunStatus:
    """pending if every variant is still PENDING; completed if every variant
    is terminal and at least one is FINISHED; failed if every variant is
    terminal and none is FINISHED; running otherwise (some RUNNING, or a mix
    of PENDING and terminal mid-serial-processing)."""
    if not job_statuses:
        return BenchmarkRunStatus.PENDING
    if all(status_value == JobStatus.PENDING for status_value in job_statuses):
        return BenchmarkRunStatus.PENDING
    if all(status_value in _TERMINAL_JOB_STATUSES for status_value in job_statuses):
        if any(status_value == JobStatus.FINISHED for status_value in job_statuses):
            return BenchmarkRunStatus.COMPLETED
        return BenchmarkRunStatus.FAILED
    return BenchmarkRunStatus.RUNNING


def _owner_response(run: BenchmarkRun) -> BenchmarkRunOwner | None:
    if run.owner is None:
        return None
    return BenchmarkRunOwner.model_validate(run.owner)


def _variant_from_job(job: Job) -> BenchmarkVariantResponse:
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    label = settings_info.get('variant_label') if isinstance(settings_info.get('variant_label'), str) else ''
    kind = settings_info.get('variant_kind') if isinstance(settings_info.get('variant_kind'), str) else ''
    return BenchmarkVariantResponse(
        job_id=job.id, label=label, kind=kind, status=job.status, error_message=job.error_message
    )


def _benchmark_detail_response(run: BenchmarkRun, jobs: list[Job]) -> BenchmarkRunDetailResponse:
    return BenchmarkRunDetailResponse(
        id=run.id,
        original_filename=run.original_filename,
        content_sha256=run.content_sha256,
        status=_derive_benchmark_status([job.status for job in jobs]),
        variant_count=len(jobs),
        created_at=run.created_at,
        updated_at=run.updated_at,
        owner=_owner_response(run),
        variants=[_variant_from_job(job) for job in jobs],
    )


def _variant_metrics_from_job(job: Job) -> BenchmarkVariantMetrics:
    """Reads job.status/error_message plus job.processing_info.execution
    (duration_seconds, page_count, quality_gate.grade, used_fallback -- see
    app/workers/tasks.py). Metrics fields beyond duration_seconds/error stay
    None for anything that hasn't FINISHED, per the report contract."""
    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    settings_info = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
    label = settings_info.get('variant_label') if isinstance(settings_info.get('variant_label'), str) else ''
    kind = settings_info.get('variant_kind') if isinstance(settings_info.get('variant_kind'), str) else ''

    duration_seconds = execution.get('duration_seconds')
    if not isinstance(duration_seconds, (int, float)):
        duration_seconds = None

    page_count: int | None = None
    output_chars: int | None = None
    quality_grade: str | None = None
    used_fallback: bool | None = None
    error: str | None = None

    if job.status == JobStatus.FINISHED:
        page_count_raw = execution.get('page_count')
        page_count = page_count_raw if isinstance(page_count_raw, int) else None
        quality_gate = execution.get('quality_gate') if isinstance(execution.get('quality_gate'), dict) else {}
        grade_raw = quality_gate.get('grade')
        quality_grade = grade_raw if isinstance(grade_raw, str) else None
        used_fallback = bool(execution.get('used_fallback'))
        output_chars = len(job.result_markdown) if job.result_markdown is not None else None
    elif job.status == JobStatus.FAILED:
        error = job.error_message

    return BenchmarkVariantMetrics(
        job_id=job.id,
        label=label,
        kind=kind,
        status=job.status,
        duration_seconds=duration_seconds,
        page_count=page_count,
        output_chars=output_chars,
        quality_grade=quality_grade,
        used_fallback=used_fallback,
        error=error,
    )


def _build_benchmark_report(run: BenchmarkRun, jobs: list[Job]) -> BenchmarkReportResponse:
    statuses = [job.status for job in jobs]
    all_terminal = bool(jobs) and all(status_value in _TERMINAL_JOB_STATUSES for status_value in statuses)
    variant_metrics = [_variant_metrics_from_job(job) for job in jobs]

    # Ties broken by variant order (creation order, since `jobs` is queried
    # ordered by created_at asc): Python's min()/max() keep the FIRST
    # occurrence on a tie because they only replace the running best on a
    # strict >/< comparison, never on equality.
    finished_pairs = list(zip(jobs, variant_metrics))
    finished_pairs = [pair for pair in finished_pairs if pair[0].status == JobStatus.FINISHED]
    # Fallback finishes never win a summary: a failed VL endpoint on a .pdf
    # upload lands FINISHED with used_fallback=true via paddle_service's
    # pypdf fallback -- in milliseconds, and on a text-layer PDF often with
    # grade A -- so crowning it would grade the fallback engine, not the
    # variant. Such variants still appear in `variants` with their
    # used_fallback flag; they are only excluded as winners.
    contender_pairs = [pair for pair in finished_pairs if not pair[1].used_fallback]

    fastest_variant_job_id: str | None = None
    timed_pairs = [pair for pair in contender_pairs if pair[1].duration_seconds is not None]
    if timed_pairs:
        fastest_variant_job_id = min(timed_pairs, key=lambda pair: pair[1].duration_seconds)[0].id

    highest_quality_variant_job_id: str | None = None
    graded_pairs = [pair for pair in contender_pairs if pair[1].quality_grade in _QUALITY_GRADE_RANK]
    if graded_pairs:
        highest_quality_variant_job_id = max(
            graded_pairs, key=lambda pair: _QUALITY_GRADE_RANK[pair[1].quality_grade]
        )[0].id

    return BenchmarkReportResponse(
        id=run.id,
        original_filename=run.original_filename,
        status=_derive_benchmark_status(statuses),
        all_terminal=all_terminal,
        created_at=run.created_at,
        variants=variant_metrics,
        summary=BenchmarkReportSummary(
            fastest_variant_job_id=fastest_variant_job_id,
            highest_quality_variant_job_id=highest_quality_variant_job_id,
        ),
    )


# --- Endpoints ------------------------------------------------------------------

@router.post('/benchmarks', response_model=BenchmarkRunDetailResponse)
def create_benchmark(
    request: Request,
    file: UploadFile = File(...),
    vl_connection_ids: list[str] = Form(default_factory=list),
    profile_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BenchmarkRunDetailResponse:
    enforce_rate_limit(request)

    cleaned_vl_ids = [raw.strip() for raw in vl_connection_ids if raw and raw.strip()]
    if len(cleaned_vl_ids) != len(set(cleaned_vl_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='vl_connection_ids must not contain duplicates'
        )
    profile_clean = (profile_id or '').strip() or None

    if len(cleaned_vl_ids) > _MAX_VL_CONNECTIONS_PER_RUN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'At most {_MAX_VL_CONNECTIONS_PER_RUN} vl_connection_ids are allowed',
        )
    total_variants = len(cleaned_vl_ids) + (1 if profile_clean else 0)
    if total_variants < _MIN_VARIANTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='At least 2 variants are required (vl_connection_ids + optional profile_id)',
        )
    # total_variants > 7 is unreachable given the vl_connection_ids cap above
    # (6 VL + 1 profile = 7 max), so no separate upper-bound check is needed.

    profile_label = ''
    if profile_clean is not None:
        profiles_by_id = {profile['value']: profile for profile in get_paddle_capabilities()['profiles']}
        if profile_clean not in profiles_by_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Unknown profile_id')
        profile_label = profiles_by_id[profile_clean]['label']

    vl_connections_by_id: dict[str, VlConnection] = {}
    if cleaned_vl_ids:
        rows = db.scalars(
            select(VlConnection).where(VlConnection.id.in_(cleaned_vl_ids), VlConnection.enabled.is_(True))
        ).all()
        vl_connections_by_id = {row.id: row for row in rows}
        for vl_id in cleaned_vl_ids:
            if vl_id not in vl_connections_by_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f'VL connection not found: {vl_id}'
                )

    variant_specs: list[dict[str, str | None]] = []
    for vl_id in cleaned_vl_ids:
        connection = vl_connections_by_id[vl_id]
        variant_specs.append({
            'kind': 'vl',
            'profile_id': 'openai_vision',
            'label': connection.name,
            'vl_connection_id': connection.id,
        })
    if profile_clean is not None:
        variant_specs.append({
            'kind': 'ocr', 'profile_id': profile_clean, 'label': profile_label, 'vl_connection_id': None,
        })

    # Generated up front (rather than relying on the mapped column's
    # flush-time default) so run.id is available below to build each
    # variant's storage_folder/subfolder before the run row is ever
    # flushed/committed.
    run_id = str(uuid.uuid4())
    run = BenchmarkRun(id=run_id, owner_id=user.id, original_filename=file.filename or 'upload', content_sha256='')
    db.add(run)

    created_jobs: list[tuple[Job, str]] = []
    for spec in variant_specs:
        # The shared UploadFile's underlying SpooledTemporaryFile is fully
        # drained by save_upload on each read; every variant after the
        # first would otherwise silently get zero bytes.
        file.file.seek(0)
        # A fresh id per variant/job -- never the VL connection's or
        # profile's own id, which would collide (as a Job primary key)
        # across separate benchmark runs reusing the same connection/profile.
        file_id = str(uuid.uuid4())
        storage_folder = _storage_folder(file_id, folder='benchmarks', subfolder=run.id)
        extra_settings: dict[str, object] = {
            'benchmark_run_id': run.id,
            'variant_kind': spec['kind'],
            'variant_label': spec['label'],
        }
        if spec['vl_connection_id']:
            extra_settings['vl_connection_id'] = spec['vl_connection_id']

        job = create_job_from_upload(
            db,
            file,
            user=user,
            storage_folder=storage_folder,
            mode='benchmark',
            email='',
            department=None,
            profile_id=spec['profile_id'],
            folder='benchmarks',
            subfolder=run.id,
            tags=[],
            extra_settings=extra_settings,
            password_hash=None,
            benchmark_run_id=run.id,
        )
        created_jobs.append((job, str(spec['profile_id'])))

    run.content_sha256 = created_jobs[0][0].content_sha256
    db.commit()

    for job, effective_profile_id in created_jobs:
        process_job.delay(job.id, effective_profile_id, 'benchmark', '', None)

    return _benchmark_detail_response(run, [job for job, _ in created_jobs])


@router.get('/benchmarks', response_model=BenchmarkRunListResponse)
def list_benchmarks(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> BenchmarkRunListResponse:
    enforce_rate_limit(request)

    query = select(BenchmarkRun).order_by(BenchmarkRun.created_at.desc())
    visible_filter = _visible_benchmark_filter(user)
    if visible_filter is not None:
        query = query.where(visible_filter)
    runs = db.scalars(query).all()

    items: list[BenchmarkRunSummaryResponse] = []
    for run in runs:
        # One grouped query per run (not a full Job load) to derive status +
        # variant_count cheaply for a list view.
        rows = db.execute(
            select(Job.status, func.count(Job.id)).where(Job.benchmark_run_id == run.id).group_by(Job.status)
        ).all()
        statuses = [status_value for status_value, count in rows for _ in range(count)]
        items.append(BenchmarkRunSummaryResponse(
            id=run.id,
            original_filename=run.original_filename,
            status=_derive_benchmark_status(statuses),
            variant_count=len(statuses),
            created_at=run.created_at,
            updated_at=run.updated_at,
            owner=_owner_response(run),
        ))
    return BenchmarkRunListResponse(items=items)


@router.get('/benchmarks/{run_id}', response_model=BenchmarkRunDetailResponse)
def get_benchmark(
    run_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> BenchmarkRunDetailResponse:
    enforce_rate_limit(request)
    run = _require_visible_benchmark(db, run_id, user)

    jobs = db.scalars(
        select(Job)
        .where(Job.benchmark_run_id == run.id)
        .order_by(Job.created_at.asc())
        .options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()
    return _benchmark_detail_response(run, jobs)


@router.get('/benchmarks/{run_id}/report', response_model=BenchmarkReportResponse)
def get_benchmark_report(
    run_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> BenchmarkReportResponse:
    enforce_rate_limit(request)
    run = _require_visible_benchmark(db, run_id, user)

    jobs = db.scalars(
        select(Job)
        .where(Job.benchmark_run_id == run.id)
        .order_by(Job.created_at.asc())
        .options(*_JOB_DEFER_UPLOAD_CONTENT_ONLY)
    ).all()
    return _build_benchmark_report(run, jobs)


@router.get('/benchmarks/{run_id}/export.json')
def export_benchmark_json(
    run_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    enforce_rate_limit(request)
    run = _require_visible_benchmark(db, run_id, user)

    jobs = db.scalars(
        select(Job)
        .where(Job.benchmark_run_id == run.id)
        .order_by(Job.created_at.asc())
        .options(*_JOB_DEFER_UPLOAD_CONTENT_ONLY)
    ).all()

    report = _build_benchmark_report(run, jobs)
    variant_labels = {variant.job_id: variant for variant in report.variants}

    stem = Path(run.original_filename).stem.strip() or run.id
    filename = f'{stem}-benchmark-{run.id}.json'

    payload = {
        'schema': 'paddledoc.benchmark-export/1',
        'benchmark': {
            'id': run.id,
            'original_filename': run.original_filename,
            'content_sha256': run.content_sha256,
            'status': report.status.value,
            'created_at': run.created_at.isoformat(),
        },
        'report': report.model_dump(mode='json'),
        'variants': [
            {
                'job_id': job.id,
                'label': variant_labels[job.id].label if job.id in variant_labels else '',
                'kind': variant_labels[job.id].kind if job.id in variant_labels else '',
                'status': job.status.value,
                'markdown': job.result_markdown if job.status == JobStatus.FINISHED else None,
            }
            for job in jobs
        ],
    }

    return JSONResponse(
        content=payload,
        headers={'Content-Disposition': _content_disposition('attachment', filename)},
    )


@router.delete('/benchmarks/{run_id}', response_model=BenchmarkDeleteResponse)
def delete_benchmark(
    run_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> BenchmarkDeleteResponse:
    enforce_rate_limit(request)
    run = _require_visible_benchmark(db, run_id, user)
    _require_benchmark_control(run, user)

    jobs = db.scalars(
        select(Job).where(Job.benchmark_run_id == run.id).options(*_JOB_BLOB_DEFER_OPTIONS)
    ).all()
    deleted_jobs = 0
    for job in jobs:
        # ORM cascade handles markdown_versions/artifacts/tags exactly like
        # DELETE /jobs/{id} does; no status/terminal gate, same as that
        # endpoint -- process_job's existing `job is None: return` guard
        # already makes deleting a RUNNING variant's row safe if the Celery
        # task completes afterward.
        _delete_job_artifacts(job)
        db.delete(job)
        deleted_jobs += 1
    db.delete(run)
    db.commit()
    return BenchmarkDeleteResponse(id=run_id, deleted_jobs=deleted_jobs)


# --- User: VL connections (read-only, enabled only) ------------------------------

@router.get('/vl-connections', response_model=VlConnectionPublicListResponse)
def list_vl_connections(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> VlConnectionPublicListResponse:
    enforce_rate_limit(request)
    rows = db.scalars(
        select(VlConnection).where(VlConnection.enabled.is_(True)).order_by(VlConnection.name)
    ).all()
    return VlConnectionPublicListResponse(items=[VlConnectionPublic.model_validate(row) for row in rows])
