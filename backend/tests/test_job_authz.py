"""Step 3 authorization tests: row-level job visibility (owner/team/admin),
legacy NULL-owner jobs, and scoped listing/search totals.

Uses real cookie-based logins (via `login_as`/`create_test_user` from
conftest.py) rather than the `get_current_user` dependency-override bypass
`test_api.py` uses for its business-logic tests: visibility is computed by
joining against the real `users` table (owner_id, team_id), so it has to be
exercised with actual persisted users and real sessions to mean anything.
"""

import io
import uuid
import zipfile

import pytest

from app.models.models import Job, JobStatus, User, UserRole
from app.services.security import rate_limiter
from conftest import TestingSessionLocal, create_test_user, login_as


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # /auth/login is rate-limited per client host, and TestClient always
    # presents as "testclient" -- every login in this module (and any other
    # module sharing the process) shares one bucket unless reset per test.
    rate_limiter.reset()
    yield


def _db():
    return TestingSessionLocal()


def _make_job(
    *,
    owner_id: str | None,
    filename: str = 'doc.pdf',
    status: JobStatus = JobStatus.FINISHED,
    result_markdown: str | None = None,
    processing_info: dict | None = None,
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
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        db.expunge(job)
        return job
    finally:
        db.close()


def _create_team(name: str) -> str:
    from app.models.models import Team

    db = _db()
    try:
        team = Team(name=name)
        db.add(team)
        db.commit()
        db.refresh(team)
        team_id = team.id
        return team_id
    finally:
        db.close()


# --- owner-only visibility: 404, not 403 -------------------------------------

def test_user_cannot_see_other_users_job_returns_404():
    user_a = create_test_user(username='authz-user-a', email='authz-a@example.com')
    user_b = create_test_user(username='authz-user-b', email='authz-b@example.com')
    job_b = _make_job(owner_id=user_b.id)

    client_a = login_as('authz-user-a')

    detail_resp = client_a.get(f'/api/v1/jobs/{job_b.id}')
    assert detail_resp.status_code == 404

    list_resp = client_a.get('/api/v1/jobs')
    assert list_resp.status_code == 200
    assert job_b.id not in {item['id'] for item in list_resp.json()['items']}

    del user_a  # only needed to create user A's session above


def test_owner_can_see_own_job():
    owner = create_test_user(username='authz-owner-1', email='authz-owner-1@example.com')
    job = _make_job(owner_id=owner.id)

    client = login_as('authz-owner-1')
    resp = client.get(f'/api/v1/jobs/{job.id}')
    assert resp.status_code == 200
    assert resp.json()['id'] == job.id


# --- team-based visibility ----------------------------------------------------

def test_team_members_share_job_visibility():
    team_id = _create_team('authz-team-shared')
    owner = create_test_user(username='authz-team-owner', email='authz-team-owner@example.com', team_id=team_id)
    teammate = create_test_user(username='authz-teammate', email='authz-teammate@example.com', team_id=team_id)
    job = _make_job(owner_id=owner.id)

    teammate_client = login_as('authz-teammate')
    resp = teammate_client.get(f'/api/v1/jobs/{job.id}')
    assert resp.status_code == 200
    assert resp.json()['id'] == job.id

    list_resp = teammate_client.get('/api/v1/jobs')
    assert job.id in {item['id'] for item in list_resp.json()['items']}


def test_different_teams_do_not_share_visibility():
    team_one = _create_team('authz-team-one')
    team_two = _create_team('authz-team-two')
    owner = create_test_user(username='authz-team-one-owner', email='authz-team-one-owner@example.com', team_id=team_one)
    outsider = create_test_user(
        username='authz-team-two-member', email='authz-team-two-member@example.com', team_id=team_two
    )
    job = _make_job(owner_id=owner.id)

    outsider_client = login_as('authz-team-two-member')
    resp = outsider_client.get(f'/api/v1/jobs/{job.id}')
    assert resp.status_code == 404


# --- admin sees everything, including legacy NULL-owner jobs -----------------

def test_admin_sees_all_jobs_including_legacy_null_owner():
    admin = create_test_user(username='authz-admin-1', email='authz-admin-1@example.com', role=UserRole.ADMIN)
    other_user = create_test_user(username='authz-admin-other', email='authz-admin-other@example.com')
    owned_job = _make_job(owner_id=other_user.id)
    legacy_job = _make_job(owner_id=None)

    admin_client = login_as('authz-admin-1')

    assert admin_client.get(f'/api/v1/jobs/{owned_job.id}').status_code == 200
    assert admin_client.get(f'/api/v1/jobs/{legacy_job.id}').status_code == 200

    visible_ids = {item['id'] for item in admin_client.get('/api/v1/jobs').json()['items']}
    assert owned_job.id in visible_ids
    assert legacy_job.id in visible_ids

    del admin


def test_legacy_null_owner_job_hidden_from_non_admin():
    user = create_test_user(username='authz-legacy-user', email='authz-legacy-user@example.com')
    legacy_job = _make_job(owner_id=None)

    client = login_as('authz-legacy-user')

    detail_resp = client.get(f'/api/v1/jobs/{legacy_job.id}')
    assert detail_resp.status_code == 404

    list_resp = client.get('/api/v1/jobs')
    assert legacy_job.id not in {item['id'] for item in list_resp.json()['items']}


# --- scoped /search total ------------------------------------------------------

def test_scoped_search_total_counts_only_visible_jobs():
    user_a = create_test_user(username='authz-search-a', email='authz-search-a@example.com')
    user_b = create_test_user(username='authz-search-b', email='authz-search-b@example.com')

    for _ in range(3):
        _make_job(owner_id=user_a.id)
    for _ in range(5):
        _make_job(owner_id=user_b.id)

    client_a = login_as('authz-search-a')

    # Unpaginated: total == len(items) == only A's own jobs.
    unpaginated = client_a.get('/api/v1/search')
    assert unpaginated.status_code == 200
    unpaginated_body = unpaginated.json()
    assert unpaginated_body['total'] == len(unpaginated_body['items']) == 3

    # Paginated: total must still reflect the scoped count, not the raw
    # (unscoped) row count across both users (8) and not just the page size.
    paginated = client_a.get('/api/v1/search', params={'limit': 2, 'offset': 0})
    assert paginated.status_code == 200
    paginated_body = paginated.json()
    assert len(paginated_body['items']) == 2
    assert paginated_body['total'] == 3


# --- other row-level endpoints reuse the same 404 gate ------------------------

def test_delete_and_restart_are_also_gated_by_visibility():
    user_a = create_test_user(username='authz-gate-a', email='authz-gate-a@example.com')
    user_b = create_test_user(username='authz-gate-b', email='authz-gate-b@example.com')
    job_b_pending = _make_job(owner_id=user_b.id, status=JobStatus.PENDING)
    job_b_finished = _make_job(owner_id=user_b.id, status=JobStatus.FINISHED)

    client_a = login_as('authz-gate-a')

    assert client_a.post(f'/api/v1/jobs/{job_b_pending.id}/restart').status_code == 404
    assert client_a.delete(f'/api/v1/jobs/{job_b_finished.id}').status_code == 404
    assert client_a.get(f'/api/v1/jobs/{job_b_finished.id}/download').status_code == 404
    assert client_a.get(f'/api/v1/jobs/{job_b_finished.id}/preview').status_code == 404
    assert client_a.put(f'/api/v1/jobs/{job_b_finished.id}/save', json={'markdown': '---\nx: 1\n---\nbody'}).status_code == 404
    assert client_a.post(f'/api/v1/jobs/{job_b_finished.id}/verify-password', json={'password': 'whatever'}).status_code == 404

    del user_a  # only needed for user A's session above


def test_password_protected_job_endpoints_are_rate_limited():
    """Regression for a blocking gap: verify-password/download/preview/save/
    delete all call `_check_job_password` (bcrypt against Job.password_hash)
    but, until fixed, none of them called `enforce_rate_limit`, so a logged
    in, visibility-authorized caller could brute-force the extra per-job
    password with unlimited attempts. All five must now share the Redis
    rate limiter budget."""
    from app.core.config import settings

    user = create_test_user(username='authz-ratelimit-user', email='authz-ratelimit-user@example.com')
    job = _make_job(owner_id=user.id, result_markdown='# hi')
    client = login_as('authz-ratelimit-user')
    rate_limiter.reset()  # isolate from the budget login_as() itself consumed

    for _ in range(settings.rate_limit_per_minute):
        # Job has no password set, so this is always a cheap 200 -- only the
        # rate-limit budget itself is under test here.
        resp = client.post(f'/api/v1/jobs/{job.id}/verify-password', json={'password': 'nope'})
        assert resp.status_code == 200

    assert client.post(f'/api/v1/jobs/{job.id}/verify-password', json={'password': 'nope'}).status_code == 429
    assert client.get(f'/api/v1/jobs/{job.id}/download').status_code == 429
    assert client.get(f'/api/v1/jobs/{job.id}/preview').status_code == 429
    assert client.put(f'/api/v1/jobs/{job.id}/save', json={'markdown': '---\nx: 1\n---\nbody'}).status_code == 429
    assert client.delete(f'/api/v1/jobs/{job.id}').status_code == 429


# --- upload stamps owner_id ----------------------------------------------------

def test_upload_stamps_owner_id_and_hides_from_other_users(monkeypatch, tmp_path):
    from app.api import routes
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    monkeypatch.setattr(routes.process_job, 'delay', lambda *a, **k: None)

    uploader = create_test_user(username='authz-uploader', email='authz-uploader@example.com')
    other = create_test_user(username='authz-not-uploader', email='authz-not-uploader@example.com')

    uploader_client = login_as('authz-uploader')
    upload_resp = uploader_client.post(
        '/api/v1/upload',
        files={'file': ('owned.pdf', b'%PDF-sample', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny'},
    )
    assert upload_resp.status_code == 200
    job_id = upload_resp.json()['job_id']

    db = _db()
    try:
        job = db.get(Job, job_id)
        assert job.owner_id == uploader.id
    finally:
        db.close()

    other_client = login_as('authz-not-uploader')
    assert other_client.get(f'/api/v1/jobs/{job_id}').status_code == 404

    del other


# --- folder ZIP: scoped visibility applied per-row without a lazy-load trap ---
#
# download_folder_markdown fetches its rows through _apply_visible_filter
# (same SQL-level WHERE fragment as /jobs and /search) and then, in a loop,
# touches job.result_markdown and job.processing_info on every row before
# building the zip. Both of those are real regression traps if the query
# options ever changed to defer them (see test_deferred_blob_column_raises_
# after_session_close for the unit-level proof the trap is real for this ORM
# config): a deferred/never-loaded column touched after the request's `db`
# session is torn down raises DetachedInstanceError instead of silently
# working. This exercises the full HTTP path -- scoped query, in-loop column
# access, response streamed back out -- for a folder that mixes a visible
# (teammate-owned) job with an invisible (outside-team) job in the same
# folder path, which is exactly the shape that would surface either bug.
def test_folder_zip_is_scoped_by_visibility_and_survives_session_teardown():
    team_id = _create_team('authz-zip-team')
    owner = create_test_user(username='authz-zip-owner', email='authz-zip-owner@example.com', team_id=team_id)
    teammate = create_test_user(username='authz-zip-teammate', email='authz-zip-teammate@example.com', team_id=team_id)
    outsider = create_test_user(username='authz-zip-outsider', email='authz-zip-outsider@example.com')

    folder_info = {'settings': {'folder': 'authz-zip-folder', 'subfolder': '', 'storage_folder': 'authz-zip-folder'}}
    visible_job = _make_job(
        owner_id=owner.id, result_markdown='# visible to the team', processing_info=folder_info
    )
    hidden_job = _make_job(
        owner_id=outsider.id, result_markdown='# not visible to the team', processing_info=folder_info
    )

    teammate_client = login_as('authz-zip-teammate')
    resp = teammate_client.get('/api/v1/folders/authz-zip-folder/download')
    assert resp.status_code == 200

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    assert any(visible_job.id in name for name in names)
    assert not any(hidden_job.id in name for name in names)

    del owner  # only needed to own the visible job above
