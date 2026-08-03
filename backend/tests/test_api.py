import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes import _JOB_LIST_PAGE_LIMIT_MAX
from app.database.session import get_db
from app.main import app
from app.models.models import Base, Job, JobMarkdownVersion, JobStatus

TEST_DB = 'sqlite:///./test.db'
engine = create_engine(TEST_DB, future=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def test_healthcheck():
    response = client.get('/api/v1/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'healthy'}


def test_upload_rejects_unsupported_type():
    response = client.post(
        '/api/v1/upload',
        files={'file': ('malware.exe', b'x', 'application/octet-stream')},
    )
    assert response.status_code == 400


def test_upload_creates_job(monkeypatch, tmp_path):
    from app.api import routes
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    called = {}

    def fake_delay(
        job_id: str,
        profile_id: str | None = None,
        mode: str | None = None,
        email: str | None = None,
        department: str | None = None,
    ):
        called['job_id'] = job_id
        called['profile_id'] = profile_id
        called['mode'] = mode
        called['email'] = email
        called['department'] = department

    monkeypatch.setattr(routes.process_job, 'delay', fake_delay)

    response = client.post(
        '/api/v1/upload',
        files={'file': ('document.pdf', b'%PDF-sample', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny', 'email': 'single@example.com', 'tags': 'finance, invoices'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == JobStatus.PENDING.value
    assert 'job_id' in payload
    assert called['job_id'] == payload['job_id']
    assert called['profile_id'] == 'ppocrv6_tiny'
    assert called['mode'] == 'single'
    assert called['email'] == 'single@example.com'

    db = TestingSessionLocal()
    job = db.get(Job, payload['job_id'])
    assert job is not None
    assert '/inbox/' in job.upload_path.replace('\\', '/')
    assert job.upload_content == b'%PDF-sample'
    assert job.upload_mime_type == 'application/pdf'
    assert job.upload_size_bytes == len(b'%PDF-sample')
    assert sorted(tag.name for tag in job.tags) == ['finance', 'invoices']
    db.close()


def test_upload_allows_missing_email(monkeypatch, tmp_path):
    from app.api import routes
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    called = {}

    def fake_delay(
        job_id: str,
        profile_id: str | None = None,
        mode: str | None = None,
        email: str | None = None,
        department: str | None = None,
    ):
        called['email'] = email

    monkeypatch.setattr(routes.process_job, 'delay', fake_delay)

    response = client.post(
        '/api/v1/upload',
        files={'file': ('document.pdf', b'%PDF-sample', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny', 'tags': 'draft'},
    )
    assert response.status_code == 200
    assert called['email'] == ''


def test_collection_flow(monkeypatch, tmp_path):
    from app.api import routes
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    delayed: list[dict[str, str | None]] = []

    def fake_delay(
        job_id: str,
        profile_id: str | None = None,
        mode: str | None = None,
        email: str | None = None,
        department: str | None = None,
    ):
        delayed.append(
            {
                'job_id': job_id,
                'profile_id': profile_id,
                'mode': mode,
                'email': email,
                'department': department,
            }
        )

    monkeypatch.setattr(routes.process_job, 'delay', fake_delay)

    create_resp = client.post(
        '/api/v1/collections',
        json={'folder': 'accounts', 'subfolder': '2026'},
    )
    assert create_resp.status_code == 200
    collection_id = create_resp.json()['collection_id']

    upload_resp = client.post(
        f'/api/v1/collections/{collection_id}/upload',
        files={'file': ('document-a.pdf', b'%PDF-sample', 'application/pdf')},
    )
    assert upload_resp.status_code == 200
    job_id = upload_resp.json()['job_id']

    db = TestingSessionLocal()
    collection_job = db.get(Job, job_id)
    assert collection_job is not None
    assert '/accounts/2026/' in collection_job.upload_path.replace('\\', '/')
    db.close()

    upload_resp_2 = client.post(
        f'/api/v1/collections/{collection_id}/upload',
        files={'file': ('document-b.pdf', b'%PDF-sample-2', 'application/pdf')},
    )
    assert upload_resp_2.status_code == 200
    job_id_2 = upload_resp_2.json()['job_id']

    start_resp = client.post(
        f'/api/v1/collections/{collection_id}/start',
        json={'profile_id': 'ppocrv6_medium'},
    )
    assert start_resp.status_code == 200
    assert start_resp.json()['started_jobs'] == 2
    assert delayed[0]['job_id'] == job_id
    assert delayed[1]['job_id'] == job_id_2
    assert delayed[0]['profile_id'] == 'ppocrv6_medium'
    assert delayed[0]['mode'] == 'collection'
    assert delayed[0]['email'] == ''
    assert delayed[0]['department'] == ''


def test_markdown_browser_lists_files(tmp_path):
    """DB-derived: the browser tree is built from Job rows with
    result_markdown, not from anything on disk. Field-for-field the response
    shape (path/filename/folder/size_bytes/updated_at) matches what the old
    filesystem-scanning handler produced.
    """
    db = TestingSessionLocal()
    db.query(Job).filter(Job.id.in_(['job-1', 'job-2'])).delete(synchronize_session=False)
    db.add_all(
        [
            Job(
                id='job-1',
                original_filename='single.pdf',
                upload_path=str(tmp_path / 'single.pdf'),
                upload_content=b's',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
                result_markdown='# single',
                # No folder/subfolder recorded -> synthesized under 'inbox'.
            ),
            Job(
                id='job-2',
                original_filename='collection.pdf',
                upload_path=str(tmp_path / 'collection.pdf'),
                upload_content=b'c',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
                result_markdown='# collection',
                processing_info={'settings': {'folder': 'collections', 'subfolder': 'collection-1'}},
            ),
        ]
    )
    db.commit()
    db.close()

    list_resp = client.get('/api/v1/markdown-files')
    assert list_resp.status_code == 200
    payload = list_resp.json()
    items_by_path = {item['path']: item for item in payload['items']}
    assert 'inbox/job-1/job-1.md' in items_by_path
    assert 'collections/collection-1/job-2/job-2.md' in items_by_path

    entry_one = items_by_path['inbox/job-1/job-1.md']
    assert entry_one['filename'] == 'job-1.md'
    assert entry_one['folder'] == 'inbox/job-1'
    assert entry_one['size_bytes'] == len('# single'.encode('utf-8'))
    assert 'updated_at' in entry_one

    entry_two = items_by_path['collections/collection-1/job-2/job-2.md']
    assert entry_two['filename'] == 'job-2.md'
    assert entry_two['folder'] == 'collections/collection-1/job-2'
    assert entry_two['size_bytes'] == len('# collection'.encode('utf-8'))

    file_resp = client.get('/api/v1/markdown-files/inbox/job-1/job-1.md')
    assert file_resp.status_code == 200
    assert file_resp.text == '# single'

    nested_resp = client.get('/api/v1/markdown-files/collections/collection-1/job-2/job-2.md')
    assert nested_resp.status_code == 200
    assert nested_resp.text == '# collection'


def test_markdown_browser_ignores_orphan_disk_files(tmp_path):
    """The filesystem is never consulted: a .md file written directly to
    results_dir with no backing Job row must not appear in the listing and
    must not be servable via the content endpoint, even if its path happens
    to line up with the synthetic layout.
    """
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    orphan = settings.results_dir / 'inbox' / 'orphan-job' / 'orphan-job.md'
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text('# orphan', encoding='utf-8')

    list_resp = client.get('/api/v1/markdown-files')
    assert list_resp.status_code == 200
    assert all('orphan-job' not in item['path'] for item in list_resp.json()['items'])

    file_resp = client.get('/api/v1/markdown-files/inbox/orphan-job/orphan-job.md')
    assert file_resp.status_code == 404


def test_search_filters_by_name_and_tag(tmp_path):
    db = TestingSessionLocal()
    job_one = Job(
        id='search-1',
        original_filename='Invoice_April.pdf',
        upload_path=str(tmp_path / 'invoice.pdf'),
        upload_content=b'1',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
    )
    job_two = Job(
        id='search-2',
        original_filename='Receipt_May.pdf',
        upload_path=str(tmp_path / 'receipt.pdf'),
        upload_content=b'2',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
    )
    db.add_all([job_one, job_two])
    db.commit()

    from app.api import routes

    tag = routes.Tag(name='search-finance')
    job_one.tags.append(tag)
    db.add(tag)
    db.commit()
    db.close()

    search_resp = client.get('/api/v1/search?q=invoice&tag=search-finance')
    assert search_resp.status_code == 200
    body = search_resp.json()
    assert body['total'] == 1
    assert body['items'][0]['id'] == 'search-1'

    jobs_resp = client.get('/api/v1/jobs?tag=search-finance')
    assert jobs_resp.status_code == 200
    assert any(item['id'] == 'search-1' for item in jobs_resp.json()['items'])

    running_job = Job(
        id='search-3',
        original_filename='Running.pdf',
        upload_path=str(tmp_path / 'running.pdf'),
        upload_content=b'3',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.RUNNING,
    )
    db = TestingSessionLocal()
    db.add(running_job)
    db.commit()
    db.close()

    running_resp = client.get('/api/v1/jobs?status=RUNNING')
    assert running_resp.status_code == 200
    assert all(item['status'] == JobStatus.RUNNING.value for item in running_resp.json()['items'])


def test_dashboard_stats_aggregate(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.models.models import Tag

    stats_db = tmp_path / 'stats.db'
    stats_db.write_bytes(b'stats')
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{stats_db}')
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    db = TestingSessionLocal()
    db.query(Job).delete()
    db.query(Tag).delete()
    db.commit()

    finished = Job(
        id='stats-finished',
        original_filename='finished.pdf',
        upload_path=str(tmp_path / 'finished.pdf'),
        upload_content=b'1',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        processing_info={'execution': {'page_count': 7}},
    )
    failed = Job(
        id='stats-failed',
        original_filename='failed.pdf',
        upload_path=str(tmp_path / 'failed.pdf'),
        upload_content=b'2',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FAILED,
    )
    db.add_all([finished, failed])
    db.commit()
    db.close()

    response = client.get('/api/v1/stats')
    assert response.status_code == 200
    payload = response.json()
    assert payload['processed_documents'] == 1
    assert payload['processed_pages'] == 7
    assert payload['errors'] == 1
    assert isinstance(payload['database_size_bytes'], int)


def test_save_markdown_creates_new_version(tmp_path):
    db = TestingSessionLocal()
    result_file = tmp_path / 'result.md'
    result_file.write_text('---\nsource: "x"\n---\n\n# done', encoding='utf-8')
    job = Job(
        id='job-save',
        original_filename='a.pdf',
        upload_path=str(tmp_path / 'a.pdf'),
        result_path=str(result_file),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        processing_info={},
    )
    db.add(job)
    db.commit()
    db.close()

    save_resp = client.put(
        '/api/v1/jobs/job-save/save',
        json={'markdown': '---\nsource: "x"\nmode: "single"\nemail: "x@y.com"\n---\n\n# edited'},
    )
    assert save_resp.status_code == 200
    body = save_resp.json()
    assert body['version'] == 1

    preview_resp = client.get('/api/v1/jobs/job-save/preview')
    assert preview_resp.status_code == 200
    assert '# edited' in preview_resp.text

    db = TestingSessionLocal()
    saved = db.get(Job, 'job-save')
    assert saved is not None
    assert saved.result_markdown is not None and '# edited' in saved.result_markdown
    db.close()


def test_save_markdown_response_path_is_null_and_no_disk_file_written(tmp_path):
    """Editor saves no longer write '.v{n}.md' files to disk; the response
    keeps its 'path' field (backward-compatible shape) but the value is now
    always null.
    """
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    db = TestingSessionLocal()
    job = Job(
        id='job-save-nodisk',
        original_filename='a.pdf',
        upload_path=str(tmp_path / 'a.pdf'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: "x"\n---\n\n# done',
        processing_info={},
    )
    db.add(job)
    db.commit()
    db.close()

    save_resp = client.put(
        '/api/v1/jobs/job-save-nodisk/save',
        json={'markdown': '---\nsource: "x"\n---\n\n# edited once'},
    )
    assert save_resp.status_code == 200
    body = save_resp.json()
    assert body['version'] == 1
    assert body['path'] is None

    disk_files = [p for p in settings.results_dir.rglob('*.md') if p.is_file()]
    assert disk_files == []


def test_save_markdown_creates_version_row_per_save(tmp_path):
    db = TestingSessionLocal()
    job = Job(
        id='job-save-versions',
        original_filename='a.pdf',
        upload_path=str(tmp_path / 'a.pdf'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: "x"\n---\n\n# done',
        processing_info={},
    )
    db.add(job)
    db.commit()
    db.close()

    first_resp = client.put(
        '/api/v1/jobs/job-save-versions/save',
        json={'markdown': '---\nsource: "x"\n---\n\n# first edit'},
    )
    assert first_resp.status_code == 200
    assert first_resp.json()['version'] == 1

    second_resp = client.put(
        '/api/v1/jobs/job-save-versions/save',
        json={'markdown': '---\nsource: "x"\n---\n\n# second edit'},
    )
    assert second_resp.status_code == 200
    assert second_resp.json()['version'] == 2

    db = TestingSessionLocal()
    rows = (
        db.query(JobMarkdownVersion)
        .filter(JobMarkdownVersion.job_id == 'job-save-versions')
        .order_by(JobMarkdownVersion.version)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].version == 1
    assert '# first edit' in rows[0].content
    assert rows[1].version == 2
    assert '# second edit' in rows[1].content

    saved_job = db.get(Job, 'job-save-versions')
    assert saved_job is not None
    assert saved_job.result_markdown is not None and '# second edit' in saved_job.result_markdown
    db.close()


def test_job_markdown_versions_cascade_delete_with_job(tmp_path):
    db = TestingSessionLocal()
    job = Job(
        id='job-save-cascade',
        original_filename='a.pdf',
        upload_path=str(tmp_path / 'a.pdf'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown='---\nsource: "x"\n---\n\n# done',
        processing_info={},
    )
    db.add(job)
    db.commit()
    db.close()

    for markdown in ('---\nsource: "x"\n---\n\n# v1', '---\nsource: "x"\n---\n\n# v2'):
        resp = client.put('/api/v1/jobs/job-save-cascade/save', json={'markdown': markdown})
        assert resp.status_code == 200

    db = TestingSessionLocal()
    assert db.query(JobMarkdownVersion).filter(JobMarkdownVersion.job_id == 'job-save-cascade').count() == 2
    db.close()

    delete_resp = client.delete('/api/v1/jobs/job-save-cascade')
    assert delete_resp.status_code == 200

    db = TestingSessionLocal()
    assert db.get(Job, 'job-save-cascade') is None
    assert db.query(JobMarkdownVersion).filter(JobMarkdownVersion.job_id == 'job-save-cascade').count() == 0
    db.close()


def test_list_and_download(tmp_path):
    db = TestingSessionLocal()
    result_file = tmp_path / 'result.md'
    result_file.write_text('# done', encoding='utf-8')
    job = Job(
        id='job-1',
        original_filename='a.pdf',
        upload_path=str(tmp_path / 'a.pdf'),
        result_path=str(result_file),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
    )
    db.add(job)
    db.commit()
    db.close()

    list_resp = client.get('/api/v1/jobs')
    assert list_resp.status_code == 200
    assert any(item['id'] == 'job-1' for item in list_resp.json()['items'])

    dl_resp = client.get('/api/v1/jobs/job-1/download')
    assert dl_resp.status_code == 200
    assert dl_resp.headers['content-type'].startswith('text/markdown')


def test_restart_pending_jobs(monkeypatch, tmp_path):
    from app.api import routes

    db = TestingSessionLocal()
    db.add_all(
        [
            Job(
                id='job-pending-restart',
                original_filename='pending.pdf',
                upload_path=str(tmp_path / 'pending.pdf'),
                upload_content=b'p',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.PENDING,
                processing_info={'settings': {'profile_id': 'ppocrv6_small', 'mode': 'single'}},
            ),
            Job(
                id='job-finished-ignore',
                original_filename='finished.pdf',
                upload_path=str(tmp_path / 'finished.pdf'),
                upload_content=b'f',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
            ),
        ]
    )
    db.commit()
    db.close()

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post('/api/v1/jobs/restart-pending')
    assert response.status_code == 200
    payload = response.json()
    assert payload['pending_jobs'] >= 1
    assert payload['queued_jobs'] >= 1
    assert any(entry[0] == 'job-pending-restart' for entry in delayed)


def test_delete_job(tmp_path):
    db = TestingSessionLocal()
    upload = tmp_path / 'u.pdf'
    result = tmp_path / 'r.md'
    upload.write_text('x', encoding='utf-8')
    result.write_text('y', encoding='utf-8')
    job = Job(
        id='job-delete',
        original_filename='x.pdf',
        upload_path=str(upload),
        result_path=str(result),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
    )
    db.add(job)
    db.commit()
    db.close()

    resp = client.delete('/api/v1/jobs/job-delete')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'deleted'
    assert not upload.exists()
    assert not result.exists()


def test_delete_folder_removes_jobs_and_files(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.results_dir.mkdir(parents=True, exist_ok=True)

    folder_upload = settings.uploads_dir / 'finance' / 'q2' / 'job-folder' / 'job-folder.pdf'
    folder_result = settings.results_dir / 'finance' / 'q2' / 'job-folder' / 'job-folder.md'
    folder_upload.parent.mkdir(parents=True, exist_ok=True)
    folder_result.parent.mkdir(parents=True, exist_ok=True)
    folder_upload.write_bytes(b'pdf')
    folder_result.write_text('# markdown', encoding='utf-8')

    db = TestingSessionLocal()
    job = Job(
        id='job-folder',
        original_filename='q2-report.pdf',
        upload_path=str(folder_upload),
        result_path=str(folder_result),
        upload_content=b'pdf',
        upload_mime_type='application/pdf',
        upload_size_bytes=3,
        status=JobStatus.FINISHED,
        processing_info={'settings': {'folder': 'finance', 'subfolder': 'q2', 'storage_folder': 'finance/q2/job-folder'}},
    )
    db.add(job)
    db.commit()
    db.close()

    response = client.delete('/api/v1/folders/finance/q2')
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'finance/q2'
    assert payload['deleted_jobs'] == 1
    assert not (settings.uploads_dir / 'finance' / 'q2').exists()
    assert not (settings.results_dir / 'finance' / 'q2').exists()


def test_download_folder_markdown_zip_recursive_finished_only(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.results_dir.mkdir(parents=True, exist_ok=True)

    result_finished = settings.results_dir / 'finance' / 'q2' / 'job-a' / 'job-a.md'
    result_finished.parent.mkdir(parents=True, exist_ok=True)
    result_finished.write_text('# finished a', encoding='utf-8')

    result_nested = settings.results_dir / 'finance' / 'q2' / 'sub' / 'job-b' / 'job-b.md'
    result_nested.parent.mkdir(parents=True, exist_ok=True)
    result_nested.write_text('# finished b', encoding='utf-8')

    result_failed = settings.results_dir / 'finance' / 'q2' / 'job-c' / 'job-c.md'
    result_failed.parent.mkdir(parents=True, exist_ok=True)
    result_failed.write_text('# failed c', encoding='utf-8')

    db = TestingSessionLocal()
    db.add_all(
        [
            Job(
                id='job-a',
                original_filename='report-a.pdf',
                upload_path=str(tmp_path / 'a.pdf'),
                result_path=str(result_finished),
                upload_content=b'a',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
                processing_info={'settings': {'folder': 'finance', 'subfolder': 'q2', 'storage_folder': 'finance/q2/job-a'}},
            ),
            Job(
                id='job-b',
                original_filename='report-b.pdf',
                upload_path=str(tmp_path / 'b.pdf'),
                result_path=str(result_nested),
                upload_content=b'b',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
                processing_info={'settings': {'folder': 'finance', 'subfolder': 'q2/sub', 'storage_folder': 'finance/q2/sub/job-b'}},
            ),
            Job(
                id='job-c',
                original_filename='report-c.pdf',
                upload_path=str(tmp_path / 'c.pdf'),
                result_path=str(result_failed),
                upload_content=b'c',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FAILED,
                processing_info={'settings': {'folder': 'finance', 'subfolder': 'q2', 'storage_folder': 'finance/q2/job-c'}},
            ),
        ]
    )
    db.commit()
    db.close()

    response = client.get('/api/v1/folders/finance/q2/download')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert len(names) == 2
    assert any(name.endswith('report-a-job-a.md') for name in names)
    assert any(name.endswith('report-b-job-b.md') for name in names)
    assert all('job-c' not in name for name in names)


def test_download_markdown_serves_from_db_when_disk_missing(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    db = TestingSessionLocal()
    job = Job(
        id='job-db-download',
        original_filename='db-only.pdf',
        upload_path=str(tmp_path / 'missing-upload.pdf'),
        result_path=str(tmp_path / 'missing-result.md'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown='# from database',
    )
    db.add(job)
    db.commit()
    db.close()

    # Prove there really is no file backing this job on disk.
    assert not Path(job.result_path).exists()

    response = client.get('/api/v1/jobs/job-db-download/download')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/markdown')
    assert response.headers['content-disposition'] == 'attachment; filename="job-db-download.md"'
    assert response.text == '# from database'


def test_download_markdown_falls_back_to_disk_for_legacy_null_column(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    result_file = tmp_path / 'legacy-result.md'
    result_file.write_text('# legacy disk content', encoding='utf-8')

    db = TestingSessionLocal()
    job = Job(
        id='job-legacy-download',
        original_filename='legacy.pdf',
        upload_path=str(tmp_path / 'legacy-upload.pdf'),
        result_path=str(result_file),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown=None,
    )
    db.add(job)
    db.commit()
    db.close()

    response = client.get('/api/v1/jobs/job-legacy-download/download')
    assert response.status_code == 200
    assert response.text == '# legacy disk content'


def test_download_folder_markdown_serves_from_db_when_disk_missing(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.results_dir.mkdir(parents=True, exist_ok=True)

    db = TestingSessionLocal()
    job = Job(
        id='job-zip-db',
        original_filename='zip-db.pdf',
        upload_path=str(tmp_path / 'missing-upload.pdf'),
        result_path=str(tmp_path / 'missing-result.md'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FINISHED,
        result_markdown='# zip content from db',
        processing_info={
            'settings': {
                'folder': 'finance',
                'subfolder': 'db-only',
                'storage_folder': 'finance/db-only/job-zip-db',
            }
        },
    )
    db.add(job)
    db.commit()
    db.close()

    assert not Path(job.result_path).exists()

    response = client.get('/api/v1/folders/finance/db-only/download')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert len(names) == 1
    assert archive.read(names[0]).decode('utf-8') == '# zip content from db'


def test_jobs_pagination_limit_offset(tmp_path):
    db = TestingSessionLocal()
    db.query(Job).filter(Job.id.like('page-%')).delete(synchronize_session=False)
    db.commit()
    db.add_all(
        [
            Job(
                id=f'page-{i}',
                original_filename=f'page-{i}.pdf',
                upload_path=str(tmp_path / f'page-{i}.pdf'),
                upload_content=b'x',
                upload_mime_type='application/pdf',
                upload_size_bytes=1,
                status=JobStatus.FINISHED,
            )
            for i in range(5)
        ]
    )
    db.commit()
    db.close()

    unbounded_resp = client.get('/api/v1/jobs?q=page-')
    assert unbounded_resp.status_code == 200
    assert len(unbounded_resp.json()['items']) == 5

    page_one_resp = client.get('/api/v1/jobs?q=page-&limit=2')
    assert page_one_resp.status_code == 200
    page_one_items = page_one_resp.json()['items']
    assert len(page_one_items) == 2

    page_two_resp = client.get('/api/v1/jobs?q=page-&limit=2&offset=2')
    assert page_two_resp.status_code == 200
    page_two_items = page_two_resp.json()['items']
    assert len(page_two_items) == 2

    page_one_ids = {item['id'] for item in page_one_items}
    page_two_ids = {item['id'] for item in page_two_items}
    assert page_one_ids.isdisjoint(page_two_ids)

    assert client.get('/api/v1/jobs?limit=-1').status_code == 422
    assert client.get('/api/v1/jobs?offset=-1').status_code == 422
    assert client.get(f'/api/v1/jobs?limit={_JOB_LIST_PAGE_LIMIT_MAX + 1}').status_code == 422

    search_page_resp = client.get('/api/v1/search?q=page-&limit=2')
    assert search_page_resp.status_code == 200
    search_page = search_page_resp.json()
    assert len(search_page['items']) == 2
    # With pagination active, total must be the full match count, not the page size.
    assert search_page['total'] == 5

    search_unbounded = client.get('/api/v1/search?q=page-').json()
    assert len(search_unbounded['items']) == 5
    assert search_unbounded['total'] == 5


def test_list_jobs_defers_blob_columns(tmp_path):
    """Regression test: GET /jobs (and /search) must not eagerly load the
    upload_content / result_markdown blob columns for rows it merely lists.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.api.routes import _job_query

    db = TestingSessionLocal()
    db.query(Job).filter(Job.id == 'job-defer-check').delete()
    db.commit()
    db.add(
        Job(
            id='job-defer-check',
            original_filename='defer-check.pdf',
            upload_path=str(tmp_path / 'defer-check.pdf'),
            upload_content=b'x' * 1000,
            upload_mime_type='application/pdf',
            upload_size_bytes=1000,
            status=JobStatus.FINISHED,
            result_markdown='# some markdown content',
        )
    )
    db.commit()
    db.close()

    query_db = TestingSessionLocal()
    jobs = _job_query(query_db)
    target = next(job for job in jobs if job.id == 'job-defer-check')
    unloaded = sa_inspect(target).unloaded
    assert 'upload_content' in unloaded
    assert 'result_markdown' in unloaded
    query_db.close()


def test_deferred_blob_column_raises_after_session_close(tmp_path):
    """Proves the DetachedInstanceError trap is real for this ORM config:
    a column deferred via query .options() has never been loaded into the
    instance's __dict__, so touching it after the owning session is closed
    must fail loudly instead of silently returning None or stale data. Any
    route that queries with _JOB_BLOB_DEFER_OPTIONS / (defer(upload_content),)
    and then reads that attribute after its `db` dependency has been torn
    down would hit exactly this.
    """
    from sqlalchemy import select
    from sqlalchemy.orm.exc import DetachedInstanceError

    from app.api.routes import _JOB_BLOB_DEFER_OPTIONS

    db = TestingSessionLocal()
    db.query(Job).filter(Job.id == 'job-detached-check').delete()
    db.commit()
    db.add(
        Job(
            id='job-detached-check',
            original_filename='detached-check.pdf',
            upload_path=str(tmp_path / 'detached-check.pdf'),
            upload_content=b'x' * 1000,
            upload_mime_type='application/pdf',
            upload_size_bytes=1000,
            status=JobStatus.FINISHED,
            result_markdown='# detached check',
        )
    )
    db.commit()
    db.close()

    query_db = TestingSessionLocal()
    job = query_db.scalars(select(Job).where(Job.id == 'job-detached-check').options(*_JOB_BLOB_DEFER_OPTIONS)).one()
    query_db.close()

    with pytest.raises(DetachedInstanceError):
        job.result_markdown  # noqa: B018 - intentional attribute access to trigger the lazy load

    with pytest.raises(DetachedInstanceError):
        job.upload_content  # noqa: B018


def test_listing_and_admin_endpoints_survive_populated_blob_columns(monkeypatch, tmp_path):
    """End-to-end regression: with upload_content and result_markdown both
    populated (the realistic post-migration shape, not NULL legacy rows),
    every endpoint that lists/administers jobs via the deferred-blob query
    options must still return 200 through the full FastAPI dependency
    lifecycle (session opened by Depends(get_db), closed only after the
    response body has been serialized). A regression here would surface as
    a 500 from DetachedInstanceError, not a wrong value.
    """
    from app.core.config import settings
    from app.api import routes

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    big_markdown = '# heading\n' + ('lorem ipsum ' * 500)
    db = TestingSessionLocal()
    db.query(Job).filter(Job.id == 'job-populated-blobs').delete()
    db.commit()
    job = Job(
        id='job-populated-blobs',
        original_filename='populated-blobs.pdf',
        upload_path=str(tmp_path / 'populated-blobs.pdf'),
        upload_content=b'\x89PNG' * 500,
        upload_mime_type='application/pdf',
        upload_size_bytes=2000,
        status=JobStatus.FINISHED,
        result_markdown=big_markdown,
        processing_info={
            'settings': {'folder': 'blob-check', 'subfolder': '', 'storage_folder': 'blob-check/job-populated-blobs'},
            'execution': {'page_count': 3},
        },
    )
    db.add(job)
    db.commit()
    db.close()

    jobs_resp = client.get('/api/v1/jobs?q=populated-blobs')
    assert jobs_resp.status_code == 200
    jobs_items = jobs_resp.json()['items']
    assert any(item['id'] == 'job-populated-blobs' for item in jobs_items)
    assert all('result_markdown' not in item and 'upload_content' not in item for item in jobs_items)

    search_resp = client.get('/api/v1/search?q=populated-blobs')
    assert search_resp.status_code == 200
    assert any(item['id'] == 'job-populated-blobs' for item in search_resp.json()['items'])

    stats_resp = client.get('/api/v1/stats')
    assert stats_resp.status_code == 200
    assert stats_resp.json()['processed_pages'] >= 3

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    restart_resp = client.post('/api/v1/folders/blob-check/restart')
    assert restart_resp.status_code == 200
    assert restart_resp.json()['restarted_jobs'] == 1

    verify_db = TestingSessionLocal()
    refreshed = verify_db.get(Job, 'job-populated-blobs')
    assert refreshed is not None
    assert refreshed.status == JobStatus.PENDING
    # _delete_job_outputs clears result_markdown as part of the restart
    # path; confirm the write-only access on a deferred attribute landed
    # correctly rather than silently no-op'ing or raising.
    assert refreshed.result_markdown is None
    verify_db.close()

    delete_resp = client.delete('/api/v1/folders/blob-check')
    assert delete_resp.status_code == 200
    assert delete_resp.json()['deleted_jobs'] == 1


def test_update_paddle_settings(monkeypatch):
    from app.services import paddle_service

    class FakeRedis:
        def __init__(self):
            self.store: dict[str, str] = {}

        def hset(self, _key: str, mapping: dict[str, str]):
            self.store.update(mapping)

        def hgetall(self, _key: str):
            return dict(self.store)

    fake_redis = FakeRedis()

    monkeypatch.setattr(paddle_service, '_redis_client', lambda: fake_redis)
    monkeypatch.setattr(paddle_service, '_runtime_capability', lambda: {
        'torch_available': True,
        'cuda_available': False,
        'selected_device': 'cpu',
        'platform': 'linux-aarch64',
        'no_cuda_reason': 'CPU-only torch installed or no NVIDIA GPU present on this host',
    })

    payload = {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 300,
    }
    response = client.put('/api/v1/paddle/settings', json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body['default_profile'] == 'ppocrv6_tiny'


def test_paddle_status_reports_queue_when_probe_degraded(monkeypatch, tmp_path):
    from app.api import routes

    db = TestingSessionLocal()
    db.add(
        Job(
            id='queue-pending',
            original_filename='queued.pdf',
            upload_path=str(tmp_path / 'queued.pdf'),
            upload_content=b'q',
            upload_mime_type='application/pdf',
            upload_size_bytes=1,
            status=JobStatus.PENDING,
        )
    )
    db.commit()
    db.close()

    monkeypatch.setattr(routes, 'get_paddle_status', lambda: ('stopped', 'Worker unavailable or Paddle probe timed out', None))

    response = client.get('/api/v1/paddle/status')
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'running'
    assert payload['queue_total'] >= 1
    assert payload['pending_jobs'] >= 1


def test_worker_restart_requeues_running_jobs(monkeypatch, tmp_path):
    from app.workers import tasks
    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)

    db = TestingSessionLocal()
    db.query(Job).filter(Job.status == JobStatus.RUNNING).delete()
    db.commit()
    db.add(
        Job(
            id='job-running-restart',
            original_filename='restart.pdf',
            upload_path=str(tmp_path / 'restart.pdf'),
            upload_content=b'r',
            upload_mime_type='application/pdf',
            upload_size_bytes=1,
            status=JobStatus.RUNNING,
            processing_info={
                'settings': {
                    'profile_id': 'ppocrv6_medium',
                    'mode': 'collection',
                    'email': 'ops@example.com',
                    'department': 'ops',
                },
                'execution': {'status': 'running'},
            },
        )
    )
    db.commit()
    db.close()

    delayed: list[tuple] = []
    monkeypatch.setattr(tasks.process_job, 'delay', lambda *args: delayed.append(args))

    restarted = tasks.requeue_running_jobs_after_restart()
    assert restarted >= 1
    assert delayed
    queued_map = {entry[0]: entry for entry in delayed}
    assert 'job-running-restart' in queued_map
    assert queued_map['job-running-restart'][1] == 'ppocrv6_medium'
    assert queued_map['job-running-restart'][2] == 'collection'

    db = TestingSessionLocal()
    job = db.get(Job, 'job-running-restart')
    assert job is not None
    assert job.status == JobStatus.PENDING
    db.close()


def test_upload_rejects_oversize_file_without_partial_remnant(monkeypatch, tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    monkeypatch.setattr(settings, 'max_upload_bytes', 5)

    response = client.post(
        '/api/v1/upload',
        files={'file': ('document.pdf', b'%PDF-well-over-the-limit', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny', 'email': 'oversize@example.com'},
    )
    assert response.status_code == 413

    leftover_files = [path for path in settings.uploads_dir.rglob('*') if path.is_file()]
    assert leftover_files == []


def test_save_upload_rejects_oversize_after_multiple_chunks_no_partial_remnant(monkeypatch, tmp_path):
    """The 413 path in save_upload is only interesting once >1 chunk has
    already been written to disk (the oversize threshold is crossed on a
    later 1MB read, not the first). This exercises that multi-chunk case
    directly against save_upload/UploadFile, bypassing HTTP multipart
    overhead, and checks total_bytes accounting plus full cleanup.
    """
    from app.core.config import settings
    from app.services import storage

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'
    chunk_size = 1024 * 1024
    # Limit sits inside the *second* chunk, so the first chunk is genuinely
    # written to the open handle before the loop detects the overage.
    monkeypatch.setattr(settings, 'max_upload_bytes', int(chunk_size * 1.5))

    data = b'A' * (chunk_size * 3)
    upload = UploadFile(file=io.BytesIO(data), filename='huge.pdf')
    upload.headers = {'content-type': 'application/pdf'}

    with pytest.raises(HTTPException) as exc_info:
        storage.save_upload(upload, 'inbox', 'huge-file-id')

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == 'File too large'

    leftover_files = [path for path in settings.uploads_dir.rglob('*') if path.is_file()]
    assert leftover_files == []


def test_create_folder_writes_keep_marker(tmp_path):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    response = client.post(
        '/api/v1/folders',
        json={'folder': 'finance', 'subfolder': 'q3'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['path'] == 'finance/q3'

    marker = settings.uploads_dir / 'finance' / 'q3' / '.keep'
    assert marker.exists()
    assert marker.is_file()
    assert marker.read_bytes() == b''

    # .keep must not surface as a result in the markdown browser listing.
    listing = client.get('/api/v1/markdown-files')
    assert listing.status_code == 200
    assert all('.keep' not in item['path'] for item in listing.json()['items'])


def test_process_job_deletes_stale_result_before_rewriting(monkeypatch, tmp_path):
    """Regression test for the delete-then-create fix in process_job.

    On Mountpoint-for-S3 there is no reliable overwrite-in-place, so a
    retried/requeued job must unlink any stale result object before writing
    the fresh one. This calls the task body directly (not `.delay`) so the
    real write path executes, and asserts both the call order and that the
    final content reflects the new run rather than the stale one.
    """
    from pathlib import Path

    from app.core.config import settings
    from app.workers import tasks

    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    upload_path = settings.uploads_dir / 'inbox' / 'job-retry.pdf'
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b'%PDF-1.4 fake upload content')

    result_path = (settings.results_dir / 'inbox' / 'job-retry.md').resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('# stale result from a prior attempt', encoding='utf-8')

    db = TestingSessionLocal()
    db.query(Job).filter(Job.id == 'job-retry').delete()
    db.commit()
    db.add(
        Job(
            id='job-retry',
            original_filename='job-retry.pdf',
            upload_path=str(upload_path),
            result_path=str(result_path),
            upload_content=b'%PDF-1.4 fake upload content',
            upload_mime_type='application/pdf',
            upload_size_bytes=len(b'%PDF-1.4 fake upload content'),
            status=JobStatus.PENDING,
            processing_info={'settings': {'storage_folder': 'inbox'}},
        )
    )
    db.commit()
    db.close()

    monkeypatch.setattr(
        tasks,
        'convert_to_markdown_with_details',
        lambda *args, **kwargs: ('# fresh result from this run', {'page_count': 1}),
    )

    call_order: list[str] = []
    original_unlink = Path.unlink
    original_write_text = Path.write_text

    def tracking_unlink(self, *args, **kwargs):
        if self.resolve() == result_path:
            call_order.append('unlink')
        return original_unlink(self, *args, **kwargs)

    def tracking_write_text(self, *args, **kwargs):
        if self.resolve() == result_path:
            call_order.append('write_text')
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', tracking_unlink)
    monkeypatch.setattr(Path, 'write_text', tracking_write_text)

    tasks.process_job('job-retry')

    assert call_order == ['unlink', 'write_text']
    assert result_path.read_text(encoding='utf-8') == '# fresh result from this run'

    db = TestingSessionLocal()
    job = db.get(Job, 'job-retry')
    assert job is not None
    assert job.status == JobStatus.FINISHED
    assert job.error_message is None
    db.close()


def test_create_folder_keep_marker_survives_job_deletion_cleanup(tmp_path):
    """Documents a side effect of the .keep marker: _cleanup_empty_parents
    only rmdir()s directories that are actually empty, so an explicitly
    created upload folder (which now always contains .keep) is never pruned
    after its last job is deleted, while the matching results folder (which
    has no marker) still gets pruned as before.
    """
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    create_response = client.post(
        '/api/v1/folders',
        json={'folder': 'ops', 'subfolder': 'weekly'},
    )
    assert create_response.status_code == 200

    upload_file = settings.uploads_dir / 'ops' / 'weekly' / 'keep-job.pdf'
    result_file = settings.results_dir / 'ops' / 'weekly' / 'keep-job.md'
    upload_file.write_bytes(b'pdf')
    result_file.write_text('# markdown', encoding='utf-8')

    db = TestingSessionLocal()
    db.query(Job).filter(Job.id == 'keep-job').delete()
    db.commit()
    db.add(
        Job(
            id='keep-job',
            original_filename='keep-job.pdf',
            upload_path=str(upload_file),
            result_path=str(result_file),
            upload_content=b'pdf',
            upload_mime_type='application/pdf',
            upload_size_bytes=3,
            status=JobStatus.FINISHED,
            processing_info={'settings': {'folder': 'ops', 'subfolder': 'weekly', 'storage_folder': 'ops/weekly'}},
        )
    )
    db.commit()
    db.close()

    response = client.delete('/api/v1/jobs/keep-job')
    assert response.status_code == 200

    # Upload-side folder persists because of .keep (folder created via API).
    assert (settings.uploads_dir / 'ops' / 'weekly').exists()
    assert (settings.uploads_dir / 'ops' / 'weekly' / '.keep').exists()
    # Results-side folder (no marker) is pruned once empty, as before.
    assert not (settings.results_dir / 'ops' / 'weekly').exists()
