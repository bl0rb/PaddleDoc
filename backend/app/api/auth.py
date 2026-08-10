"""Auth endpoints: setup, local login/logout, session introspection, public
OIDC provider list + authorize/callback, and the /auth/admin/* management
surface (users, teams, providers, ownerless-job claiming).

Three routers, matching the enforcement architecture in the auth design
doc:
  - `router_public`      -- no session required (setup, login, provider
                             discovery, OIDC redirect dance). Still runs
                             `origin_guard` at the router level so the CSRF
                             check covers these state-changing POSTs too.
  - `router_authenticated` -- requires a valid session (logout, me).
  - `router_admin`        -- requires an admin session, enforced at the
                             router level (not per-endpoint) via
                             `dependencies=[Depends(require_admin)]`.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    SESSION_COOKIE_NAME,
    SESSION_SLIDING_WINDOW,
    get_current_user,
    origin_guard,
    require_admin,
)
from app.core.config import settings
from app.database.session import get_db
from app.models.models import (
    ApiToken,
    AuthProvider,
    Job,
    Session as SessionModel,
    Team,
    User,
    UserRole,
    VlConnection,
    WorkerLogEntry,
)
from app.schemas.auth import (
    AdminUserCreateRequest,
    AdminUserListResponse,
    AdminUserUpdateRequest,
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenListResponse,
    ApiTokenResponse,
    ClaimOwnerlessRequest,
    ClaimOwnerlessResponse,
    LoginRequest,
    ProviderAdminResponse,
    ProviderCreateRequest,
    ProviderListResponse,
    ProviderPublic,
    ProviderTestResponse,
    ProviderUpdateRequest,
    ProvidersPublicResponse,
    SetupRequest,
    SetupStatusResponse,
    TeamCreateRequest,
    TeamListResponse,
    TeamResponse,
    TeamUpdateRequest,
    UserResponse,
    VlConnectionAdminListResponse,
    VlConnectionAdminResponse,
    VlConnectionCreateRequest,
    VlConnectionTestResponse,
    VlConnectionUpdateRequest,
    WorkerLogEntryResponse,
    WorkerLogLevel,
    WorkerLogListResponse,
)
from app.services import paddle_service
from app.services.oidc import OIDCError, exchange_code_for_tokens, fetch_jwks, get_discovery_document, validate_id_token
from app.services.security import (
    decrypt_client_secret,
    decrypt_vl_api_key,
    encrypt_client_secret,
    encrypt_vl_api_key,
    enforce_rate_limit,
    generate_session_token,
    hash_password,
    hash_session_token,
    is_trusted_proxy_peer,
    sign_value,
    unsign_value,
    verify_password,
)

router_public = APIRouter(prefix='/api/v1/auth', tags=['auth'], dependencies=[Depends(origin_guard)])
router_authenticated = APIRouter(
    prefix='/api/v1/auth', tags=['auth'], dependencies=[Depends(get_current_user), Depends(origin_guard)]
)
router_admin = APIRouter(
    prefix='/api/v1/auth/admin', tags=['auth-admin'], dependencies=[Depends(require_admin), Depends(origin_guard)]
)

# Fixed bigint constant (NOT python hash(), which is salted per-process and
# would defeat the whole point of a shared lock key across replicas).
# Arbitrary -- just needs to be the same constant everywhere this runs.
_SETUP_ADVISORY_LOCK_KEY = 918273645

# A fixed, valid bcrypt digest of a random string nobody knows. Used to spend
# the same ~bcrypt time on logins for unknown/OIDC-only accounts as for real
# ones, closing the username-enumeration timing side-channel.
_DUMMY_PASSWORD_HASH = '$2b$12$0dWbhp.Mm4hFrTNrFotMn.2lgDSumJrdRHfgOSWOjj6f/8zo3Wlsu'

_OIDC_STATE_COOKIE = 'paddledoc_oidc_state'
_OIDC_STATE_TTL = timedelta(minutes=10)
_OIDC_STATE_COOKIE_PATH = '/api/v1/auth/oidc'


# --- shared helpers -----------------------------------------------------------

def _is_https_request(request: Request) -> bool:
    # X-Forwarded-Proto is only honoured when the direct TCP peer is a
    # configured trusted proxy -- otherwise a client on a plain-HTTP
    # connection could set the header itself and make us mark the session
    # cookie Secure (same header-trust class as _client_id_from_request).
    peer_ip = request.client.host if request.client and request.client.host else None
    if is_trusted_proxy_peer(peer_ip) and request.headers.get('x-forwarded-proto', '').lower() == 'https':
        return True
    return request.url.scheme == 'https'


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite='lax',
        secure=_is_https_request(request),
        path='/',
        max_age=int(SESSION_SLIDING_WINDOW.total_seconds()),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path='/')


def _create_session(db: Session, request: Request, response: Response, user: User) -> None:
    token = generate_session_token()
    now = datetime.now(timezone.utc)
    session = SessionModel(
        token_hash=hash_session_token(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + SESSION_SLIDING_WINDOW,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get('user-agent'),
    )
    db.add(session)
    db.commit()
    _set_session_cookie(response, token, request)


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        team_id=user.team_id,
        is_active=user.is_active,
        oidc_provider_id=user.oidc_provider_id,
        created_at=user.created_at,
    )


def _team_response(team: Team) -> TeamResponse:
    return TeamResponse(id=team.id, name=team.name, created_at=team.created_at)


def _provider_response(provider: AuthProvider) -> ProviderAdminResponse:
    return ProviderAdminResponse(
        id=provider.id,
        slug=provider.slug,
        display_name=provider.display_name,
        issuer_url=provider.issuer_url,
        client_id=provider.client_id,
        client_secret_set=bool(provider.client_secret_encrypted),
        enabled=provider.enabled,
        scopes=provider.scopes,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


def _count_active_admins(db: Session, *, exclude_user_id: str | None = None) -> int:
    query = select(func.count()).select_from(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    return db.scalar(query) or 0


def _generate_unique_username(db: Session, base: str) -> str:
    cleaned = ''.join(ch for ch in (base or '').lower() if ch.isalnum() or ch in ('.', '-', '_')) or 'user'
    candidate = cleaned
    suffix = 1
    while db.scalar(select(User.id).where(User.username == candidate)) is not None:
        suffix += 1
        candidate = f'{cleaned}{suffix}'
    return candidate


# --- public: setup ------------------------------------------------------------

@router_public.get('/setup-status', response_model=SetupStatusResponse)
def setup_status(db: Session = Depends(get_db)) -> SetupStatusResponse:
    count = db.scalar(select(func.count()).select_from(User)) or 0
    return SetupStatusResponse(needs_setup=count == 0)


@router_public.post('/setup', response_model=UserResponse)
def setup(payload: SetupRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> UserResponse:
    enforce_rate_limit(request)

    # Race guard for concurrent first-run setups: a fixed advisory-lock key
    # (postgres only -- sqlite serializes writes anyway, and a single-writer
    # engine has no concept of this lock, so it's a no-op there) plus an
    # in-transaction recount as the real guard on every dialect.
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': _SETUP_ADVISORY_LOCK_KEY})

    existing = db.scalar(select(func.count()).select_from(User)) or 0
    if existing > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Setup already completed')

    username = payload.username.strip().lower()
    email = payload.email.strip()
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        # Setup is meant to be one-shot; a username collision here can only
        # happen if setup already ran, so report it the same way.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Setup already completed')

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()

    _create_session(db, request, response, user)
    return _user_response(user)


# --- public: local login -------------------------------------------------------

@router_public.post('/login', response_model=UserResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> UserResponse:
    enforce_rate_limit(request)

    identifier = payload.identifier.strip().lower()
    user = db.scalar(select(User).where((User.username == identifier) | (func.lower(User.email) == identifier)))

    # Always perform one bcrypt verification, even for unknown/OIDC-only/
    # inactive accounts, so the response time can't be used to enumerate
    # which identifiers correspond to real, local-password accounts. The
    # dummy hash is a fixed valid bcrypt digest that never matches.
    password_hash = user.password_hash if user is not None and user.password_hash is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(payload.password, password_hash)

    # One generic 401 for every failure mode (unknown identifier, wrong
    # password, inactive account, OIDC-only account with no local password)
    # -- never reveal which one applies.
    if user is None or not user.is_active or user.password_hash is None or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid credentials')

    _create_session(db, request, response, user)
    return _user_response(user)


# --- authenticated: logout / me ------------------------------------------------

@router_authenticated.post('/logout')
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        token_hash = hash_session_token(token)
        session = db.scalar(select(SessionModel).where(SessionModel.token_hash == token_hash))
        if session is not None:
            db.delete(session)
            db.commit()
    _clear_session_cookie(response)
    return {'status': 'logged_out'}


@router_authenticated.get('/me', response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    return _user_response(user)


# --- authenticated: personal API bearer tokens ---------------------------------
#
# Programmatic (non-cookie) access: 'pd_' + 32 bytes of urlsafe randomness,
# stored as sha256(token) (see app/models/models.ApiToken and
# deps.get_current_user's bearer path). The raw value is only ever returned
# here, at creation time -- GET /tokens exposes token_prefix only.

_API_TOKEN_PREFIX = 'pd_'
_API_TOKEN_PREFIX_LEN = 8
_API_TOKEN_MAX_PER_USER = 50


def _api_token_response(token: ApiToken) -> ApiTokenResponse:
    return ApiTokenResponse(
        id=token.id,
        name=token.name,
        token_prefix=token.token_prefix,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
    )


def _reject_bearer_token_management(request: Request) -> None:
    """Token management (create/list/delete) requires a browser session: a
    stolen bearer token must not be able to mint its own replacements or
    delete the owner's other tokens. Detected off the raw header rather than
    how `user` ended up resolved, since that's what actually distinguishes
    the two credential kinds here."""
    auth_header = request.headers.get('authorization') or ''
    if auth_header.lower().startswith('bearer '):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='API tokens cannot manage tokens; use a browser session',
        )


@router_authenticated.post('/tokens', response_model=ApiTokenCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_token(
    payload: ApiTokenCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiTokenCreateResponse:
    enforce_rate_limit(request)
    _reject_bearer_token_management(request)

    existing_count = db.scalar(select(func.count()).select_from(ApiToken).where(ApiToken.user_id == user.id)) or 0
    if existing_count >= _API_TOKEN_MAX_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Token limit reached ({_API_TOKEN_MAX_PER_USER} per user)',
        )

    raw_token = f'{_API_TOKEN_PREFIX}{secrets.token_urlsafe(32)}'
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days is not None
        else None
    )
    token = ApiToken(
        user_id=user.id,
        name=payload.name.strip(),
        token_hash=hash_session_token(raw_token),
        token_prefix=raw_token[:_API_TOKEN_PREFIX_LEN],
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    return ApiTokenCreateResponse(
        id=token.id,
        name=token.name,
        token=raw_token,
        token_prefix=token.token_prefix,
        created_at=token.created_at,
        expires_at=token.expires_at,
    )


@router_authenticated.get('/tokens', response_model=ApiTokenListResponse)
def list_api_tokens(
    request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ApiTokenListResponse:
    enforce_rate_limit(request)
    _reject_bearer_token_management(request)

    tokens = db.scalars(
        select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())
    ).all()
    return ApiTokenListResponse(items=[_api_token_response(token) for token in tokens])


@router_authenticated.delete('/tokens/{token_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_api_token(
    token_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    enforce_rate_limit(request)
    _reject_bearer_token_management(request)

    # (token_id AND user_id) binding lives in the query itself: another
    # user's token_id must 404, never be deletable by guessing/enumerating
    # ids (IDOR), same discipline as the job-artifact content endpoint.
    token = db.scalar(select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id))
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Token not found')
    db.delete(token)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- public: OIDC providers + redirect dance -----------------------------------

@router_public.get('/providers', response_model=ProvidersPublicResponse)
def list_public_providers(db: Session = Depends(get_db)) -> ProvidersPublicResponse:
    providers = db.scalars(select(AuthProvider).where(AuthProvider.enabled.is_(True)).order_by(AuthProvider.slug)).all()
    return ProvidersPublicResponse(items=[ProviderPublic(slug=p.slug, display_name=p.display_name) for p in providers])


@router_public.get('/oidc/{slug}/authorize')
def oidc_authorize(slug: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    enforce_rate_limit(request)

    provider = db.scalar(select(AuthProvider).where(AuthProvider.slug == slug, AuthProvider.enabled.is_(True)))
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Provider not found')

    try:
        discovery = get_discovery_document(provider.issuer_url)
    except OIDCError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    authorization_endpoint = discovery.get('authorization_endpoint')
    if not authorization_endpoint:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail='Provider discovery is missing authorization_endpoint'
        )

    state = generate_token(32)
    nonce = generate_token(32)
    code_verifier = generate_token(64)
    code_challenge = create_s256_code_challenge(code_verifier)
    redirect_uri = f'{settings.public_api_url}/api/v1/auth/oidc/{slug}/callback'

    query = urlencode(
        {
            'response_type': 'code',
            'client_id': provider.client_id,
            'redirect_uri': redirect_uri,
            'scope': provider.scopes,
            'state': state,
            'nonce': nonce,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }
    )

    redirect_response = RedirectResponse(url=f'{authorization_endpoint}?{query}', status_code=status.HTTP_302_FOUND)
    state_payload = json.dumps({'slug': slug, 'state': state, 'nonce': nonce, 'code_verifier': code_verifier})
    redirect_response.set_cookie(
        key=_OIDC_STATE_COOKIE,
        value=sign_value(state_payload),
        httponly=True,
        samesite='lax',
        secure=_is_https_request(request),
        path=_OIDC_STATE_COOKIE_PATH,
        max_age=int(_OIDC_STATE_TTL.total_seconds()),
    )
    return redirect_response


@router_public.get('/oidc/{slug}/callback')
def oidc_callback(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    enforce_rate_limit(request)

    raw_state_cookie = request.cookies.get(_OIDC_STATE_COOKIE)
    if not raw_state_cookie:
        # No state cookie means this request didn't originate from our own
        # /authorize redirect -- the classic IdP-initiated login pattern.
        # Fail closed rather than trying to recover: there's no state/nonce
        # to validate against, so there is nothing safe to do here.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Missing OIDC state (IdP-initiated login is not supported)',
        )

    unsigned = unsign_value(raw_state_cookie)
    if unsigned is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid OIDC state cookie')

    try:
        state_payload = json.loads(unsigned)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid OIDC state cookie')

    if not isinstance(state_payload, dict) or state_payload.get('slug') != slug or not state or state_payload.get('state') != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='OIDC state mismatch')

    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'OIDC provider returned an error: {error}')
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Missing authorization code')

    provider = db.scalar(select(AuthProvider).where(AuthProvider.slug == slug, AuthProvider.enabled.is_(True)))
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Provider not found')

    try:
        discovery = get_discovery_document(provider.issuer_url)
        redirect_uri = f'{settings.public_api_url}/api/v1/auth/oidc/{slug}/callback'
        tokens = exchange_code_for_tokens(
            discovery['token_endpoint'],
            grant_type='authorization_code',
            code=code,
            redirect_uri=redirect_uri,
            client_id=provider.client_id,
            client_secret=decrypt_client_secret(provider.client_secret_encrypted),
            code_verifier=state_payload['code_verifier'],
        )
        id_token = tokens.get('id_token')
        if not id_token:
            raise OIDCError('token response did not include an id_token')
        key_set = fetch_jwks(discovery['jwks_uri'])
        claims = validate_id_token(
            id_token,
            key_set=key_set,
            issuer=discovery.get('issuer', provider.issuer_url),
            audience=provider.client_id,
            nonce=state_payload['nonce'],
        )
    except (OIDCError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f'OIDC login failed: {exc}') from exc

    subject = claims.get('sub')
    if not subject:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='ID token is missing sub claim')

    user = db.scalar(select(User).where(User.oidc_provider_id == provider.id, User.oidc_subject == subject))
    if user is not None:
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Account is inactive')
    else:
        # No silent email auto-link: a brand-new local user is always
        # created for an unrecognized (provider, sub) pair, even if the
        # claimed email matches an existing local account -- linking an
        # OIDC identity onto an existing account is an explicit admin-only
        # action (silent auto-link by email is an account-takeover vector:
        # anyone who controls that email address at the IdP could hijack a
        # pre-existing local account).
        email = claims.get('email') or f'{subject}@{slug}.oidc.invalid'
        username_base = claims.get('preferred_username') or (email.split('@')[0] if '@' in email else subject)
        user = User(
            username=_generate_unique_username(db, username_base),
            email=email,
            password_hash=None,
            role=UserRole.USER,
            team_id=None,
            oidc_provider_id=provider.id,
            oidc_subject=subject,
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Concurrent first-login callback for the same OIDC identity (or
            # an email already taken): fall back to the row that won the race
            # instead of surfacing a 500.
            db.rollback()
            user = db.scalar(
                select(User).where(User.oidc_provider_id == provider.id, User.oidc_subject == subject)
            )
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='OIDC account could not be provisioned (email may already be in use)',
                )
            if not user.is_active:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Account is inactive')

    redirect_target = settings.cors_origins[0] if settings.cors_origins else '/'
    redirect_response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)
    redirect_response.delete_cookie(key=_OIDC_STATE_COOKIE, path=_OIDC_STATE_COOKIE_PATH)
    _create_session(db, request, redirect_response, user)
    return redirect_response


# --- admin: users --------------------------------------------------------------

@router_admin.get('/users', response_model=AdminUserListResponse)
def admin_list_users(db: Session = Depends(get_db)) -> AdminUserListResponse:
    users = db.scalars(select(User).order_by(User.created_at)).all()
    return AdminUserListResponse(items=[_user_response(u) for u in users])


@router_admin.post('/users', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def admin_create_user(payload: AdminUserCreateRequest, db: Session = Depends(get_db)) -> UserResponse:
    username = payload.username.strip().lower()
    email = payload.email.strip()
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Username already exists')
    if db.scalar(select(User.id).where(func.lower(User.email) == email.lower())) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Email already exists')
    if payload.team_id and db.get(Team, payload.team_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Team not found')

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(payload.password) if payload.password else None,
        role=payload.role,
        team_id=payload.team_id,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    return _user_response(user)


@router_admin.patch('/users/{user_id}', response_model=UserResponse)
def admin_update_user(user_id: str, payload: AdminUserUpdateRequest, db: Session = Depends(get_db)) -> UserResponse:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if user.role == UserRole.ADMIN and user.is_active:
        would_demote = payload.role is not None and payload.role != UserRole.ADMIN
        would_deactivate = payload.is_active is False
        if (would_demote or would_deactivate) and _count_active_admins(db, exclude_user_id=user.id) < 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail='Cannot demote or deactivate the last active admin'
            )

    if payload.email is not None:
        email = payload.email.strip()
        conflict = db.scalar(select(User.id).where(func.lower(User.email) == email.lower(), User.id != user.id))
        if conflict is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Email already exists')
        user.email = email
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.role is not None:
        user.role = payload.role
    if payload.clear_team:
        user.team_id = None
    elif payload.team_id is not None:
        if db.get(Team, payload.team_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Team not found')
        user.team_id = payload.team_id
    if payload.is_active is not None:
        user.is_active = payload.is_active

    db.commit()
    return _user_response(user)


@router_admin.delete('/users/{user_id}')
def admin_delete_user(user_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
    if user.role == UserRole.ADMIN and user.is_active and _count_active_admins(db, exclude_user_id=user.id) < 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Cannot delete the last active admin')
    db.delete(user)
    db.commit()
    return {'status': 'deleted'}


# --- admin: teams ----------------------------------------------------------------

@router_admin.get('/teams', response_model=TeamListResponse)
def admin_list_teams(db: Session = Depends(get_db)) -> TeamListResponse:
    teams = db.scalars(select(Team).order_by(Team.name)).all()
    return TeamListResponse(items=[_team_response(t) for t in teams])


@router_admin.post('/teams', response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def admin_create_team(payload: TeamCreateRequest, db: Session = Depends(get_db)) -> TeamResponse:
    name = payload.name.strip()
    if db.scalar(select(Team.id).where(Team.name == name)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Team already exists')
    team = Team(name=name)
    db.add(team)
    db.commit()
    return _team_response(team)


@router_admin.patch('/teams/{team_id}', response_model=TeamResponse)
def admin_update_team(team_id: str, payload: TeamUpdateRequest, db: Session = Depends(get_db)) -> TeamResponse:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Team not found')
    name = payload.name.strip()
    conflict = db.scalar(select(Team.id).where(Team.name == name, Team.id != team.id))
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Team already exists')
    team.name = name
    db.commit()
    return _team_response(team)


@router_admin.delete('/teams/{team_id}')
def admin_delete_team(team_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Team not found')
    db.delete(team)
    db.commit()
    return {'status': 'deleted'}


# --- admin: OIDC providers ---------------------------------------------------------

@router_admin.get('/providers', response_model=ProviderListResponse)
def admin_list_providers(db: Session = Depends(get_db)) -> ProviderListResponse:
    providers = db.scalars(select(AuthProvider).order_by(AuthProvider.slug)).all()
    return ProviderListResponse(items=[_provider_response(p) for p in providers])


@router_admin.post('/providers', response_model=ProviderAdminResponse, status_code=status.HTTP_201_CREATED)
def admin_create_provider(payload: ProviderCreateRequest, db: Session = Depends(get_db)) -> ProviderAdminResponse:
    slug = payload.slug.strip().lower()
    if db.scalar(select(AuthProvider.id).where(AuthProvider.slug == slug)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Provider slug already exists')
    provider = AuthProvider(
        slug=slug,
        display_name=payload.display_name.strip(),
        issuer_url=payload.issuer_url.strip(),
        client_id=payload.client_id.strip(),
        client_secret_encrypted=encrypt_client_secret(payload.client_secret),
        enabled=payload.enabled,
        scopes=payload.scopes.strip() or 'openid profile email',
    )
    db.add(provider)
    db.commit()
    return _provider_response(provider)


@router_admin.patch('/providers/{provider_id}', response_model=ProviderAdminResponse)
def admin_update_provider(
    provider_id: str, payload: ProviderUpdateRequest, db: Session = Depends(get_db)
) -> ProviderAdminResponse:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Provider not found')
    if payload.display_name is not None:
        provider.display_name = payload.display_name.strip()
    if payload.issuer_url is not None:
        provider.issuer_url = payload.issuer_url.strip()
    if payload.client_id is not None:
        provider.client_id = payload.client_id.strip()
    if payload.client_secret is not None:
        provider.client_secret_encrypted = encrypt_client_secret(payload.client_secret)
    if payload.enabled is not None:
        provider.enabled = payload.enabled
    if payload.scopes is not None:
        provider.scopes = payload.scopes.strip() or 'openid profile email'
    db.commit()
    return _provider_response(provider)


@router_admin.delete('/providers/{provider_id}')
def admin_delete_provider(provider_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Provider not found')
    db.delete(provider)
    db.commit()
    return {'status': 'deleted'}


@router_admin.post('/providers/{provider_id}/test', response_model=ProviderTestResponse)
def admin_test_provider(provider_id: str, db: Session = Depends(get_db)) -> ProviderTestResponse:
    provider = db.get(AuthProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Provider not found')
    try:
        discovery = get_discovery_document(provider.issuer_url)
    except OIDCError as exc:
        return ProviderTestResponse(ok=False, detail=str(exc))
    return ProviderTestResponse(
        ok=True,
        issuer=discovery.get('issuer'),
        authorization_endpoint=discovery.get('authorization_endpoint'),
        token_endpoint=discovery.get('token_endpoint'),
    )


# --- admin: VL connections -------------------------------------------------------
#
# Admin-managed OpenAI-compatible vision-API endpoints, usable as benchmark
# variants (see app/api/benchmarks.py, app/models/models.VlConnection).
# Mirrors the OIDC-providers admin block above 1:1, PUT instead of PATCH for
# the update verb per the API contract.

def _normalize_vl_base_url(raw: str) -> str:
    """Same shape rules as import_routes._normalize_base_url (scheme must be
    http/https, host present, no embedded credentials, no query/fragment,
    trailing slash stripped) -- duplicated locally rather than imported from
    import_routes.py to avoid a cross-feature import into this otherwise
    unrelated router module. Deliberately does NOT apply import_routes'
    SSRF host-blocking: admin-entered VL endpoints are expected to be
    internal/private (self-hosted vLLM/Ollama/etc.)."""
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must use http or https')
    if not parts.hostname:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must include a host')
    if parts.username or parts.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not embed credentials'
        )
    if parts.query or parts.fragment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not contain a query or fragment'
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _vl_connection_response(connection: VlConnection) -> VlConnectionAdminResponse:
    return VlConnectionAdminResponse(
        id=connection.id,
        name=connection.name,
        base_url=connection.base_url,
        model=connection.model,
        has_api_key=bool(connection.api_key_encrypted),
        system_prompt=connection.system_prompt,
        enabled=connection.enabled,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


@router_admin.get('/vl-connections', response_model=VlConnectionAdminListResponse)
def admin_list_vl_connections(request: Request, db: Session = Depends(get_db)) -> VlConnectionAdminListResponse:
    enforce_rate_limit(request)
    connections = db.scalars(select(VlConnection).order_by(VlConnection.name)).all()
    return VlConnectionAdminListResponse(items=[_vl_connection_response(c) for c in connections])


@router_admin.post('/vl-connections', response_model=VlConnectionAdminResponse, status_code=status.HTTP_201_CREATED)
def admin_create_vl_connection(
    payload: VlConnectionCreateRequest, request: Request, db: Session = Depends(get_db)
) -> VlConnectionAdminResponse:
    enforce_rate_limit(request)
    connection = VlConnection(
        name=payload.name.strip(),
        base_url=_normalize_vl_base_url(payload.base_url),
        model=payload.model.strip(),
        api_key_encrypted=encrypt_vl_api_key(payload.api_key),
        system_prompt=payload.system_prompt,
        enabled=payload.enabled,
    )
    db.add(connection)
    db.commit()
    return _vl_connection_response(connection)


@router_admin.put('/vl-connections/{connection_id}', response_model=VlConnectionAdminResponse)
def admin_update_vl_connection(
    connection_id: str, payload: VlConnectionUpdateRequest, request: Request, db: Session = Depends(get_db)
) -> VlConnectionAdminResponse:
    enforce_rate_limit(request)
    connection = db.get(VlConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='VL connection not found')
    if payload.name is not None:
        connection.name = payload.name.strip()
    if payload.base_url is not None:
        connection.base_url = _normalize_vl_base_url(payload.base_url)
    if payload.model is not None:
        connection.model = payload.model.strip()
    # Write-only update contract: omitted or null api_key keeps the stored one.
    if payload.api_key:
        connection.api_key_encrypted = encrypt_vl_api_key(payload.api_key)
    if payload.system_prompt is not None:
        connection.system_prompt = payload.system_prompt
    if payload.enabled is not None:
        connection.enabled = payload.enabled
    db.commit()
    return _vl_connection_response(connection)


@router_admin.delete('/vl-connections/{connection_id}')
def admin_delete_vl_connection(connection_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    enforce_rate_limit(request)
    connection = db.get(VlConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='VL connection not found')
    # No cascade needed: Job only holds a loose vl_connection_id string in
    # processing_info.settings, not a FK, so a still-referenced connection
    # simply produces the graceful 'VL connection is no longer available'
    # job failure in app/workers/tasks.py, not an integrity error.
    db.delete(connection)
    db.commit()
    return {'status': 'deleted'}


@router_admin.post('/vl-connections/{connection_id}/test', response_model=VlConnectionTestResponse)
def admin_test_vl_connection(
    connection_id: str, request: Request, db: Session = Depends(get_db)
) -> VlConnectionTestResponse:
    enforce_rate_limit(request)
    connection = db.get(VlConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='VL connection not found')

    try:
        api_key = decrypt_vl_api_key(connection.api_key_encrypted)
    except ValueError:
        return VlConnectionTestResponse(
            ok=False,
            detail='Stored API key could not be decrypted (SECRET_KEY changed?); the key must be re-entered',
            latency_ms=None,
        )

    result = paddle_service.test_vl_connection(
        connection.base_url, connection.model, api_key, connection.system_prompt
    )
    return VlConnectionTestResponse(**result)


# --- admin: legacy job claiming ---------------------------------------------------

@router_admin.post('/jobs/claim-ownerless', response_model=ClaimOwnerlessResponse)
def admin_claim_ownerless_jobs(payload: ClaimOwnerlessRequest, db: Session = Depends(get_db)) -> ClaimOwnerlessResponse:
    target_user = db.get(User, payload.owner_id)
    if target_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
    result = db.execute(update(Job).where(Job.owner_id.is_(None)).values(owner_id=payload.owner_id))
    db.commit()
    return ClaimOwnerlessResponse(claimed=result.rowcount or 0)


# --- admin: worker logs ------------------------------------------------------
#
# Read side of app/workers/log_capture.py's WorkerLogDBHandler, which is the
# only writer (see that module for how/why records land in
# worker_log_entries). `level` is a floor, not an exact match -- e.g.
# level=WARNING returns WARNING, ERROR, and CRITICAL rows.

_WORKER_LOG_LEVEL_ORDER = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
_WORKER_LOG_PAGE_LIMIT_DEFAULT = 200
_WORKER_LOG_PAGE_LIMIT_MAX = 500


def _worker_log_response(entry: WorkerLogEntry) -> WorkerLogEntryResponse:
    return WorkerLogEntryResponse(
        id=entry.id,
        created_at=entry.created_at,
        level=entry.level,
        logger_name=entry.logger_name,
        worker_name=entry.worker_name,
        task_id=entry.task_id,
        task_name=entry.task_name,
        message=entry.message,
        exc_text=entry.exc_text,
    )


@router_admin.get('/worker-logs', response_model=WorkerLogListResponse)
def admin_list_worker_logs(
    db: Session = Depends(get_db),
    level: WorkerLogLevel | None = None,
    worker: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=_WORKER_LOG_PAGE_LIMIT_DEFAULT, ge=1, le=_WORKER_LOG_PAGE_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> WorkerLogListResponse:
    conditions = []
    if level is not None:
        floor_idx = _WORKER_LOG_LEVEL_ORDER.index(level.value)
        conditions.append(WorkerLogEntry.level.in_(_WORKER_LOG_LEVEL_ORDER[floor_idx:]))
    if worker:
        conditions.append(WorkerLogEntry.worker_name == worker)
    if q:
        conditions.append(WorkerLogEntry.message.ilike(f'%{q}%'))
    if since is not None:
        conditions.append(WorkerLogEntry.created_at >= since)
    if until is not None:
        conditions.append(WorkerLogEntry.created_at <= until)

    total = db.scalar(select(func.count(WorkerLogEntry.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(WorkerLogEntry)
        .where(*conditions)
        # id DESC as a tiebreak keeps ordering deterministic across pages
        # when several records share a created_at (same-millisecond writes).
        .order_by(WorkerLogEntry.created_at.desc(), WorkerLogEntry.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return WorkerLogListResponse(items=[_worker_log_response(row) for row in rows], total=total)
