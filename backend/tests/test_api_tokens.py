"""FEATURE 4: personal bearer API tokens (create/list/delete, bearer
round-trip auth, expiry, cross-user isolation).

Real cookie-based logins via login_as/create_test_user (same idioms as
test_job_authz.py) to mint tokens, then fresh cookie-less TestClients to
exercise the bearer path in app.api.deps.get_current_user.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.models import ApiToken
from app.services.security import rate_limiter
from conftest import TestingSessionLocal, create_test_user, login_as


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield


def test_create_list_delete_token_roundtrip():
    create_test_user(username='tok-crud-user', email='tok-crud-user@example.com')
    client = login_as('tok-crud-user')

    create_resp = client.post('/api/v1/auth/tokens', json={'name': 'ci-script'})
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created['name'] == 'ci-script'
    assert created['token'].startswith('pd_')
    assert created['token_prefix'] == created['token'][:8]
    assert created['expires_at'] is None
    token_id = created['id']

    list_resp = client.get('/api/v1/auth/tokens')
    assert list_resp.status_code == 200
    items = list_resp.json()['items']
    assert len(items) == 1
    assert items[0]['id'] == token_id
    assert items[0]['token_prefix'] == created['token_prefix']
    assert 'token' not in items[0]  # full value is only ever returned once

    delete_resp = client.delete(f'/api/v1/auth/tokens/{token_id}')
    assert delete_resp.status_code == 204

    after = client.get('/api/v1/auth/tokens').json()['items']
    assert after == []


def test_create_token_with_expiry_sets_expires_at():
    create_test_user(username='tok-expiry-user', email='tok-expiry-user@example.com')
    client = login_as('tok-expiry-user')

    resp = client.post('/api/v1/auth/tokens', json={'name': 'temp', 'expires_in_days': 30})
    assert resp.status_code == 201
    assert resp.json()['expires_at'] is not None


def test_bearer_round_trip_without_cookies():
    create_test_user(username='tok-bearer-user', email='tok-bearer-user@example.com')
    cookie_client = login_as('tok-bearer-user')
    token = cookie_client.post('/api/v1/auth/tokens', json={'name': 'bearer-test'}).json()['token']

    # A brand new TestClient has its own, empty cookie jar -- this exercises
    # the bearer path with no session cookie present at all.
    bare_client = TestClient(app)

    me_resp = bare_client.get('/api/v1/auth/me', headers={'Authorization': f'Bearer {token}'})
    assert me_resp.status_code == 200
    assert me_resp.json()['username'] == 'tok-bearer-user'

    jobs_resp = bare_client.get('/api/v1/jobs', headers={'Authorization': f'Bearer {token}'})
    assert jobs_resp.status_code == 200

    anon_resp = TestClient(app).get('/api/v1/auth/me')
    assert anon_resp.status_code == 401

    bad_token_resp = TestClient(app).get('/api/v1/auth/me', headers={'Authorization': 'Bearer not-a-real-token'})
    assert bad_token_resp.status_code == 401


def test_expired_token_is_rejected():
    create_test_user(username='tok-expired-user', email='tok-expired-user@example.com')
    client = login_as('tok-expired-user')
    created = client.post('/api/v1/auth/tokens', json={'name': 'short-lived', 'expires_in_days': 1}).json()
    token = created['token']
    token_id = created['id']

    # Backdate expires_at directly -- the API only accepts a future
    # expires_in_days, so this is the only way to get an expired row.
    db = TestingSessionLocal()
    try:
        api_token = db.get(ApiToken, token_id)
        api_token.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    resp = TestClient(app).get('/api/v1/auth/me', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 401


def test_deleting_another_users_token_returns_404():
    create_test_user(username='tok-owner-user', email='tok-owner-user@example.com')
    create_test_user(username='tok-intruder-user', email='tok-intruder-user@example.com')
    owner_client = login_as('tok-owner-user')
    intruder_client = login_as('tok-intruder-user')

    token_id = owner_client.post('/api/v1/auth/tokens', json={'name': 'owners-token'}).json()['id']

    resp = intruder_client.delete(f'/api/v1/auth/tokens/{token_id}')
    assert resp.status_code == 404

    # Still present -- the failed cross-user delete must not have removed it.
    items = owner_client.get('/api/v1/auth/tokens').json()['items']
    assert any(item['id'] == token_id for item in items)


# --- bearer tokens cannot manage tokens (a stolen token must not be able to
# mint replacements for itself or delete the owner's other tokens) -----------

def test_bearer_authed_post_tokens_is_forbidden():
    create_test_user(username='tok-bearer-mgmt-user', email='tok-bearer-mgmt-user@example.com')
    cookie_client = login_as('tok-bearer-mgmt-user')
    token = cookie_client.post('/api/v1/auth/tokens', json={'name': 'seed'}).json()['token']

    bare_client = TestClient(app)
    resp = bare_client.post(
        '/api/v1/auth/tokens', json={'name': 'minted-via-bearer'}, headers={'Authorization': f'Bearer {token}'}
    )
    assert resp.status_code == 403
    assert resp.json()['detail'] == 'API tokens cannot manage tokens; use a browser session'

    # Same rejection for GET/DELETE, and case-insensitively on the scheme.
    list_resp = bare_client.get('/api/v1/auth/tokens', headers={'Authorization': f'bearer {token}'})
    assert list_resp.status_code == 403

    delete_resp = bare_client.delete('/api/v1/auth/tokens/does-not-matter', headers={'Authorization': f'Bearer {token}'})
    assert delete_resp.status_code == 403

    # The seed token itself must still be intact -- none of the rejected
    # calls above should have had any side effect.
    items = cookie_client.get('/api/v1/auth/tokens').json()['items']
    assert len(items) == 1


def test_51st_token_returns_409_token_limit_reached():
    create_test_user(username='tok-cap-user', email='tok-cap-user@example.com')
    client = login_as('tok-cap-user')

    for i in range(50):
        resp = client.post('/api/v1/auth/tokens', json={'name': f'token-{i}'})
        assert resp.status_code == 201, resp.text

    over_limit = client.post('/api/v1/auth/tokens', json={'name': 'token-51'})
    assert over_limit.status_code == 409
    assert over_limit.json()['detail'] == 'Token limit reached (50 per user)'

    # The rejected call must not have created a row.
    items = client.get('/api/v1/auth/tokens').json()['items']
    assert len(items) == 50
