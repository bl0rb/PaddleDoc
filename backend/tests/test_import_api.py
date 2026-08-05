"""PR C1 API tests: Confluence-import sources/runs, job artifacts, and the
restart guards for imported jobs.

Uses real cookie-based logins (create_test_user/login_as from conftest.py)
because source ownership and run visibility join against the real users
table, exactly like the Step 3 job-authz tests.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api import import_routes
from app.core.config import settings
from app.models.models import (
    ImportAuthType,
    ImportRun,
    ImportRunStatus,
    ImportSource,
    Job,
    JobArtifact,
    JobStatus,
    Team,
    UserRole,
)
from app.services import security
from app.services.confluence import ConfluenceError
from app.services.security import hash_password, rate_limiter
from conftest import TestingSessionLocal, create_test_user, login_as

# The API encrypts credentials via security.encrypt_import_credential /
# decrypt_import_credential (spec 1.1: Fernet key HKDF-derived from
# SECRET_KEY with info b'import-source-credential'). Those helpers belong to
# the services slice of PR C1; until that slice lands, install a
# byte-compatible stand-in on the module so this file runs either way. The
# derivation is deterministic from SECRET_KEY, so ciphertexts written under
# the stand-in decrypt identically under the real helpers (and vice versa);
# once the real functions exist this block is a no-op and the tests exercise
# them directly.
if not hasattr(security, 'encrypt_import_credential'):  # pragma: no cover - only until the services slice lands
    import base64 as _base64

    from cryptography.fernet import Fernet as _Fernet
    from cryptography.fernet import InvalidToken as _InvalidToken
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _HKDF

    def _import_credential_fernet_key() -> bytes:
        key_material = _HKDF(
            algorithm=_hashes.SHA256(), length=32, salt=None, info=b'import-source-credential'
        ).derive(settings.secret_key.encode('utf-8'))
        return _base64.urlsafe_b64encode(key_material)

    def _encrypt_import_credential(plaintext: str) -> str:
        return _Fernet(_import_credential_fernet_key()).encrypt(plaintext.encode('utf-8')).decode('utf-8')

    def _decrypt_import_credential(ciphertext: str) -> str:
        try:
            return _Fernet(_import_credential_fernet_key()).decrypt(ciphertext.encode('utf-8')).decode('utf-8')
        except _InvalidToken as exc:
            raise ValueError('import credential could not be decrypted') from exc

    security.encrypt_import_credential = _encrypt_import_credential
    security.decrypt_import_credential = _decrypt_import_credential


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield


def _db():
    return TestingSessionLocal()


def _user(prefix: str, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'{prefix}-{suffix}', email=f'{prefix}-{suffix}@example.com', **kwargs)


def _make_team(name_prefix: str) -> str:
    db = _db()
    try:
        team = Team(name=f'{name_prefix}-{uuid.uuid4().hex[:8]}')
        db.add(team)
        db.commit()
        db.refresh(team)
        return team.id
    finally:
        db.close()


def _make_source(
    owner_id: str,
    *,
    credential: str = 'secret-token-123',
    server_kind: str = 'cloud',
    base_url: str = 'https://acme.example.com',
) -> ImportSource:
    db = _db()
    try:
        source = ImportSource(
            owner_id=owner_id,
            name='Test Confluence',
            base_url=base_url,
            server_kind=server_kind,
            api_base_path='/wiki/api/v2' if server_kind == 'cloud' else '',
            auth_type=ImportAuthType.CLOUD_BASIC,
            auth_username='user@example.com',
            credential_encrypted=security.encrypt_import_credential(credential),
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        db.expunge(source)
        return source
    finally:
        db.close()


def _make_run(
    owner_id: str,
    *,
    source_id: str | None = None,
    status: ImportRunStatus = ImportRunStatus.PENDING,
    updated_at: datetime | None = None,
    state: dict | None = None,
) -> ImportRun:
    db = _db()
    try:
        run = ImportRun(
            owner_id=owner_id,
            source_id=source_id,
            scope_type='page',
            scope_value='123456',
            status=status,
            options={'max_pages': 10},
            state=state if state is not None else {'frontier': [], 'visited': {}, 'errors': []},
        )
        if updated_at is not None:
            run.updated_at = updated_at
        db.add(run)
        db.commit()
        db.refresh(run)
        db.expunge(run)
        return run
    finally:
        db.close()


def _make_job(
    *,
    owner_id: str | None,
    filename: str = 'doc.pdf',
    status: JobStatus = JobStatus.FINISHED,
    result_markdown: str | None = None,
    processing_info: dict | None = None,
    password_hash: str | None = None,
    import_run_id: str | None = None,
) -> Job:
    db = _db()
    try:
        job = Job(
            id=str(uuid.uuid4()),
            original_filename=filename,
            upload_path=f'/tmp/{filename}',
            status=status,
            owner_id=owner_id,
            result_markdown=result_markdown,
            processing_info=processing_info,
            password_hash=password_hash,
            import_run_id=import_run_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.expunge(job)
        return job
    finally:
        db.close()


def _make_artifact(
    job_id: str,
    *,
    filename: str = 'diagram.png',
    content_type: str = 'image/png',
    content: bytes = b'\x89PNG\r\n\x1a\nfakepng',
    kind: str = 'image',
) -> JobArtifact:
    db = _db()
    try:
        artifact = JobArtifact(
            job_id=job_id,
            kind=kind,
            filename=filename,
            content_type=content_type,
            content=content,
            size_bytes=len(content),
            sha256='0' * 64,
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        db.expunge(artifact)
        return artifact
    finally:
        db.close()


def _get_source_row(source_id: str) -> ImportSource:
    db = _db()
    try:
        source = db.get(ImportSource, source_id)
        db.expunge(source)
        return source
    finally:
        db.close()


def _get_run_row(run_id: str) -> ImportRun | None:
    db = _db()
    try:
        run = db.get(ImportRun, run_id)
        if run is not None:
            db.expunge(run)
        return run
    finally:
        db.close()


# --- Sources: CRUD + write-only credential ------------------------------------

def test_create_source_credential_write_only_round_trip():
    user = _user('imp-src-create')
    client = login_as(user.username)

    resp = client.post(
        '/api/v1/import/sources',
        json={
            'name': 'ACME Confluence',
            'base_url': 'https://acme.atlassian.net/',
            'auth_type': 'cloud_basic',
            'auth_username': 'me@example.com',
            'credential': 'super-secret-api-token',
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['name'] == 'ACME Confluence'
    assert body['base_url'] == 'https://acme.atlassian.net'  # trailing slash normalized away
    assert body['has_credential'] is True
    # Write-only contract: no credential-shaped key in any response.
    assert 'credential' not in body
    assert 'credential_encrypted' not in body
    assert 'super-secret-api-token' not in resp.text

    row = _get_source_row(body['id'])
    assert row.credential_encrypted != 'super-secret-api-token'
    assert security.decrypt_import_credential(row.credential_encrypted) == 'super-secret-api-token'

    list_resp = client.get('/api/v1/import/sources')
    assert list_resp.status_code == 200
    items = list_resp.json()['items']
    assert [item['id'] for item in items] == [body['id']]
    assert 'super-secret-api-token' not in list_resp.text
    assert 'credential' not in items[0]


def test_create_source_rejects_bad_base_urls():
    user = _user('imp-src-badurl')
    client = login_as(user.username)
    base = {'name': 'x', 'auth_type': 'pat_bearer', 'credential': 'tok'}
    for bad_url in ('ftp://acme.example.com', 'https://user:pw@acme.example.com', 'https://acme.example.com/#frag', 'https://acme.example.com/?q=1', 'not-a-url'):
        resp = client.post('/api/v1/import/sources', json={**base, 'base_url': bad_url})
        assert resp.status_code == 422, bad_url


def test_update_source_credential_semantics_and_detection_reset():
    user = _user('imp-src-patch')
    source = _make_source(user.id, credential='original-token', server_kind='cloud')
    client = login_as(user.username)

    # Name-only patch keeps the credential AND the detection result.
    resp = client.patch(f'/api/v1/import/sources/{source.id}', json={'name': 'Renamed'})
    assert resp.status_code == 200
    row = _get_source_row(source.id)
    assert security.decrypt_import_credential(row.credential_encrypted) == 'original-token'
    assert row.server_kind == 'cloud'

    # Empty credential is "keep the stored one".
    resp = client.patch(f'/api/v1/import/sources/{source.id}', json={'credential': ''})
    assert resp.status_code == 200
    row = _get_source_row(source.id)
    assert security.decrypt_import_credential(row.credential_encrypted) == 'original-token'

    # A new credential replaces it and invalidates the detection result.
    resp = client.patch(f'/api/v1/import/sources/{source.id}', json={'credential': 'rotated-token'})
    assert resp.status_code == 200
    row = _get_source_row(source.id)
    assert security.decrypt_import_credential(row.credential_encrypted) == 'rotated-token'
    assert row.server_kind == ''
    assert row.last_validated_at is None


def test_sources_are_owner_private_even_from_admin_and_teammates():
    team_id = _make_team('imp-src-team')
    owner = _user('imp-src-owner', team_id=team_id)
    teammate = _user('imp-src-teammate', team_id=team_id)
    outsider = _user('imp-src-outsider')
    admin = _user('imp-src-admin', role=UserRole.ADMIN)
    source = _make_source(owner.id)

    for username in (teammate.username, outsider.username, admin.username):
        other_client = login_as(username)
        assert source.id not in {i['id'] for i in other_client.get('/api/v1/import/sources').json()['items']}
        assert other_client.patch(f'/api/v1/import/sources/{source.id}', json={'name': 'x'}).status_code == 404
        assert other_client.delete(f'/api/v1/import/sources/{source.id}').status_code == 404
        assert other_client.post(f'/api/v1/import/sources/{source.id}/test').status_code == 404
        rate_limiter.reset()

    owner_client = login_as(owner.username)
    assert source.id in {i['id'] for i in owner_client.get('/api/v1/import/sources').json()['items']}


def test_delete_source_keeps_runs_with_null_source_id():
    user = _user('imp-src-del')
    source = _make_source(user.id)
    run = _make_run(user.id, source_id=source.id, status=ImportRunStatus.FINISHED)
    client = login_as(user.username)

    resp = client.delete(f'/api/v1/import/sources/{source.id}')
    assert resp.status_code == 200

    row = _get_run_row(run.id)
    assert row is not None
    assert row.source_id is None


# --- Sources: /test probe -----------------------------------------------------

def test_source_test_success_persists_detection_and_cooldown_429(monkeypatch):
    user = _user('imp-test-ok')
    source = _make_source(user.id, server_kind='', credential='probe-secret-tok')
    client = login_as(user.username)

    calls: list[dict] = []

    def fake_detect(base_url, **kwargs):
        calls.append({'base_url': base_url, **kwargs})
        return 'cloud', '/wiki/api/v2'

    monkeypatch.setattr(import_routes, 'detect_server_kind', fake_detect)

    resp = client.post(f'/api/v1/import/sources/{source.id}/test')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {'ok': True, 'detail': 'Connected (Confluence Cloud)', 'server_kind': 'cloud'}
    assert 'probe-secret-tok' not in resp.text
    # The decrypted credential reached the probe, and the allowlist was passed.
    assert calls[0]['credential'] == 'probe-secret-tok'
    assert calls[0]['allowed_private_hosts'] == frozenset(settings.import_private_host_allowlist)

    row = _get_source_row(source.id)
    assert row.server_kind == 'cloud'
    assert row.api_base_path == '/wiki/api/v2'
    assert row.last_validated_at is not None
    assert row.last_test_at is not None

    # DB-backed cooldown: immediate retry is refused regardless of Redis.
    resp = client.post(f'/api/v1/import/sources/{source.id}/test')
    assert resp.status_code == 429
    assert len(calls) == 1  # no second outbound probe

    # After the cooldown window the probe runs again.
    db = _db()
    try:
        row = db.get(ImportSource, source.id)
        row.last_test_at = datetime.now(timezone.utc) - timedelta(seconds=settings.import_test_cooldown_seconds + 1)
        db.commit()
    finally:
        db.close()
    assert client.post(f'/api/v1/import/sources/{source.id}/test').status_code == 200
    assert len(calls) == 2


def test_source_test_cooldown_claim_is_atomic_for_parallel_requests(monkeypatch):
    # TOCTOU guard: the cooldown slot is claimed with one guarded UPDATE
    # BEFORE the outbound probe starts, so a second request arriving while
    # the first probe is still in flight gets 429 without probing -- the
    # Redis-independent outbound-request floor holds under concurrency.
    user = _user('imp-test-race')
    source = _make_source(user.id, server_kind='', credential='probe-secret-tok')
    outer = login_as(user.username)
    inner = login_as(user.username)

    probes: list[str] = []
    nested_status: list[int] = []

    def fake_detect(base_url, **kwargs):
        probes.append(base_url)
        if len(probes) == 1:
            # Simulates the parallel request: it runs while the first
            # request's probe has not returned yet.
            nested_status.append(inner.post(f'/api/v1/import/sources/{source.id}/test').status_code)
        return 'cloud', '/wiki/api/v2'

    monkeypatch.setattr(import_routes, 'detect_server_kind', fake_detect)

    resp = outer.post(f'/api/v1/import/sources/{source.id}/test')
    assert resp.status_code == 200, resp.text
    assert nested_status == [429]
    assert len(probes) == 1  # exactly one outbound probe


def test_source_test_failure_reports_detail_without_credential(monkeypatch):
    user = _user('imp-test-fail')
    source = _make_source(user.id, credential='probe-secret-tok')
    client = login_as(user.username)

    def fake_detect(base_url, **kwargs):
        raise ConfluenceError('authentication failed (HTTP 401) -- check the credential and auth type', status_code=401)

    monkeypatch.setattr(import_routes, 'detect_server_kind', fake_detect)

    resp = client.post(f'/api/v1/import/sources/{source.id}/test')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ok'] is False
    assert 'authentication failed' in body['detail']
    assert body['server_kind'] is None
    assert 'probe-secret-tok' not in resp.text
    # Failed attempts still stamp the cooldown anchor.
    assert _get_source_row(source.id).last_test_at is not None


# --- Runs: creation, caps, scope parsing --------------------------------------

def test_create_run_extracts_page_id_clamps_caps_and_enqueues_by_name(monkeypatch):
    user = _user('imp-run-create')
    source = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)

    sent: list[tuple] = []
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda name, args=None, **kw: sent.append((name, args)))

    resp = client.post(
        '/api/v1/import/runs',
        json={
            'source_id': source.id,
            'scope': {'type': 'page', 'value': 'https://acme.atlassian.net/wiki/spaces/DOC/pages/123456/My+Page'},
            'options': {'max_pages': 999999, 'max_depth': 999, 'tags': ['A', 'a', 'B ']},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['scope_type'] == 'page'
    assert body['scope_value'] == '123456'
    assert body['status'] == 'pending'
    assert body['owner']['id'] == user.id

    assert sent == [('import_confluence', [body['id'], 0])]

    row = _get_run_row(body['id'])
    assert row.options['max_pages'] == settings.import_max_pages  # clamped
    assert row.options['max_depth'] == settings.import_max_depth  # clamped
    assert row.options['tags'] == ['a', 'b']
    assert row.state['frontier'] == [['123456', 0]]
    assert row.state['visited'] == {}


def test_create_run_space_scope_from_url_leaves_frontier_for_worker(monkeypatch):
    user = _user('imp-run-space')
    source = _make_source(user.id, server_kind='datacenter')
    client = login_as(user.username)
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda *a, **k: None)

    resp = client.post(
        '/api/v1/import/runs',
        json={'source_id': source.id, 'scope': {'type': 'space', 'value': 'https://acme.example.com/wiki/spaces/DOC/overview'}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['scope_value'] == 'DOC'
    row = _get_run_row(body['id'])
    assert row.state['frontier'] == []  # worker resolves the space homepage


def test_create_run_rejects_untested_source_and_bad_scope(monkeypatch):
    user = _user('imp-run-reject')
    untested = _make_source(user.id, server_kind='')
    tested = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda *a, **k: None)

    resp = client.post(
        '/api/v1/import/runs',
        json={'source_id': untested.id, 'scope': {'type': 'page', 'value': '123'}},
    )
    assert resp.status_code == 409

    resp = client.post(
        '/api/v1/import/runs',
        json={'source_id': tested.id, 'scope': {'type': 'page', 'value': 'https://acme.example.com/display/KEY/Title'}},
    )
    assert resp.status_code == 422

    # A teammate's source id is a 404, never a credential borrow.
    other = _user('imp-run-otheruser')
    other_source = _make_source(other.id, server_kind='cloud')
    resp = client.post(
        '/api/v1/import/runs',
        json={'source_id': other_source.id, 'scope': {'type': 'page', 'value': '123'}},
    )
    assert resp.status_code == 404


def test_active_run_cap_and_stale_run_reaping(monkeypatch):
    user = _user('imp-run-cap')
    source = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda *a, **k: None)

    payload = {'source_id': source.id, 'scope': {'type': 'page', 'value': '111'}}
    first = client.post('/api/v1/import/runs', json=payload)
    assert first.status_code == 201

    # Cap of 1 active run per user.
    assert client.post('/api/v1/import/runs', json=payload).status_code == 409

    # Cancel frees the slot.
    assert client.post(f"/api/v1/import/runs/{first.json()['id']}/cancel").status_code == 200
    second = client.post('/api/v1/import/runs', json=payload)
    assert second.status_code == 201

    # A stale RUNNING run (worker lost) is reaped to FAILED instead of
    # blocking its owner forever.
    db = _db()
    try:
        row = db.get(ImportRun, second.json()['id'])
        row.status = ImportRunStatus.RUNNING
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_run_seconds + 60)
        db.commit()
    finally:
        db.close()

    third = client.post('/api/v1/import/runs', json=payload)
    assert third.status_code == 201
    reaped = _get_run_row(second.json()['id'])
    assert reaped.status == ImportRunStatus.FAILED
    assert reaped.error_message == 'worker lost; run stalled'


def test_stale_pending_run_is_reaped_and_does_not_block_creation(monkeypatch):
    # A PENDING run whose creation message was lost (send_task 500'd) has no
    # worker heartbeat ever coming; older than the stale window it must be
    # reaped by the cap check instead of locking its owner out forever.
    user = _user('imp-run-stalepending')
    source = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda *a, **k: None)

    phantom = _make_run(
        user.id,
        status=ImportRunStatus.PENDING,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_run_seconds + 60),
    )

    resp = client.post(
        '/api/v1/import/runs', json={'source_id': source.id, 'scope': {'type': 'page', 'value': '123'}}
    )
    assert resp.status_code == 201, resp.text
    reaped = _get_run_row(phantom.id)
    assert reaped.status == ImportRunStatus.FAILED
    assert reaped.error_message == 'worker lost; run stalled'


def test_active_run_cap_recheck_rejects_loser_of_concurrent_create(monkeypatch):
    # TOCTOU guard: a competing run committed between the pre-count and this
    # request's insert (simulated via the options-sanitizer hook, which runs
    # exactly in that window). The post-commit recheck must remove the newer
    # run again, 409, and never enqueue its task.
    user = _user('imp-run-race')
    source = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)

    sent: list[tuple] = []
    monkeypatch.setattr(import_routes.celery_app, 'send_task', lambda name, args=None, **k: sent.append((name, args)))

    competing: dict[str, str] = {}
    real_sanitize = import_routes._sanitize_storage_path

    def sneaky_sanitize(value):
        if 'run_id' not in competing:
            db = _db()
            try:
                other = ImportRun(
                    owner_id=user.id,
                    source_id=source.id,
                    scope_type='page',
                    scope_value='999',
                    status=ImportRunStatus.PENDING,
                    options={'max_pages': 10},
                    state={'frontier': [['999', 0]], 'visited': {}, 'errors': []},
                )
                other.created_at = datetime.now(timezone.utc) - timedelta(seconds=5)
                db.add(other)
                db.commit()
                competing['run_id'] = other.id
            finally:
                db.close()
        return real_sanitize(value)

    monkeypatch.setattr(import_routes, '_sanitize_storage_path', sneaky_sanitize)

    resp = client.post(
        '/api/v1/import/runs', json={'source_id': source.id, 'scope': {'type': 'page', 'value': '123'}}
    )
    assert resp.status_code == 409
    assert sent == []  # the loser's task was never enqueued
    # The earlier (competing) run survived; the loser's row was removed.
    db = _db()
    try:
        active = db.scalars(
            select(ImportRun).where(
                ImportRun.owner_id == user.id,
                ImportRun.status.in_([ImportRunStatus.PENDING, ImportRunStatus.RUNNING]),
            )
        ).all()
        assert [r.id for r in active] == [competing['run_id']]
    finally:
        db.close()


# --- Runs: visibility, cancel, delete -----------------------------------------

def test_run_visibility_and_control_matrix():
    team_id = _make_team('imp-run-team')
    owner = _user('imp-run-owner', team_id=team_id)
    teammate = _user('imp-run-teammate', team_id=team_id)
    outsider = _user('imp-run-outsider')
    admin = _user('imp-run-admin', role=UserRole.ADMIN)
    run = _make_run(owner.id, status=ImportRunStatus.PENDING)

    teammate_client = login_as(teammate.username)
    assert teammate_client.get(f'/api/v1/import/runs/{run.id}').status_code == 200
    assert run.id in {r['id'] for r in teammate_client.get('/api/v1/import/runs').json()['items']}
    # Read is not control: teammates cannot cancel or delete.
    assert teammate_client.post(f'/api/v1/import/runs/{run.id}/cancel').status_code == 403
    assert teammate_client.delete(f'/api/v1/import/runs/{run.id}').status_code == 403

    outsider_client = login_as(outsider.username)
    assert outsider_client.get(f'/api/v1/import/runs/{run.id}').status_code == 404
    assert run.id not in {r['id'] for r in outsider_client.get('/api/v1/import/runs').json()['items']}
    assert outsider_client.post(f'/api/v1/import/runs/{run.id}/cancel').status_code == 404

    admin_client = login_as(admin.username)
    assert admin_client.get(f'/api/v1/import/runs/{run.id}').status_code == 200
    cancel_resp = admin_client.post(f'/api/v1/import/runs/{run.id}/cancel')
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()['status'] == 'cancelled'


def test_run_detail_exposes_progress_errors_and_jobs():
    user = _user('imp-run-detail')
    run = _make_run(
        user.id,
        status=ImportRunStatus.RUNNING,
        state={'frontier': [], 'visited': {'1': 'j1'}, 'errors': [{'page_id': '99', 'title': 'Broken', 'error': 'boom'}]},
    )
    job = _make_job(
        owner_id=user.id,
        filename='imported-page.md',
        import_run_id=run.id,
        processing_info={'settings': {'mode': 'import'}},
    )

    client = login_as(user.username)
    resp = client.get(f'/api/v1/import/runs/{run.id}')
    assert resp.status_code == 200
    body = resp.json()
    assert body['errors'] == [{'page_id': '99', 'title': 'Broken', 'error': 'boom'}]
    assert [j['id'] for j in body['jobs']] == [job.id]
    assert body['jobs'][0]['title'] == 'imported-page.md'
    assert body['cancel_requested'] is False
    assert 'current_page_title' in body


def test_cancel_semantics_pending_running_stale_finished():
    user = _user('imp-run-cancel')
    client = login_as(user.username)

    # pending -> cancelled immediately.
    pending = _make_run(user.id, status=ImportRunStatus.PENDING)
    resp = client.post(f'/api/v1/import/runs/{pending.id}/cancel')
    assert resp.status_code == 200
    assert resp.json() == {'id': pending.id, 'status': 'cancelled', 'cancel_requested': True}
    assert _get_run_row(pending.id).finished_at is not None

    # healthy running -> only cancel_requested; the worker flips the status.
    running = _make_run(user.id, status=ImportRunStatus.RUNNING)
    resp = client.post(f'/api/v1/import/runs/{running.id}/cancel')
    assert resp.status_code == 200
    assert resp.json() == {'id': running.id, 'status': 'running', 'cancel_requested': True}

    # stale running -> force-terminated directly (worker lost).
    stale = _make_run(
        user.id,
        status=ImportRunStatus.RUNNING,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_run_seconds + 60),
    )
    resp = client.post(f'/api/v1/import/runs/{stale.id}/cancel')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'cancelled'
    assert _get_run_row(stale.id).finished_at is not None

    # finished -> 409; cancelled -> idempotent 200.
    finished = _make_run(user.id, status=ImportRunStatus.FINISHED)
    assert client.post(f'/api/v1/import/runs/{finished.id}/cancel').status_code == 409
    assert client.post(f'/api/v1/import/runs/{pending.id}/cancel').status_code == 200


def test_delete_run_default_keeps_jobs_and_nulls_fk():
    user = _user('imp-run-delkeep')
    run = _make_run(user.id, status=ImportRunStatus.FINISHED)
    job = _make_job(owner_id=user.id, import_run_id=run.id, processing_info={'settings': {'mode': 'import'}})
    client = login_as(user.username)

    # Non-terminal runs cannot be deleted.
    active = _make_run(user.id, status=ImportRunStatus.RUNNING)
    assert client.delete(f'/api/v1/import/runs/{active.id}').status_code == 409

    resp = client.delete(f'/api/v1/import/runs/{run.id}')
    assert resp.status_code == 200
    assert resp.json() == {'id': run.id, 'deleted_jobs': 0}

    assert _get_run_row(run.id) is None
    db = _db()
    try:
        surviving = db.get(Job, job.id)
        assert surviving is not None
        # Explicit SQL NULLing (sqlite never enforces the FK cascade).
        assert surviving.import_run_id is None
    finally:
        db.close()


def test_delete_run_with_delete_jobs_removes_jobs_and_artifacts():
    user = _user('imp-run-delall')
    run = _make_run(user.id, status=ImportRunStatus.CANCELLED)
    job = _make_job(owner_id=user.id, import_run_id=run.id, processing_info={'settings': {'mode': 'import'}})
    artifact = _make_artifact(job.id)
    client = login_as(user.username)

    resp = client.delete(f'/api/v1/import/runs/{run.id}?delete_jobs=true')
    assert resp.status_code == 200
    assert resp.json() == {'id': run.id, 'deleted_jobs': 1}

    db = _db()
    try:
        assert db.get(Job, job.id) is None
        assert db.get(JobArtifact, artifact.id) is None
    finally:
        db.close()


# --- Job artifacts ------------------------------------------------------------

def test_artifact_listing_and_content_headers():
    user = _user('imp-art-headers')
    job = _make_job(owner_id=user.id)
    png = _make_artifact(job.id, filename='diagram.png', content_type='image/png', content=b'\x89PNGdata')
    txt = _make_artifact(job.id, filename='notes.txt', content_type='text/plain', content=b'hello', kind='attachment')
    client = login_as(user.username)

    listing = client.get(f'/api/v1/jobs/{job.id}/artifacts')
    assert listing.status_code == 200
    items = listing.json()['items']
    assert [i['id'] for i in items] == [png.id, txt.id]  # ordered by filename
    assert all(set(i) == {'id', 'kind', 'filename', 'content_type', 'size_bytes'} for i in items)

    inline = client.get(f'/api/v1/jobs/{job.id}/artifacts/{png.id}/content')
    assert inline.status_code == 200
    assert inline.content == b'\x89PNGdata'
    assert inline.headers['content-type'] == 'image/png'
    assert inline.headers['x-content-type-options'] == 'nosniff'
    assert inline.headers['content-disposition'].startswith('inline; filename="diagram.png"')
    assert inline.headers['cache-control'] == 'private, max-age=3600'

    download = client.get(f'/api/v1/jobs/{job.id}/artifacts/{txt.id}/content')
    assert download.status_code == 200
    assert download.headers['content-disposition'].startswith('attachment; filename="notes.txt"')


def test_artifact_cross_tenant_idor_returns_404():
    victim = _user('imp-art-victim')
    attacker = _user('imp-art-attacker')
    victim_job = _make_job(owner_id=victim.id)
    victim_artifact = _make_artifact(victim_job.id, content=b'victim-bytes')
    attacker_job = _make_job(owner_id=attacker.id)

    attacker_client = login_as(attacker.username)

    # The IDOR shape: a job the attacker CAN see, plus a foreign artifact id.
    resp = attacker_client.get(f'/api/v1/jobs/{attacker_job.id}/artifacts/{victim_artifact.id}/content')
    assert resp.status_code == 404
    assert b'victim-bytes' not in resp.content

    # And the invisible-job paths stay 404 as well.
    assert attacker_client.get(f'/api/v1/jobs/{victim_job.id}/artifacts').status_code == 404
    assert attacker_client.get(f'/api/v1/jobs/{victim_job.id}/artifacts/{victim_artifact.id}/content').status_code == 404


def test_artifact_endpoints_honor_job_password():
    user = _user('imp-art-pw')
    job = _make_job(owner_id=user.id, password_hash=hash_password('open sesame'))
    artifact = _make_artifact(job.id)
    client = login_as(user.username)

    assert client.get(f'/api/v1/jobs/{job.id}/artifacts').status_code == 401
    assert client.get(f'/api/v1/jobs/{job.id}/artifacts', params={'password': 'wrong'}).status_code == 401
    ok = client.get(f'/api/v1/jobs/{job.id}/artifacts', params={'password': 'open sesame'})
    assert ok.status_code == 200
    assert len(ok.json()['items']) == 1

    content_url = f'/api/v1/jobs/{job.id}/artifacts/{artifact.id}/content'
    assert client.get(content_url).status_code == 401
    assert client.get(content_url, params={'password': 'wrong'}).status_code == 401
    assert client.get(content_url, params={'password': 'open sesame'}).status_code == 200


# --- Restart guards for imported jobs -----------------------------------------

_IMPORT_MARKDOWN = '---\ntitle: Imported\n---\n\n# Imported page\n'


def test_restart_and_retry_reject_import_page_jobs_markdown_survives():
    user = _user('imp-guard-restart')
    job = _make_job(
        owner_id=user.id,
        filename='imported-page.md',
        result_markdown=_IMPORT_MARKDOWN,
        processing_info={'settings': {'mode': 'import', 'profile_id': 'ppocrv6_medium'}},
    )
    client = login_as(user.username)

    resp = client.post(f'/api/v1/jobs/{job.id}/restart')
    assert resp.status_code == 409
    assert 'Imported pages cannot be restarted' in resp.json()['detail']

    resp = client.post(f'/api/v1/jobs/{job.id}/retry-lower-profile')
    assert resp.status_code == 409

    db = _db()
    try:
        row = db.get(Job, job.id)
        # The whole point of the guard: the converted markdown is NOT wiped.
        assert row.result_markdown == _IMPORT_MARKDOWN
        assert row.status == JobStatus.FINISHED
    finally:
        db.close()


def test_restart_folder_skips_import_pages_but_restarts_others(monkeypatch):
    from app.api import routes

    user = _user('imp-guard-folder')
    folder = f'imp-guard-{uuid.uuid4().hex[:8]}'
    import_job = _make_job(
        owner_id=user.id,
        filename='page.md',
        result_markdown=_IMPORT_MARKDOWN,
        processing_info={'settings': {'mode': 'import', 'folder': folder, 'subfolder': ''}},
    )
    normal_job = _make_job(
        owner_id=user.id,
        filename='scan.pdf',
        result_markdown='# ocr result',
        processing_info={'settings': {'mode': 'single', 'folder': folder, 'subfolder': ''}},
    )
    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    client = login_as(user.username)
    resp = client.post(f'/api/v1/folders/{folder}/restart')
    assert resp.status_code == 200
    body = resp.json()
    assert body['restarted_jobs'] == 1
    assert body['skipped_import_jobs'] == 1
    assert [args[0] for args in delayed] == [normal_job.id]

    db = _db()
    try:
        assert db.get(Job, import_job.id).result_markdown == _IMPORT_MARKDOWN
        assert db.get(Job, normal_job.id).result_markdown is None  # actually restarted
    finally:
        db.close()


def test_import_attachment_children_remain_restartable(monkeypatch):
    from app.api import routes

    user = _user('imp-guard-child')
    child = _make_job(
        owner_id=user.id,
        filename='attachment.pdf',
        result_markdown='# previous ocr output',
        processing_info={'settings': {'mode': 'import_attachment', 'profile_id': 'ppocrv6_tiny'}},
    )
    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    client = login_as(user.username)
    resp = client.post(f'/api/v1/jobs/{child.id}/restart')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'queued'
    assert [args[0] for args in delayed] == [child.id]


# --- Kill-switch --------------------------------------------------------------

def test_import_disabled_kill_switch_404s_the_surface(monkeypatch):
    user = _user('imp-killswitch')
    source = _make_source(user.id, server_kind='cloud')
    client = login_as(user.username)

    monkeypatch.setattr(settings, 'import_enabled', False)
    assert client.get('/api/v1/import/sources').status_code == 404
    assert client.get('/api/v1/import/runs').status_code == 404
    assert client.post(f'/api/v1/import/sources/{source.id}/test').status_code == 404
    assert client.post(
        '/api/v1/import/runs', json={'source_id': source.id, 'scope': {'type': 'page', 'value': '123'}}
    ).status_code == 404

    monkeypatch.setattr(settings, 'import_enabled', True)
    assert client.get('/api/v1/import/sources').status_code == 200
