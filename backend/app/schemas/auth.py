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


class ClaimOwnerlessRequest(BaseModel):
    owner_id: str = Field(min_length=1)


class ClaimOwnerlessResponse(BaseModel):
    claimed: int
