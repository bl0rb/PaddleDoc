"""User-facing schemas for VL connections (read-only) and benchmark runs.
Admin VL connection CRUD schemas live in app/schemas/auth.py next to the
OIDC Provider* schemas they're modeled on; this module is scoped to what
app/api/benchmarks.py actually returns.
"""

import enum
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.models import JobStatus


# --- VL connections (read-only, enabled only) ---------------------------------

class VlConnectionPublic(BaseModel):
    id: str
    name: str
    model: str

    model_config = {'from_attributes': True}


class VlConnectionPublicListResponse(BaseModel):
    items: list[VlConnectionPublic]


# --- Benchmark runs -------------------------------------------------------------

class BenchmarkRunStatus(str, enum.Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'


class BenchmarkVariantResponse(BaseModel):
    job_id: str
    # VL connection name or OCR profile label, snapshotted at creation time.
    label: str
    kind: str  # 'vl' | 'ocr'
    status: JobStatus
    error_message: str | None = None


class BenchmarkRunOwner(BaseModel):
    id: str
    username: str

    model_config = {'from_attributes': True}


class BenchmarkRunSummaryResponse(BaseModel):
    id: str
    original_filename: str
    status: BenchmarkRunStatus
    variant_count: int
    created_at: datetime
    updated_at: datetime
    owner: BenchmarkRunOwner | None = None


class BenchmarkRunListResponse(BaseModel):
    items: list[BenchmarkRunSummaryResponse]


class BenchmarkRunDetailResponse(BaseModel):
    id: str
    original_filename: str
    content_sha256: str
    status: BenchmarkRunStatus
    variant_count: int
    created_at: datetime
    updated_at: datetime
    owner: BenchmarkRunOwner | None = None
    # Ordered exactly as requested: vl_connection_ids first (request order),
    # then the profile_id variant last if present. Same order used
    # consistently by the report and export endpoints.
    variants: list[BenchmarkVariantResponse] = Field(default_factory=list)


class BenchmarkVariantMetrics(BaseModel):
    job_id: str
    label: str
    kind: str  # 'vl' | 'ocr'
    status: JobStatus
    duration_seconds: float | None = None
    page_count: int | None = None
    output_chars: int | None = None
    quality_grade: str | None = None  # 'A' | 'B' | 'C' | None
    used_fallback: bool | None = None
    error: str | None = None


class BenchmarkReportSummary(BaseModel):
    # Winners are picked among FINISHED variants with used_fallback=false
    # only (see _build_benchmark_report) -- a fallback conversion measures
    # the fallback engine, not the variant under test.
    fastest_variant_job_id: str | None = None
    highest_quality_variant_job_id: str | None = None


class BenchmarkReportResponse(BaseModel):
    id: str
    original_filename: str
    status: BenchmarkRunStatus
    # True iff every variant's status is FINISHED or FAILED -- callers
    # should treat the report as final only once this is true.
    all_terminal: bool
    created_at: datetime
    variants: list[BenchmarkVariantMetrics] = Field(default_factory=list)
    summary: BenchmarkReportSummary


class BenchmarkDeleteResponse(BaseModel):
    id: str
    deleted_jobs: int = 0
