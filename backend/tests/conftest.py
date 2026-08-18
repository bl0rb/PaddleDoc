"""Shared TestClient/DB wiring for the backend test suite.

Centralized here (rather than duplicated per test file, as test_api.py used
to do) because app.dependency_overrides lives on the single `app` singleton
that's shared process-wide across every test module: two files independently
assigning app.dependency_overrides[get_db] would race on which one "wins"
for the whole session, since the assignment happens at import time (before
any test runs) and the last import wins. conftest.py is guaranteed by pytest
to be imported before any test module in this directory, so it's the one
place this can safely be set up once.

Auth is deliberately NOT bypassed here: only get_db is overridden.
test_api.py (pre-auth business-logic tests, written before Step 2) opts
into bypassing app.api.deps.get_current_user via its own autouse fixture;
test_auth_api.py exercises the real cookie-based login flow against this
same client.
"""

import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.database.session import get_db
from app.main import app
from app.models.models import Base, User, UserRole
from app.services import security as security_module
from app.services.security import hash_password

TEST_DB = 'sqlite:///./test.db'
engine = create_engine(TEST_DB, future=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)


# --- Fake Redis for the rate limiter -----------------------------------------
#
# app.services.security.RedisRateLimiter talks to Redis (INCR/EXPIRE) so
# limits hold across replicas in real deployments (see security.py). There's
# no real Redis server in this test environment, so we swap in a tiny
# in-process fake implementing just the handful of redis-py calls the
# limiter makes, once, process-wide, before any test runs -- the same way
# `get_db` is overridden above.
class _FakeRedis:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}

    def _evict_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= time.time():
            self._counts.pop(key, None)
            self._expires_at.pop(key, None)

    def incr(self, key: str) -> int:
        self._evict_if_expired(key)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def expire(self, key: str, seconds: int) -> bool:
        self._expires_at[key] = time.time() + seconds
        return True

    def keys(self, pattern: str) -> list[str]:
        prefix = pattern[:-1] if pattern.endswith('*') else pattern
        return [key for key in self._counts if key.startswith(prefix)]

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if self._counts.pop(key, None) is not None:
                deleted += 1
            self._expires_at.pop(key, None)
        return deleted


security_module._redis_client = _FakeRedis()


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

# Browsers always send Origin on a state-changing request; origin_guard now
# relies on that (a cookie-carrying POST without Origin or Referer is treated
# as cross-site and rejected). The TestClient does not set the header on its
# own, so both clients below carry the configured frontend origin to stay a
# faithful stand-in for a real browser.
BROWSER_HEADERS = {'Origin': settings.cors_origins[0]}

client = TestClient(app, headers=BROWSER_HEADERS)


# --- Step 3 authz test helpers -----------------------------------------------
#
# Shared by any test module that needs a real, DB-backed user (and
# optionally a team) rather than the `get_current_user` dependency-override
# bypass `test_api.py` uses for its pre-existing business-logic tests: row
# visibility (`_visible_job_filter`/`_owner_visible`) joins against the real
# `users` table (owner_id, team_id), so authz tests need actual persisted
# users to exercise that path rather than a stand-in object.

DEFAULT_TEST_PASSWORD = 'TestPassw0rd1'


def create_test_user(
    *,
    username: str,
    email: str,
    password: str | None = DEFAULT_TEST_PASSWORD,
    role: UserRole = UserRole.USER,
    team_id: str | None = None,
    is_active: bool = True,
) -> User:
    """Persist a user directly (bypassing /auth/setup and /auth/admin/users)
    for test setup. Returns a detached instance safe to read after the
    session that created it is closed."""
    db = TestingSessionLocal()
    try:
        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password) if password else None,
            role=role,
            team_id=team_id,
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def login_as(identifier: str, password: str = DEFAULT_TEST_PASSWORD) -> TestClient:
    """A fresh, isolated-cookie-jar TestClient logged in as the given user
    (matched by username or email, same as the real /auth/login endpoint)."""
    authed_client = TestClient(app, headers=BROWSER_HEADERS)
    resp = authed_client.post('/api/v1/auth/login', json={'identifier': identifier, 'password': password})
    assert resp.status_code == 200, resp.text
    return authed_client
