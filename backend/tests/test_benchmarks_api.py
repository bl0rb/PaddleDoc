"""VL benchmark API tests: admin VL-connection CRUD, user-facing VL
connections list, benchmark create validation, duplicate-content bypass,
exclusion from jobs list/search/stats/markdown-browser/folder actions +
predecessor-immunity, report/export shape (incl. fallback-variant summary
exclusion), delete cascade, and visibility.

Real cookie-based logins (create_test_user/login_as, same idioms as
test_versioning_api.py/test_import_api.py) because owner/team visibility and
predecessor lookup join against real users/jobs rows.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.models import (
    BenchmarkRun,
    Job,
    JobMarkdownVersion,
    JobStatus,
    Team,
    UserRole,
    VlConnection,
)
from app.services import security
from app.services.security import rate_limiter
from conftest import TestingSessionLocal, create_test_user, login_as


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch, tmp_path):
    from app.api import routes
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    # Benchmark jobs under test never reach a real worker; process_job.delay
    # is a no-op, so jobs stay PENDING unless a test flips status itself.
    # routes.process_job and benchmarks.process_job are the SAME imported
    # Celery task object, so patching .delay here covers both call sites.
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args, **kwargs: None)
    yield


def _db():
    return TestingSessionLocal()


def _user(prefix: str, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'{prefix}-{suffix}', email=f'{prefix}-{suffix}@example.com', **kwargs)


def _make_team(prefix: str) -> str:
    db = _db()
    try:
        team = Team(name=f'{prefix}-{uuid.uuid4().hex[:8]}')
        db.add(team)
        db.commit()
        db.refresh(team)
        return team.id
    finally:
        db.close()


def _make_vl_connection(
    *,
    name: str = 'Test VL Connection',
    model: str = 'vl-model',
    enabled: bool = True,
    base_url: str = 'https://vl.example.com',
    api_key: str = 'super-secret-key',
    system_prompt: str = '',
) -> VlConnection:
    db = _db()
    try:
        connection = VlConnection(
            name=name,
            base_url=base_url,
            model=model,
            api_key_encrypted=security.encrypt_vl_api_key(api_key),
            system_prompt=system_prompt,
            enabled=enabled,
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        db.expunge(connection)
        return connection
    finally:
        db.close()


def _mark_job(
    job_id: str,
    *,
    status: JobStatus,
    result_markdown: str | None = None,
    error_message: str | None = None,
    execution: dict | None = None,
) -> None:
    db = _db()
    try:
        job = db.get(Job, job_id)
        job.status = status
        if result_markdown is not None:
            job.result_markdown = result_markdown
        if error_message is not None:
            job.error_message = error_message
        if execution is not None:
            info = job.processing_info if isinstance(job.processing_info, dict) else {}
            job.processing_info = {**info, 'execution': execution}
        db.commit()
    finally:
        db.close()


def _upload_benchmark(client, *, filename='doc.pdf', content=b'%PDF-benchmark-content', vl_ids=None, profile_id=None):
    data = {}
    if vl_ids is not None:
        data['vl_connection_ids'] = vl_ids
    if profile_id is not None:
        data['profile_id'] = profile_id
    return client.post(
        '/api/v1/benchmarks',
        files={'file': (filename, content, 'application/pdf')},
        data=data,
    )


# --- Admin: VL connections CRUD ------------------------------------------------

def test_admin_can_create_list_and_never_see_api_key():
    admin_user = _user('vladmin', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)

    create_resp = admin.post(
        '/api/v1/auth/admin/vl-connections',
        json={
            'name': 'Internal vLLM',
            'base_url': 'https://vllm.internal.example.com:8000/',
            'model': 'qwen2-vl-7b',
            'api_key': 'sk-internal-abc123',
            'system_prompt': 'Extract markdown.',
            'enabled': True,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert 'api_key' not in body
    assert body['has_api_key'] is True
    assert body['base_url'] == 'https://vllm.internal.example.com:8000'  # trailing slash stripped
    connection_id = body['id']

    list_resp = admin.get('/api/v1/auth/admin/vl-connections')
    assert list_resp.status_code == 200
    items = list_resp.json()['items']
    assert any(item['id'] == connection_id for item in items)
    assert all('api_key' not in item for item in items)


def test_admin_vl_connection_update_keeps_key_on_blank_omit():
    admin_user = _user('vlupd', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)
    connection = _make_vl_connection(name='Original Name', api_key='original-key-value')

    update_resp = admin.put(
        f'/api/v1/auth/admin/vl-connections/{connection.id}',
        json={'name': 'Renamed Connection'},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()['name'] == 'Renamed Connection'
    assert update_resp.json()['has_api_key'] is True

    db = _db()
    try:
        refreshed = db.get(VlConnection, connection.id)
        assert security.decrypt_vl_api_key(refreshed.api_key_encrypted) == 'original-key-value'
    finally:
        db.close()


def test_admin_vl_connection_delete():
    admin_user = _user('vldel', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)
    connection = _make_vl_connection()

    resp = admin.delete(f'/api/v1/auth/admin/vl-connections/{connection.id}')
    assert resp.status_code == 200
    assert resp.json() == {'status': 'deleted'}

    db = _db()
    try:
        assert db.get(VlConnection, connection.id) is None
    finally:
        db.close()


def test_admin_vl_connection_delete_while_referenced_by_pending_job_degrades_gracefully():
    """Deleting a connection referenced by a still-pending benchmark job must
    not error here (Job only holds a loose vl_connection_id string, not a
    FK) -- the job later fails on its own with a graceful message; see
    test_process_job_fails_gracefully_when_vl_connection_missing below."""
    user = _user('vldelrefowner', role=UserRole.USER)
    client = login_as(user.username)
    admin_user = _user('vldelrefadmin', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)

    connection = _make_vl_connection()
    other = _make_vl_connection(name='Second Conn')
    bench = _upload_benchmark(client, vl_ids=[connection.id, other.id])
    assert bench.status_code == 200, bench.text
    job_id = bench.json()['variants'][0]['job_id']

    resp = admin.delete(f'/api/v1/auth/admin/vl-connections/{connection.id}')
    assert resp.status_code == 200

    db = _db()
    try:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.PENDING  # untouched -- no cascade, no integrity error
        info = job.processing_info
        assert info['settings']['vl_connection_id'] == connection.id
    finally:
        db.close()


def test_non_admin_cannot_manage_vl_connections():
    user = _user('vlnonadmin', role=UserRole.USER)
    client = login_as(user.username)

    assert client.get('/api/v1/auth/admin/vl-connections').status_code == 403
    assert client.post(
        '/api/v1/auth/admin/vl-connections',
        json={'name': 'x', 'base_url': 'https://x.example.com', 'model': 'm', 'api_key': 'k'},
    ).status_code == 403


def test_admin_vl_connection_base_url_validation():
    admin_user = _user('vlbadurl', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)

    resp = admin.post(
        '/api/v1/auth/admin/vl-connections',
        json={'name': 'x', 'base_url': 'ftp://bad.example.com', 'model': 'm', 'api_key': 'k'},
    )
    assert resp.status_code == 422


def test_admin_test_vl_connection_mocked(monkeypatch):
    admin_user = _user('vltest', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)
    connection = _make_vl_connection()

    from app.api import auth as auth_module

    monkeypatch.setattr(
        auth_module.paddle_service,
        'test_vl_connection',
        lambda base_url, model, api_key, system_prompt, timeout_seconds=20.0: {
            'ok': True, 'detail': 'Connected', 'latency_ms': 42,
        },
    )

    resp = admin.post(f'/api/v1/auth/admin/vl-connections/{connection.id}/test')
    assert resp.status_code == 200
    assert resp.json() == {'ok': True, 'detail': 'Connected', 'latency_ms': 42}


def test_admin_test_vl_connection_decrypt_failure_returns_ok_false_null_latency(monkeypatch):
    admin_user = _user('vldecrypt', role=UserRole.ADMIN)
    admin = login_as(admin_user.username)
    connection = _make_vl_connection()

    from app.api import auth as auth_module

    monkeypatch.setattr(
        auth_module,
        'decrypt_vl_api_key',
        lambda ciphertext: (_ for _ in ()).throw(ValueError('bad key')),
    )

    resp = admin.post(f'/api/v1/auth/admin/vl-connections/{connection.id}/test')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ok'] is False
    assert body['latency_ms'] is None
    assert 'decrypted' in body['detail']


# --- User: VL connections (read-only, enabled only) ----------------------------

def test_user_vl_connections_only_enabled_and_minimal_shape():
    user = _user('vlpublic', role=UserRole.USER)
    client = login_as(user.username)
    enabled = _make_vl_connection(name='Enabled Conn', model='model-a', enabled=True)
    _make_vl_connection(name='Disabled Conn', model='model-b', enabled=False)

    resp = client.get('/api/v1/vl-connections')
    assert resp.status_code == 200
    items = resp.json()['items']
    ids = {item['id'] for item in items}
    assert enabled.id in ids
    assert all(set(item.keys()) == {'id', 'name', 'model'} for item in items)
    names = [item['name'] for item in items]
    assert 'Disabled Conn' not in names


# --- Benchmark create validation ------------------------------------------------

def test_benchmark_create_zero_variants_returns_422():
    user = _user('bench0', role=UserRole.USER)
    client = login_as(user.username)
    resp = _upload_benchmark(client)
    assert resp.status_code == 422
    assert 'At least 2 variants' in resp.json()['detail']


def test_benchmark_create_one_variant_returns_422():
    user = _user('bench1', role=UserRole.USER)
    client = login_as(user.username)
    resp = _upload_benchmark(client, profile_id='ppocrv6_tiny')
    assert resp.status_code == 422
    assert 'At least 2 variants' in resp.json()['detail']


def test_benchmark_create_seven_vl_connections_returns_422():
    user = _user('bench7', role=UserRole.USER)
    client = login_as(user.username)
    seven_ids = [str(uuid.uuid4()) for _ in range(7)]
    resp = _upload_benchmark(client, vl_ids=seven_ids)
    assert resp.status_code == 422
    assert 'At most 6' in resp.json()['detail']


def test_benchmark_create_duplicate_vl_connection_ids_returns_422():
    user = _user('benchdup', role=UserRole.USER)
    client = login_as(user.username)
    connection = _make_vl_connection()
    resp = _upload_benchmark(client, vl_ids=[connection.id, connection.id])
    assert resp.status_code == 422
    assert 'duplicate' in resp.json()['detail']


def test_benchmark_create_unknown_profile_id_returns_422():
    user = _user('benchprof', role=UserRole.USER)
    client = login_as(user.username)
    connection = _make_vl_connection()
    resp = _upload_benchmark(client, vl_ids=[connection.id], profile_id='not-a-real-profile')
    assert resp.status_code == 422
    assert resp.json()['detail'] == 'Unknown profile_id'


def test_benchmark_create_disabled_vl_connection_returns_404():
    user = _user('benchdisabled', role=UserRole.USER)
    client = login_as(user.username)
    disabled = _make_vl_connection(enabled=False)
    resp = _upload_benchmark(client, vl_ids=[disabled.id], profile_id='ppocrv6_tiny')
    assert resp.status_code == 404
    assert disabled.id in resp.json()['detail']


def test_benchmark_create_unknown_vl_connection_returns_404():
    user = _user('benchunknown', role=UserRole.USER)
    client = login_as(user.username)
    fake_id = str(uuid.uuid4())
    resp = _upload_benchmark(client, vl_ids=[fake_id], profile_id='ppocrv6_tiny')
    assert resp.status_code == 404
    assert fake_id in resp.json()['detail']


# --- Benchmark create success, dedup bypass, predecessor immunity --------------

def test_benchmark_create_succeeds_with_two_vl_variants():
    user = _user('benchok', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Connection One')
    conn2 = _make_vl_connection(name='Connection Two')

    resp = _upload_benchmark(client, filename='report.pdf', vl_ids=[conn1.id, conn2.id])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['variant_count'] == 2
    assert body['status'] == 'pending'
    assert len(body['content_sha256']) == 64
    assert [v['kind'] for v in body['variants']] == ['vl', 'vl']
    assert [v['label'] for v in body['variants']] == ['Connection One', 'Connection Two']
    assert all(v['status'] == 'PENDING' for v in body['variants'])


def test_benchmark_create_mixed_vl_and_ocr_variant_order():
    user = _user('benchorder', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Vision A')
    conn2 = _make_vl_connection(name='Vision B')

    resp = _upload_benchmark(client, vl_ids=[conn1.id, conn2.id], profile_id='ppocrv6_tiny')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['variant_count'] == 3
    # vl_connection_ids first (request order), profile_id variant last.
    assert [v['kind'] for v in body['variants']] == ['vl', 'vl', 'ocr']
    assert body['variants'][2]['label']  # OCR profile label snapshot, non-empty


def test_benchmark_create_bypasses_duplicate_content_dedup():
    user = _user('benchdedup', role=UserRole.USER)
    client = login_as(user.username)

    normal = client.post(
        '/api/v1/upload',
        files={'file': ('dup.pdf', b'%PDF-shared-bytes', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny'},
    )
    assert normal.status_code == 200
    normal_job_id = normal.json()['job_id']

    conn1 = _make_vl_connection(name='Dup Conn One')
    conn2 = _make_vl_connection(name='Dup Conn Two')
    bench = _upload_benchmark(client, filename='dup.pdf', content=b'%PDF-shared-bytes', vl_ids=[conn1.id, conn2.id])
    assert bench.status_code == 200, bench.text
    body = bench.json()
    assert body['variant_count'] == 2

    db = _db()
    try:
        for variant in body['variants']:
            job = db.get(Job, variant['job_id'])
            assert job.document_version == 1
            assert job.previous_job_id is None
            assert job.benchmark_run_id == body['id']
        normal_job = db.get(Job, normal_job_id)
        assert normal_job.document_version == 1
        assert normal_job.benchmark_run_id is None
    finally:
        db.close()


def test_benchmark_children_never_become_predecessors_for_later_normal_uploads():
    user = _user('benchpred', role=UserRole.USER)
    client = login_as(user.username)

    v1 = client.post(
        '/api/v1/upload',
        files={'file': ('chain.pdf', b'%PDF-chain-v1', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny'},
    )
    assert v1.status_code == 200
    v1_id = v1.json()['job_id']

    # A benchmark run using the SAME filename+bytes as v1 creates children
    # that are newer (by created_at) than v1 but must be invisible to
    # predecessor lookup.
    conn1 = _make_vl_connection(name='Pred Conn One')
    conn2 = _make_vl_connection(name='Pred Conn Two')
    bench = _upload_benchmark(client, filename='chain.pdf', content=b'%PDF-chain-v1', vl_ids=[conn1.id, conn2.id])
    assert bench.status_code == 200, bench.text

    v2 = client.post(
        '/api/v1/upload',
        files={'file': ('chain.pdf', b'%PDF-chain-v2-modified', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny'},
    )
    assert v2.status_code == 200
    v2_body = v2.json()
    v2_detail = client.get(f"/api/v1/jobs/{v2_body['job_id']}").json()
    assert v2_detail['document_version'] == 2
    assert v2_detail['previous_job_id'] == v1_id


# --- Jobs-list exclusion ---------------------------------------------------------

def test_benchmark_children_excluded_from_jobs_list_and_search_but_fetchable_by_id():
    user = _user('benchlist', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='List Conn One')
    conn2 = _make_vl_connection(name='List Conn Two')

    bench = _upload_benchmark(client, filename='listed.pdf', vl_ids=[conn1.id, conn2.id])
    assert bench.status_code == 200, bench.text
    variant_job_ids = {v['job_id'] for v in bench.json()['variants']}

    jobs_resp = client.get('/api/v1/jobs')
    assert jobs_resp.status_code == 200
    job_ids_in_list = {item['id'] for item in jobs_resp.json()['items']}
    assert not (variant_job_ids & job_ids_in_list)

    search_resp = client.get('/api/v1/search')
    assert search_resp.status_code == 200
    search_ids = {item['id'] for item in search_resp.json()['items']}
    assert not (variant_job_ids & search_ids)

    for job_id in variant_job_ids:
        detail_resp = client.get(f'/api/v1/jobs/{job_id}')
        assert detail_resp.status_code == 200
        assert detail_resp.json()['benchmark_run_id'] == bench.json()['id']


def test_benchmark_children_excluded_from_stats_markdown_files_and_folder_surfaces():
    # The _apply_visible_filter-only surfaces (/stats, /markdown-files,
    # /folders/*) must honor the same exclusion as GET /jobs and /search:
    # finished benchmark variants are not browsable documents and never
    # count toward dashboard stats.
    user = _user('benchsurfaces', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Surface Conn One')
    conn2 = _make_vl_connection(name='Surface Conn Two')

    bench = _upload_benchmark(client, filename='surfaced.pdf', vl_ids=[conn1.id, conn2.id])
    assert bench.status_code == 200, bench.text
    run_id = bench.json()['id']
    for variant in bench.json()['variants']:
        _mark_job(
            variant['job_id'],
            status=JobStatus.FINISHED,
            result_markdown='---\nsource: surfaced.pdf\n---\n\nBody',
            execution={'status': 'finished', 'duration_seconds': 1.0, 'page_count': 3, 'used_fallback': False},
        )

    stats = client.get('/api/v1/stats').json()
    assert stats['processed_documents'] == 0
    assert stats['processed_pages'] == 0

    listing = client.get('/api/v1/markdown-files').json()
    assert listing['items'] == []

    assert client.get('/api/v1/folders/benchmarks/download').status_code == 404

    restart_resp = client.post('/api/v1/folders/benchmarks/restart')
    assert restart_resp.status_code == 200
    assert restart_resp.json()['restarted_jobs'] == 0

    # Deleting the synthetic 'benchmarks' folder must not orphan the run by
    # deleting its child jobs -- benchmark rows go away only via
    # DELETE /benchmarks/{id}.
    delete_resp = client.delete('/api/v1/folders/benchmarks')
    assert delete_resp.status_code == 200
    assert delete_resp.json()['deleted_jobs'] == 0
    detail_resp = client.get(f'/api/v1/benchmarks/{run_id}')
    assert detail_resp.status_code == 200
    assert detail_resp.json()['variant_count'] == 2


# --- GET /benchmarks, /benchmarks/{id} -------------------------------------------

def test_list_and_get_benchmark_runs():
    user = _user('benchget', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Get Conn One')
    conn2 = _make_vl_connection(name='Get Conn Two')

    bench = _upload_benchmark(client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']

    list_resp = client.get('/api/v1/benchmarks')
    assert list_resp.status_code == 200
    items = list_resp.json()['items']
    assert any(item['id'] == run_id for item in items)
    matching = next(item for item in items if item['id'] == run_id)
    assert matching['variant_count'] == 2
    assert matching['owner']['username'] == user.username
    # Exercises list_benchmarks' grouped Job.status/count(*) Core query path
    # (distinct from the ORM-loaded-job path every other status assertion in
    # this file goes through) -- both jobs are freshly created PENDING.
    assert matching['status'] == 'pending'

    detail_resp = client.get(f'/api/v1/benchmarks/{run_id}')
    assert detail_resp.status_code == 200
    assert detail_resp.json()['variant_count'] == 2

    # One variant FINISHED, one still PENDING -> mixed, must derive 'running'
    # (not 'pending' and not 'completed') through the same grouped query.
    variant_job_id = bench.json()['variants'][0]['job_id']
    _mark_job(variant_job_id, status=JobStatus.FINISHED, result_markdown='---\nsource: x\n---\n\nBody')
    mixed_list_resp = client.get('/api/v1/benchmarks')
    mixed_matching = next(item for item in mixed_list_resp.json()['items'] if item['id'] == run_id)
    assert mixed_matching['status'] == 'running'


def test_get_benchmark_not_found_returns_404():
    user = _user('benchmissing', role=UserRole.USER)
    client = login_as(user.username)
    resp = client.get(f'/api/v1/benchmarks/{uuid.uuid4()}')
    assert resp.status_code == 404


# --- Visibility -------------------------------------------------------------------

def test_benchmark_visibility_team_sees_outsider_gets_404():
    team_id = _make_team('bench-team')
    owner = create_test_user(username=f'benchowner-{uuid.uuid4().hex[:8]}', email=f'bo-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id)
    teammate = create_test_user(username=f'benchmate-{uuid.uuid4().hex[:8]}', email=f'bm-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id)
    outsider = _user('benchoutsider', role=UserRole.USER)

    owner_client = login_as(owner.username)
    conn1 = _make_vl_connection(name='Vis Conn One')
    conn2 = _make_vl_connection(name='Vis Conn Two')
    bench = _upload_benchmark(owner_client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']

    teammate_client = login_as(teammate.username)
    assert teammate_client.get(f'/api/v1/benchmarks/{run_id}').status_code == 200

    outsider_client = login_as(outsider.username)
    assert outsider_client.get(f'/api/v1/benchmarks/{run_id}').status_code == 404


def test_benchmark_delete_requires_owner_or_admin():
    team_id = _make_team('bench-del-team')
    owner = create_test_user(username=f'bdelowner-{uuid.uuid4().hex[:8]}', email=f'bdo-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id)
    teammate = create_test_user(username=f'bdelmate-{uuid.uuid4().hex[:8]}', email=f'bdm-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id)

    owner_client = login_as(owner.username)
    conn1 = _make_vl_connection(name='Del Conn One')
    conn2 = _make_vl_connection(name='Del Conn Two')
    bench = _upload_benchmark(owner_client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']

    teammate_client = login_as(teammate.username)
    resp = teammate_client.delete(f'/api/v1/benchmarks/{run_id}')
    assert resp.status_code == 403

    owner_resp = owner_client.delete(f'/api/v1/benchmarks/{run_id}')
    assert owner_resp.status_code == 200
    assert owner_resp.json()['deleted_jobs'] == 2


# --- Report + export.json --------------------------------------------------------

def test_benchmark_report_partial_then_finished_and_failed():
    user = _user('benchreport', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Report Conn One')
    conn2 = _make_vl_connection(name='Report Conn Two')

    bench = _upload_benchmark(client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']
    job1_id, job2_id = [v['job_id'] for v in bench.json()['variants']]

    partial = client.get(f'/api/v1/benchmarks/{run_id}/report')
    assert partial.status_code == 200
    partial_body = partial.json()
    assert partial_body['status'] == 'pending'
    assert partial_body['all_terminal'] is False
    for variant in partial_body['variants']:
        assert variant['status'] == 'PENDING'
        assert variant['duration_seconds'] is None
        assert variant['page_count'] is None
        assert variant['output_chars'] is None
        assert variant['quality_grade'] is None
        assert variant['used_fallback'] is None
        assert variant['error'] is None
    assert partial_body['summary']['fastest_variant_job_id'] is None
    assert partial_body['summary']['highest_quality_variant_job_id'] is None

    _mark_job(
        job1_id,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: x\n---\n\nHello world',
        execution={
            'status': 'finished', 'duration_seconds': 3.5, 'page_count': 2,
            'quality_gate': {'grade': 'A'}, 'used_fallback': False,
        },
    )
    _mark_job(
        job2_id,
        status=JobStatus.FAILED,
        error_message='vision endpoint unreachable',
        execution={'status': 'failed', 'error': 'vision endpoint unreachable', 'duration_seconds': 1.2},
    )

    final = client.get(f'/api/v1/benchmarks/{run_id}/report')
    assert final.status_code == 200
    final_body = final.json()
    assert final_body['status'] == 'completed'
    assert final_body['all_terminal'] is True

    finished_variant = next(v for v in final_body['variants'] if v['job_id'] == job1_id)
    assert finished_variant['status'] == 'FINISHED'
    assert finished_variant['duration_seconds'] == 3.5
    assert finished_variant['page_count'] == 2
    assert finished_variant['quality_grade'] == 'A'
    assert finished_variant['used_fallback'] is False
    assert finished_variant['output_chars'] == len('---\nsource: x\n---\n\nHello world')
    assert finished_variant['error'] is None

    failed_variant = next(v for v in final_body['variants'] if v['job_id'] == job2_id)
    assert failed_variant['status'] == 'FAILED'
    assert failed_variant['error'] == 'vision endpoint unreachable'
    assert failed_variant['duration_seconds'] == 1.2
    assert failed_variant['page_count'] is None
    assert failed_variant['quality_grade'] is None

    assert final_body['summary']['fastest_variant_job_id'] == job1_id
    assert final_body['summary']['highest_quality_variant_job_id'] == job1_id


def test_benchmark_report_summary_never_crowns_fallback_variants():
    user = _user('benchfallback', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Fallback Conn One')
    conn2 = _make_vl_connection(name='Fallback Conn Two')

    bench = _upload_benchmark(client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']
    job1_id, job2_id = [v['job_id'] for v in bench.json()['variants']]

    # Variant 1 'finished' through the pypdf fallback after its VL endpoint
    # failed: milliseconds fast and grade A on the text layer -- exactly the
    # shape paddle_service produces for a broken endpoint on a .pdf upload.
    _mark_job(
        job1_id,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: x\n---\n\nFallback body',
        execution={
            'status': 'finished', 'duration_seconds': 0.02, 'page_count': 2,
            'quality_gate': {'grade': 'A'}, 'used_fallback': True,
        },
    )

    # Only the fallback variant is terminal: no winner may be crowned.
    partial = client.get(f'/api/v1/benchmarks/{run_id}/report').json()
    fallback_variant = next(v for v in partial['variants'] if v['job_id'] == job1_id)
    assert fallback_variant['used_fallback'] is True
    assert partial['summary']['fastest_variant_job_id'] is None
    assert partial['summary']['highest_quality_variant_job_id'] is None

    # A real (non-fallback) finish wins both summaries despite being slower
    # and lower-graded than the fallback.
    _mark_job(
        job2_id,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: x\n---\n\nReal body',
        execution={
            'status': 'finished', 'duration_seconds': 41.7, 'page_count': 2,
            'quality_gate': {'grade': 'B'}, 'used_fallback': False,
        },
    )
    final = client.get(f'/api/v1/benchmarks/{run_id}/report').json()
    assert final['summary']['fastest_variant_job_id'] == job2_id
    assert final['summary']['highest_quality_variant_job_id'] == job2_id


def test_benchmark_export_json_shape():
    user = _user('benchexport', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Export Conn One')
    conn2 = _make_vl_connection(name='Export Conn Two')

    bench = _upload_benchmark(client, filename='exportme.pdf', vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']
    job1_id, job2_id = [v['job_id'] for v in bench.json()['variants']]

    _mark_job(
        job1_id, status=JobStatus.FINISHED, result_markdown='---\nsource: exportme.pdf\n---\n\nBody',
        execution={'status': 'finished', 'duration_seconds': 1.0, 'page_count': 1, 'quality_gate': {'grade': 'B'}, 'used_fallback': False},
    )

    resp = client.get(f'/api/v1/benchmarks/{run_id}/export.json')
    assert resp.status_code == 200
    assert resp.headers['content-type'].startswith('application/json')
    assert f'benchmark-{run_id}.json' in resp.headers['content-disposition']
    assert 'attachment' in resp.headers['content-disposition']

    body = resp.json()
    assert body['schema'] == 'paddledoc.benchmark-export/1'
    assert body['benchmark']['id'] == run_id
    assert body['benchmark']['status'] == 'running'
    assert len(body['benchmark']['content_sha256']) == 64
    assert body['report']['id'] == run_id

    variants_by_id = {v['job_id']: v for v in body['variants']}
    assert variants_by_id[job1_id]['markdown'] == '---\nsource: exportme.pdf\n---\n\nBody'
    assert variants_by_id[job2_id]['markdown'] is None
    assert variants_by_id[job2_id]['status'] == 'PENDING'


# --- Delete cascade -----------------------------------------------------------

def test_benchmark_delete_cascades_child_jobs_and_markdown_versions():
    user = _user('benchdelcascade', role=UserRole.USER)
    client = login_as(user.username)
    conn1 = _make_vl_connection(name='Cascade Conn One')
    conn2 = _make_vl_connection(name='Cascade Conn Two')

    bench = _upload_benchmark(client, vl_ids=[conn1.id, conn2.id])
    run_id = bench.json()['id']
    job1_id, job2_id = [v['job_id'] for v in bench.json()['variants']]

    _mark_job(job1_id, status=JobStatus.FINISHED, result_markdown='---\nsource: x\n---\n\nBody')
    save_resp = client.put(f'/api/v1/jobs/{job1_id}/save', json={'markdown': '---\nsource: x\n---\n\nEdited body'})
    assert save_resp.status_code == 200

    delete_resp = client.delete(f'/api/v1/benchmarks/{run_id}')
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {'id': run_id, 'deleted_jobs': 2}

    assert client.get(f'/api/v1/benchmarks/{run_id}').status_code == 404
    assert client.get(f'/api/v1/jobs/{job1_id}').status_code == 404
    assert client.get(f'/api/v1/jobs/{job2_id}').status_code == 404

    db = _db()
    try:
        assert db.get(BenchmarkRun, run_id) is None
        assert db.get(Job, job1_id) is None
        assert db.get(Job, job2_id) is None
        remaining_versions = db.scalars(
            select(JobMarkdownVersion).where(JobMarkdownVersion.job_id == job1_id)
        ).all()
        assert remaining_versions == []
    finally:
        db.close()


def test_job_level_delete_and_restart_reject_benchmark_children_regardless_of_caller():
    """Job.benchmark_run_id children must only ever be removed/requeued via
    DELETE /benchmarks/{id} (owner/admin control, see
    _require_benchmark_control) -- never the general single-job endpoints,
    which only apply ordinary owner/teammate *visibility*. A teammate with
    mere read access to someone else's run must not be able to corrupt it
    through DELETE /jobs/{id}, /restart, or /retry-lower-profile; neither
    should the run owner or an admin bypass the benchmark endpoint by using
    the job endpoint directly."""
    team_id = _make_team('bench-job-guard-team')
    owner = create_test_user(
        username=f'bjgowner-{uuid.uuid4().hex[:8]}', email=f'bjgo-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id
    )
    teammate = create_test_user(
        username=f'bjgmate-{uuid.uuid4().hex[:8]}', email=f'bjgm-{uuid.uuid4().hex[:8]}@example.com', team_id=team_id
    )
    admin_user = _user('bjgadmin', role=UserRole.ADMIN)

    owner_client = login_as(owner.username)
    conn1 = _make_vl_connection(name='Guard Conn One')
    conn2 = _make_vl_connection(name='Guard Conn Two')
    bench = _upload_benchmark(owner_client, vl_ids=[conn1.id, conn2.id])
    assert bench.status_code == 200, bench.text
    job_id = bench.json()['variants'][0]['job_id']
    _mark_job(job_id, status=JobStatus.FINISHED, result_markdown='---\nsource: x\n---\n\nBody')

    teammate_client = login_as(teammate.username)
    admin_client = login_as(admin_user.username)

    expected_detail = 'This job is part of a benchmark run; manage it via the benchmark.'
    for client in (teammate_client, owner_client, admin_client):
        restart_resp = client.post(f'/api/v1/jobs/{job_id}/restart')
        assert restart_resp.status_code == 409, restart_resp.text
        assert restart_resp.json()['detail'] == expected_detail

        retry_resp = client.post(f'/api/v1/jobs/{job_id}/retry-lower-profile')
        assert retry_resp.status_code == 409, retry_resp.text
        assert retry_resp.json()['detail'] == expected_detail

        delete_resp = client.delete(f'/api/v1/jobs/{job_id}')
        assert delete_resp.status_code == 409, delete_resp.text
        assert delete_resp.json()['detail'] == expected_detail

    db = _db()
    try:
        row = db.get(Job, job_id)
        assert row is not None
        assert row.status == JobStatus.FINISHED
        assert row.result_markdown == '---\nsource: x\n---\n\nBody'
    finally:
        db.close()


# --- Worker: graceful failure when the VL connection is gone/disabled ----------
#
# Calls the Celery task body directly (`tasks.process_job(...)`, bypassing
# `.delay`), same idiom as test_api.py's
# test_process_job_deletes_stale_result_before_rewriting.

def _make_pending_job_with_settings(job_id: str, upload_path, settings_extra: dict) -> None:
    db = TestingSessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).delete()
        db.commit()
        db.add(
            Job(
                id=job_id,
                original_filename=f'{job_id}.pdf',
                upload_path=str(upload_path),
                upload_content=b'%PDF-1.4 fake upload content',
                upload_mime_type='application/pdf',
                upload_size_bytes=len(b'%PDF-1.4 fake upload content'),
                status=JobStatus.PENDING,
                processing_info={'settings': settings_extra},
            )
        )
        db.commit()
    finally:
        db.close()


def test_process_job_fails_gracefully_when_vl_connection_missing(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.workers import tasks

    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    upload_path = settings.uploads_dir / 'benchmarks' / 'run-x' / 'job-vl-missing.pdf'
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b'%PDF-1.4 fake upload content')

    _make_pending_job_with_settings('job-vl-missing', upload_path, {
        'storage_folder': 'benchmarks/run-x/job-vl-missing',
        'vl_connection_id': str(uuid.uuid4()),  # never created -> missing
    })

    tasks.process_job('job-vl-missing', 'openai_vision', 'benchmark', '', None)

    db = TestingSessionLocal()
    try:
        job = db.get(Job, 'job-vl-missing')
        assert job.status == JobStatus.FAILED
        assert job.error_message == 'VL connection is no longer available'
        assert job.processing_info['execution']['status'] == 'failed'
    finally:
        db.close()


def test_process_job_fails_gracefully_when_vl_connection_disabled(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.workers import tasks

    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    connection = _make_vl_connection(enabled=False)

    upload_path = settings.uploads_dir / 'benchmarks' / 'run-y' / 'job-vl-disabled.pdf'
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b'%PDF-1.4 fake upload content')

    _make_pending_job_with_settings('job-vl-disabled', upload_path, {
        'storage_folder': 'benchmarks/run-y/job-vl-disabled',
        'vl_connection_id': connection.id,
    })

    tasks.process_job('job-vl-disabled', 'openai_vision', 'benchmark', '', None)

    db = TestingSessionLocal()
    try:
        job = db.get(Job, 'job-vl-disabled')
        assert job.status == JobStatus.FAILED
        assert job.error_message == 'VL connection is no longer available'
    finally:
        db.close()
