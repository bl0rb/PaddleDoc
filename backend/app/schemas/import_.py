from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.models import ImportAuthType, ImportRunStatus, JobStatus


# --- Sources ------------------------------------------------------------------

class ImportSourceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    auth_type: ImportAuthType
    # Email for cloud_basic; ignored (kept empty) for pat_bearer.
    auth_username: str = Field(default='', max_length=320)
    # Write-only: no response schema in this module has a credential field.
    credential: str = Field(min_length=1, max_length=4096)


class ImportSourceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    auth_type: ImportAuthType | None = None
    auth_username: str | None = Field(default=None, max_length=320)
    # Write-only update: omitted or empty keeps the stored credential.
    credential: str | None = Field(default=None, max_length=4096)


class ImportSourceResponse(BaseModel):
    id: str
    name: str
    base_url: str
    server_kind: str = ''
    auth_type: ImportAuthType
    auth_username: str = ''
    # Never the credential itself -- just whether one is on file.
    has_credential: bool = True
    last_validated_at: datetime | None = None
    created_at: datetime

    model_config = {'from_attributes': True}


class ImportSourceListResponse(BaseModel):
    items: list[ImportSourceResponse]


class ImportSourceTestResponse(BaseModel):
    ok: bool
    detail: str | None = None
    server_kind: str | None = None


# --- Runs ---------------------------------------------------------------------

class ImportRunScope(BaseModel):
    type: Literal['space', 'page']
    # Space key, numeric page id, or a pasted Confluence page URL (the server
    # extracts the pageId before persisting, so this may exceed the stored
    # scope_value's 512 chars).
    value: str = Field(min_length=1, max_length=2048)


class ImportRunOptions(BaseModel):
    # None = server default; the server clamps to import_max_pages /
    # import_max_depth regardless of the requested value.
    max_pages: int | None = Field(default=None, ge=1)
    max_depth: int | None = Field(default=None, ge=0)
    include_attachments: bool = True
    ocr_attachments: bool = False
    ocr_profile_id: str | None = None
    folder: str = ''
    subfolder: str = ''
    tags: list[str] = Field(default_factory=list)
    email: str = ''


class ImportRunCreateRequest(BaseModel):
    source_id: str = Field(min_length=1)
    scope: ImportRunScope
    options: ImportRunOptions = Field(default_factory=ImportRunOptions)


class ImportRunOwner(BaseModel):
    id: str
    username: str

    model_config = {'from_attributes': True}


class ImportRunResponse(BaseModel):
    id: str
    kind: str
    status: ImportRunStatus
    scope_type: str
    scope_value: str
    root_page_title: str = ''
    pages_discovered: int = 0
    pages_imported: int = 0
    pages_failed: int = 0
    attachments_saved: int = 0
    artifact_bytes: int = 0
    content_bytes: int = 0
    created_at: datetime
    # Refreshed at least once per imported page by the worker -- doubles as
    # the staleness heartbeat the frontend/API read.
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    owner: ImportRunOwner | None = None

    model_config = {'from_attributes': True}


class ImportRunListResponse(BaseModel):
    items: list[ImportRunResponse]


class ImportRunError(BaseModel):
    page_id: str
    title: str = ''
    error: str


class ImportRunJobSummary(BaseModel):
    id: str
    # From Job.original_filename.
    title: str
    status: JobStatus


class ImportRunDetailResponse(ImportRunResponse):
    current_page_title: str = ''
    error_message: str | None = None
    cancel_requested: bool = False
    errors: list[ImportRunError] = Field(default_factory=list)
    jobs: list[ImportRunJobSummary] = Field(default_factory=list)


class ImportRunCancelResponse(BaseModel):
    id: str
    status: ImportRunStatus
    cancel_requested: bool = False


class ImportRunDeleteResponse(BaseModel):
    id: str
    deleted_jobs: int = 0


# --- Job artifacts ------------------------------------------------------------

class JobArtifactResponse(BaseModel):
    id: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int

    model_config = {'from_attributes': True}


class JobArtifactListResponse(BaseModel):
    items: list[JobArtifactResponse]
