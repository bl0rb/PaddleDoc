"""Per-account login throttling and the CSRF fail-closed path.

The general rate limiter caps requests per client; these tests cover the
second brake added on top of it: a counter per ACCOUNT, held in the database
so it survives (and keeps refusing during) a Redis outage.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.auth import _LOGIN_MAX_FAILED_ATTEMPTS
from app.main import app
from app.models.models import User
from conftest import BROWSER_HEADERS, DEFAULT_TEST_PASSWORD, TestingSessionLocal, create_test_user


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The 60/min limiter would otherwise trip before the lockout does."""
    from app.services.security import rate_limiter
    rate_limiter.reset()
    yield
    rate_limiter.reset()


def _login(client: TestClient, identifier: str, password: str):
    return client.post('/api/v1/auth/login', json={'identifier': identifier, 'password': password})


def _user(username: str) -> User:
    db = TestingSessionLocal()
    try:
        return db.query(User).filter(User.username == username).one()
    finally:
        db.close()


def test_account_locks_after_threshold_and_rejects_the_correct_password():
    create_test_user(username='lockme', email='lockme@example.com')
    client = TestClient(app, headers=BROWSER_HEADERS)

    for _ in range(_LOGIN_MAX_FAILED_ATTEMPTS):
        assert _login(client, 'lockme', 'definitely-wrong').status_code == 401

    # The whole point: even the RIGHT password is refused while locked.
    response = _login(client, 'lockme', DEFAULT_TEST_PASSWORD)
    assert response.status_code == 401
    # ...and the lockout is never announced, or it would answer "does this
    # account exist?" for anyone willing to fail ten times.
    assert response.json()['detail'] == 'Invalid credentials'

    assert _user('lockme').locked_until is not None


def test_lockout_does_not_affect_other_accounts():
    """Deliberately not keyed by IP: behind NAT one member's typos must not
    lock out everybody else."""
    create_test_user(username='noisy', email='noisy@example.com')
    create_test_user(username='quiet', email='quiet@example.com')
    client = TestClient(app, headers=BROWSER_HEADERS)

    for _ in range(_LOGIN_MAX_FAILED_ATTEMPTS):
        _login(client, 'noisy', 'wrong')

    assert _login(client, 'quiet', DEFAULT_TEST_PASSWORD).status_code == 200


def test_successful_login_clears_the_counter():
    create_test_user(username='recovers', email='recovers@example.com')
    client = TestClient(app, headers=BROWSER_HEADERS)

    for _ in range(_LOGIN_MAX_FAILED_ATTEMPTS - 1):
        _login(client, 'recovers', 'wrong')
    assert _user('recovers').failed_login_count == _LOGIN_MAX_FAILED_ATTEMPTS - 1

    assert _login(client, 'recovers', DEFAULT_TEST_PASSWORD).status_code == 200

    user = _user('recovers')
    assert user.failed_login_count == 0
    assert user.last_failed_login_at is None
    assert user.locked_until is None


def test_expired_lockout_lets_the_user_back_in():
    create_test_user(username='waited', email='waited@example.com')
    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.username == 'waited').one()
        user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    client = TestClient(app, headers=BROWSER_HEADERS)
    assert _login(client, 'waited', DEFAULT_TEST_PASSWORD).status_code == 200


def test_unknown_identifier_is_not_counted_anywhere():
    """An unknown name has no row to count on; the per-client rate limiter is
    what covers spraying. This also keeps the response indistinguishable from
    a wrong password for a real account."""
    client = TestClient(app, headers=BROWSER_HEADERS)
    for _ in range(_LOGIN_MAX_FAILED_ATTEMPTS + 2):
        assert _login(client, 'ghost@example.com', 'wrong').status_code == 401


# --- CSRF: fail closed when a session cookie rides along ----------------------

def test_state_changing_request_without_origin_but_with_cookie_is_rejected():
    """Browsers always send Origin on a cross-origin POST. A cookie-carrying
    POST without one did not come from a page of ours."""
    create_test_user(username='csrfy', email='csrfy@example.com')
    client = TestClient(app, headers=BROWSER_HEADERS)
    assert _login(client, 'csrfy', DEFAULT_TEST_PASSWORD).status_code == 200

    response = client.post('/api/v1/auth/logout', headers={'Origin': ''})
    assert response.status_code == 403


def test_bearer_client_without_origin_still_works():
    """A script authenticating with a bearer token sends no cookie and cannot
    be a CSRF victim, so it must not be caught by the same rule."""
    create_test_user(username='scripted', email='scripted@example.com')
    session_client = TestClient(app, headers=BROWSER_HEADERS)
    assert _login(session_client, 'scripted', DEFAULT_TEST_PASSWORD).status_code == 200
    token = session_client.post('/api/v1/auth/tokens', json={'name': 'ci'}).json()['token']

    bare = TestClient(app)  # no Origin header at all
    response = bare.post(
        '/api/v1/collections',
        headers={'Authorization': f'Bearer {token}'},
        json={'name': 'from-a-script'},
    )
    # Anything but 403 proves origin_guard let it through; the endpoint's own
    # outcome is beside the point here.
    assert response.status_code != 403


def test_foreign_referer_is_rejected_when_origin_is_absent():
    create_test_user(username='refer', email='refer@example.com')
    client = TestClient(app, headers=BROWSER_HEADERS)
    assert _login(client, 'refer', DEFAULT_TEST_PASSWORD).status_code == 200

    response = client.post(
        '/api/v1/auth/logout',
        headers={'Origin': '', 'Referer': 'https://attacker.example/x'},
    )
    assert response.status_code == 403
