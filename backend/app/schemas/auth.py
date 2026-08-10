import enum
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.models import UserRole


class SetupStatusResponse(BaseModel):
    needs_setup: bool


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    role: UserRole
    team_id: str | None = None
    is_active: bool
    oidc_provider_id: str | None = None
    created_at: datetime

    model_config = {'from_attributes': True}


class ProviderPublic(BaseModel):
    slug: str
    display_name: str


class ProvidersPublicResponse(BaseModel):
    items: list[ProviderPublic]


# --- Admin: users -----------------------------------------------------------

class AdminUserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    email: EmailStr
    # Omit to create an OIDC-only account (no local password login possible).
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: UserRole = UserRole.USER
    team_id: str | None = None
    is_active: bool = True


class AdminUserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: UserRole | None = None
    team_id: str | None = None
    # team_id=None is ambiguous ("unchanged" vs "clear it"); this makes
    # clearing explicit.
    clear_team: bool = False
    is_active: bool | None = None


class AdminUserListResponse(BaseModel):
    items: list[UserResponse]


# --- Admin: teams ------------------------------------------------------------

class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TeamUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TeamResponse(BaseModel):
    id: str
    name: str
    created_at: datetime

    model_config = {'from_attributes': True}


class TeamListResponse(BaseModel):
    items: list[TeamResponse]


# --- Admin: OIDC providers ----------------------------------------------------

class ProviderCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)
    issuer_url: str = Field(min_length=1, max_length=1024)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(min_length=1)
    enabled: bool = False
    scopes: str = 'openid profile email'


class ProviderUpdateRequest(BaseModel):
    display_name: str | None = None
    issuer_url: str | None = None
    client_id: str | None = None
    # Write-only: omit to keep the existing stored secret unchanged.
    client_secret: str | None = None
    enabled: bool | None = None
    scopes: str | None = None


class ProviderAdminResponse(BaseModel):
    id: str
    slug: str
    display_name: str
    issuer_url: str
    client_id: str
    # Never the secret itself -- just whether one is on file.
    client_secret_set: bool
    enabled: bool
    scopes: str
    created_at: datetime
    updated_at: datetime


class ProviderListResponse(BaseModel):
    items: list[ProviderAdminResponse]


class ProviderTestResponse(BaseModel):
    ok: bool
    detail: str | None = None
    issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None


class VlConnectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    model: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=4096)
    system_prompt: str = Field(default='', max_length=8000)
    enabled: bool = True


class VlConnectionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    # Write-only: omit or leave null to keep the existing stored key unchanged.
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    system_prompt: str | None = Field(default=None, max_length=8000)
    enabled: bool | None = None


class VlConnectionAdminResponse(BaseModel):
    id: str
    name: str
    base_url: str
    model: str
    # Never the key itself -- just whether one is on file.
    has_api_key: bool
    system_prompt: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class VlConnectionAdminListResponse(BaseModel):
    items: list[VlConnectionAdminResponse]


class VlConnectionTestResponse(BaseModel):
    ok: bool
    detail: str | None = None
    latency_ms: int | None = None


class ClaimOwnerlessRequest(BaseModel):
    owner_id: str = Field(min_length=1)


class ClaimOwnerlessResponse(BaseModel):
    claimed: int


# --- Admin: worker logs --------------------------------------------------------

class WorkerLogLevel(str, enum.Enum):
    """Query-param floor for GET /auth/admin/worker-logs -- 'WARNING' means
    WARNING and everything more severe (WARNING, ERROR, CRITICAL). Storage
    (WorkerLogEntry.level, see app/workers/log_capture.py) is a plain string
    column, not a DB enum, so a level the capture-side stdlib logging module
    knows about but this list doesn't (a custom level name) is still stored
    -- it just cannot be used as a floor value in this filter.
    """

    DEBUG = 'DEBUG'
    INFO = 'INFO'
    WARNING = 'WARNING'
    ERROR = 'ERROR'
    CRITICAL = 'CRITICAL'


class WorkerLogEntryResponse(BaseModel):
    id: str
    created_at: datetime
    level: str
    logger_name: str
    worker_name: str
    task_id: str | None = None
    task_name: str | None = None
    message: str
    exc_text: str | None = None


class WorkerLogListResponse(BaseModel):
    items: list[WorkerLogEntryResponse]
    total: int


# --- Personal API bearer tokens -----------------------------------------------

class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiTokenCreateResponse(BaseModel):
    """The only response that ever carries the raw token value -- every
    other read of an api_tokens row (GET /tokens) exposes token_prefix
    only."""

    id: str
    name: str
    token: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime | None = None


class ApiTokenResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ApiTokenListResponse(BaseModel):
    items: list[ApiTokenResponse]
