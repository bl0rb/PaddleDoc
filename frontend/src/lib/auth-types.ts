/**
 * TypeScript mirrors of the backend auth API schemas
 * (backend/app/api/v1/auth — see /api/v1/auth and /api/v1/auth/admin).
 */

export type UserRole = 'admin' | 'user';

/** UserResponse from the backend. */
export interface AuthUser {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  team_id: string | null;
  is_active: boolean;
  oidc_provider_id: string | null;
  created_at: string;
}

/** TeamResponse from the backend. */
export interface Team {
  id: string;
  name: string;
  created_at: string;
}

/** Public provider entry from GET /api/v1/auth/providers. */
export interface PublicProvider {
  slug: string;
  display_name: string;
}

/** Admin provider entry from /api/v1/auth/admin/providers. */
export interface AdminProvider {
  id: string;
  slug: string;
  display_name: string;
  issuer_url: string;
  client_id: string;
  /** Write-only secret: responses only report whether one is stored. */
  client_secret_set: boolean;
  enabled: boolean;
  scopes: string;
  created_at: string;
}

/** GET /api/v1/auth/setup-status */
export interface SetupStatusResponse {
  needs_setup: boolean;
}

/** POST /api/v1/auth/setup */
export interface SetupRequest {
  username: string;
  email: string;
  password: string;
}

/** POST /api/v1/auth/login */
export interface LoginRequest {
  identifier: string;
  password: string;
}

/** Generic list wrapper used by all list endpoints. */
export interface ListResponse<T> {
  items: T[];
}

/** POST /api/v1/auth/admin/users */
export interface AdminUserCreateRequest {
  username: string;
  email: string;
  /** Omit for OIDC-only accounts. */
  password?: string;
  role: UserRole;
  team_id?: string | null;
  is_active: boolean;
}

/** PATCH /api/v1/auth/admin/users/{id} */
export interface AdminUserUpdateRequest {
  email?: string;
  password?: string;
  role?: UserRole;
  team_id?: string | null;
  /** Explicit team removal (distinct from "leave unchanged"). */
  clear_team?: boolean;
  is_active?: boolean;
}

/** POST/PATCH /api/v1/auth/admin/teams */
export interface TeamRequest {
  name: string;
}

/** POST /api/v1/auth/admin/providers */
export interface ProviderCreateRequest {
  slug: string;
  display_name: string;
  issuer_url: string;
  client_id: string;
  client_secret: string;
  enabled?: boolean;
  scopes?: string;
}

/** PATCH /api/v1/auth/admin/providers/{id} — client_secret is write-only; omit to keep the stored secret. */
export interface ProviderUpdateRequest {
  slug?: string;
  display_name?: string;
  issuer_url?: string;
  client_id?: string;
  client_secret?: string;
  enabled?: boolean;
  scopes?: string;
}

/** POST /api/v1/auth/admin/providers/{id}/test */
export interface ProviderTestResponse {
  ok: boolean;
  detail?: string;
  issuer?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
}

/** POST /api/v1/auth/admin/jobs/claim-ownerless */
export interface ClaimOwnerlessRequest {
  owner_id: string;
}

export interface ClaimOwnerlessResponse {
  claimed: number;
}
