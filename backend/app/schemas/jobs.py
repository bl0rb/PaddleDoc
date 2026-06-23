from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.models import JobStatus


class UploadResponse(BaseModel):
    job_id: str
    status: JobStatus


class CollectionCreateRequest(BaseModel):
    email: str = ''
    department: str = ''
    folder: str = ''
    subfolder: str = ''
    password: str = ''


class CollectionResponse(BaseModel):
    collection_id: str
    email: str
    department: str
    folder: str = ''
    subfolder: str = ''
    job_ids: list[str] = Field(default_factory=list)


class CollectionStartRequest(BaseModel):
    profile_id: str = Field(min_length=1)


class CollectionStartResponse(BaseModel):
    collection_id: str
    started_jobs: int
    profile_id: str


class JobSaveRequest(BaseModel):
    markdown: str = Field(min_length=1)


class JobSaveResponse(BaseModel):
    job_id: str
    version: int
    path: str
    updated_at: datetime


class JobResponse(BaseModel):
    id: str
    original_filename: str
    status: JobStatus
    tags: list[str] = Field(default_factory=list)
    error_message: str | None = None
    processing_info: dict | None = None
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    items: list[JobResponse]


class JobSearchResponse(JobListResponse):
    total: int


class DashboardStatsResponse(BaseModel):
    processed_documents: int
    processed_pages: int
    errors: int
    database_size_bytes: int | None = None


class HealthResponse(BaseModel):
    status: str


class RuntimeCapabilityInfo(BaseModel):
    torch_available: bool
    cuda_available: bool
    selected_device: Literal['gpu', 'cuda', 'cpu']
    platform: str
    no_cuda_reason: str | None = None


class ContainerState(BaseModel):
    name: str
    state: Literal['running', 'stopped', 'degraded', 'unknown']
    detail: str | None = None


class PaddleStatusResponse(BaseModel):
    status: Literal['running', 'failed', 'stopped']
    detail: str | None = None
    runtime: RuntimeCapabilityInfo | None = None
    pending_jobs: int = 0
    running_jobs: int = 0
    queue_total: int = 0
    running_workers: int = 0
    worker_nodes: list[str] = Field(default_factory=list)
    containers: list[ContainerState] = Field(default_factory=list)


class PaddleSettingsResponse(BaseModel):
    default_profile: str
    timeout_seconds: int


class PaddleSettingsUpdate(BaseModel):
    default_profile: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)


class PaddleOption(BaseModel):
    value: str
    label: str
    description: str
    text_detection_model_name: str | None = None
    text_recognition_model_name: str | None = None


class PaddleCapabilitiesResponse(BaseModel):
    profiles: list[PaddleOption]


class MarkdownFileEntry(BaseModel):
    path: str
    filename: str
    folder: str
    size_bytes: int
    updated_at: datetime


class MarkdownBrowserResponse(BaseModel):
    items: list[MarkdownFileEntry]


class FolderActionRequest(BaseModel):
    folder: str = ''
    subfolder: str = ''


class FolderActionResponse(BaseModel):
    path: str
    deleted_jobs: int = 0


class PasswordVerificationRequest(BaseModel):
    password: str = Field(min_length=1)
