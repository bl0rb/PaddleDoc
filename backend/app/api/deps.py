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
from app.models.models import Session as SessionModel
from app.models.models import User, UserRole
from app.services.security import hash_session_token

SESSION_COOKIE_NAME = 'paddledoc_session'

# Sliding 7d expiry, touched at most once/hour (no DB write on every single
# request); absolute cap of 30d from session creation regardless of activity.
SESSION_TOUCH_THRESHOLD = timedelta(hours=1)
SESSION_SLIDING_WINDOW = timedelta(days=7)
SESSION_ABSOLUTE_CAP = timedelta(days=30)

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


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Cookie -> session lookup, with sliding-expiry touch and lazy expiry
    deletion. Raises 401 if there's no session, it's expired, or the user
    behind it has been deactivated -- never distinguishes which for the
    caller."""
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
