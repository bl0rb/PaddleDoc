"""Auth enforcement dependencies: session lookup, admin gating, and the CSRF
Origin check. Kept separate from app/api/auth.py (which owns the endpoints
that issue/consume these) so the main job/folder router in app/api/routes.py
can depend on `get_current_user`/`origin_guard` without importing the whole
auth endpoint module.
"""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.session import get_db
from app.models.models import ApiToken
from app.models.models import Session as SessionModel
from app.models.models import User, UserRole
from app.services.security import hash_session_token

SESSION_COOKIE_NAME = 'paddledoc_session'

# Sliding 7d expiry, touched at most once/hour (no DB write on every single
# request); absolute cap of 30d from session creation regardless of activity.
SESSION_TOUCH_THRESHOLD = timedelta(hours=1)
SESSION_SLIDING_WINDOW = timedelta(days=7)
SESSION_ABSOLUTE_CAP = timedelta(days=30)

# Bearer API tokens touch last_used_at at most this often, to bound write
# volume for a token used on every request of a hot script/integration.
API_TOKEN_TOUCH_THRESHOLD = timedelta(seconds=60)

_STATE_CHANGING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _aware_utc(value: datetime) -> datetime:
    """sqlite has no real tz-aware storage: DateTime(timezone=True) columns
    round-trip as naive datetimes on that dialect (every write in this
    codebase uses datetime.now(timezone.utc), so naive values here are
    always implicitly UTC). Attach tzinfo before doing python-side
    arithmetic against datetime.now(timezone.utc), or the subtraction below
    raises TypeError. No-op on postgres, which already returns aware
    datetimes for these columns.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _authenticate_api_token(db: Session, raw_token: str) -> User:
    """sha256 lookup against api_tokens, the bearer counterpart of the
    cookie session lookup below. Raises the exact same 401 'Not
    authenticated' for every failure mode (unknown/expired token, inactive
    user) -- never distinguishes which for the caller, same discipline as
    the cookie path."""
    token_hash = hash_session_token(raw_token)
    api_token = db.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))
    if api_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    now = datetime.now(timezone.utc)
    if api_token.expires_at is not None and _aware_utc(api_token.expires_at) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    user = db.get(User, api_token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    if api_token.last_used_at is None or now - _aware_utc(api_token.last_used_at) > API_TOKEN_TOUCH_THRESHOLD:
        api_token.last_used_at = now
        db.commit()

    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Bearer token -> cookie/session lookup, in that order.

    An `Authorization: Bearer <token>` header, when present, is
    authenticated against api_tokens and the cookie path is skipped
    entirely -- a caller presenting a bearer credential never falls back to
    (or is confused with) a browser session. Absent that header, this is the
    original cookie -> session lookup, with sliding-expiry touch and lazy
    expiry deletion. Raises 401 if there's no session, it's expired, or the
    user behind it has been deactivated -- never distinguishes which for the
    caller.
    """
    auth_header = request.headers.get('authorization') or ''
    scheme, _, bearer_token = auth_header.partition(' ')
    if scheme.lower() == 'bearer' and bearer_token.strip():
        return _authenticate_api_token(db, bearer_token.strip())

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    token_hash = hash_session_token(token)
    session = db.scalar(select(SessionModel).where(SessionModel.token_hash == token_hash))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    now = datetime.now(timezone.utc)
    if _aware_utc(session.expires_at) <= now:
        # Lazy delete: this row is expired and can never become valid again.
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Session expired')

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Not authenticated')

    if now - _aware_utc(session.last_seen_at) > SESSION_TOUCH_THRESHOLD:
        session.last_seen_at = now
        session.expires_at = min(now + SESSION_SLIDING_WINDOW, _aware_utc(session.created_at) + SESSION_ABSOLUTE_CAP)
        db.commit()

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin privileges required')
    return user


def origin_guard(request: Request) -> None:
    """CSRF defense-in-depth on top of SameSite=Lax: reject any
    state-changing request that carries an Origin header not in our
    configured frontend list. Applied to the PUBLIC auth POSTs too
    (login/setup/OIDC) -- not just authenticated routes -- since those are
    exactly what a cross-site form/fetch would try to forge.
    """
    if request.method not in _STATE_CHANGING_METHODS:
        return
    origin = request.headers.get('origin')
    if origin and origin not in settings.cors_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Origin not allowed')
