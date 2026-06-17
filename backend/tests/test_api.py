import io
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import get_db
from app.main import app
from app.models.models import Base, Job, JobStatus

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


def test_markdown_browser_lists_files(tmp_path, monkeypatch):
    from app.core.config import settings

    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    result_one = settings.results_dir / 'single' / 'job-1' / 'job-1.md'
    result_one.parent.mkdir(parents=True, exist_ok=True)
    result_one.write_text('# single', encoding='utf-8')

    result_two = settings.results_dir / 'collections' / 'collection-1' / 'job-2' / 'job-2.md'
    result_two.parent.mkdir(parents=True, exist_ok=True)
    result_two.write_text('# collection', encoding='utf-8')

    list_resp = client.get('/api/v1/markdown-files')
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert len(payload['items']) == 2
    assert any(item['path'] == 'single/job-1/job-1.md' for item in payload['items'])
    assert any(item['path'] == 'collections/collection-1/job-2/job-2.md' for item in payload['items'])

    file_resp = client.get('/api/v1/markdown-files/single/job-1/job-1.md')
    assert file_resp.status_code == 200
    assert '# single' in file_resp.text


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
