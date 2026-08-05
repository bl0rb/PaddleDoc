"""PR C1 worker tests: the chunked `import_confluence` task.

Drives the task function directly (no broker) against the shared sqlite test
DB: SessionLocal is monkeypatched to conftest's TestingSessionLocal, the
Confluence client is a fake PageSource, and celery_app.send_task is captured
so chunk continuations and child OCR enqueues can be asserted (and replayed
manually to simulate the queue).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, update

from app.core.config import settings
from app.models.models import (
    ImportAuthType,
    ImportRun,
    ImportRunStatus,
    ImportSource,
    Job,
    JobArtifact,
    JobStatus,
)
from app.services import security
from app.services.confluence import AttachmentMeta, ConfluenceError, Page
from app.workers import import_tasks
from app.workers import tasks as worker_tasks
from app.workers.celery_app import celery_app
from app.workers.import_tasks import import_confluence
from conftest import TestingSessionLocal, create_test_user

BASE_URL = 'https://acme.example.com'

PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'fake-png-payload'
PDF_BYTES = b'%PDF-1.4 fake-pdf-payload'
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


def _page(page_id: str, title: str, html: str) -> Page:
    return Page(
        id=page_id,
        title=title,
        version=3,
        html=html,
        url=f'{BASE_URL}/wiki/spaces/KEY/pages/{page_id}/{title.replace(" ", "-")}',
    )


def _attachment(page_id: str, filename: str, data: bytes, *, size_bytes: int | None = None) -> AttachmentMeta:
    return AttachmentMeta(
        id=f'att-{filename}',
        filename=filename,
        media_type='application/octet-stream',
        size_bytes=len(data) if size_bytes is None else size_bytes,
        download_url=f'{BASE_URL}/wiki/download/attachments/{page_id}/{filename}',
        page_id=page_id,
        _fetch=lambda: data,
    )


class FakeClient:
    """In-memory PageSource over a small page tree."""

    def __init__(self, pages: dict[str, Page], children: dict[str, list[str]], attachments=None, root_id: str = '100'):
        self.pages = pages
        self.children = children
        self.attachments = attachments or {}
        self.root_id = root_id
        self.fetched: list[str] = []
        self.on_fetch = None  # optional hook(page_id), for mid-run side effects

    def fetch_page(self, page_id: str) -> Page:
        self.fetched.append(page_id)
        if self.on_fetch is not None:
            self.on_fetch(page_id)
        if page_id not in self.pages:
            raise ConfluenceError(f'Confluence API request returned HTTP 404 for page {page_id}', status_code=404)
        return self.pages[page_id]

    def iter_children(self, page_id: str):
        return iter(self.children.get(page_id, []))

    def iter_attachments(self, page_id: str):
        return iter(self.attachments.get(page_id, []))

    def resolve_space_root(self, space_key: str) -> str:
        return self.root_id


def _three_page_tree() -> FakeClient:
    pages = {
        '100': _page(
            '100',
            'Root Page',
            '<h1>Welcome</h1>'
            f'<p><a href="{BASE_URL}/wiki/spaces/KEY/pages/101/Child-One">Child One</a></p>'
            '<p><img src="/wiki/download/attachments/100/diagram.png" /></p>',
        ),
        '101': _page('101', 'Child One', '<p>First child body</p>'),
        '102': _page('102', 'Child Two', '<p>Second child body</p>'),
    }
    children = {'100': ['101', '102']}
    attachments = {
        '100': [_attachment('100', 'diagram.png', PNG_BYTES)],
        '101': [_attachment('101', 'report.pdf', PDF_BYTES)],
    }
    return FakeClient(pages, children, attachments)


@pytest.fixture()
def sent(monkeypatch):
    """Route the worker DB at the test DB, capture every send_task, and make
    the fake client injectable via `client_holder`."""
    monkeypatch.setattr(import_tasks, 'SessionLocal', TestingSessionLocal)
    monkeypatch.setattr(worker_tasks, 'SessionLocal', TestingSessionLocal)
    captured: list[tuple[str, list]] = []
    monkeypatch.setattr(celery_app, 'send_task', lambda name, args=None, **kw: captured.append((name, list(args or []))))
    return captured


@pytest.fixture()
def client_holder(monkeypatch):
    holder: dict[str, FakeClient] = {}
    monkeypatch.setattr(import_tasks, 'create_client', lambda **kwargs: holder['client'])
    return holder


def _db():
    return TestingSessionLocal()


def _make_owner():
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'importer-{suffix}', email=f'importer-{suffix}@example.com')


def _make_source(owner_id: str, *, credential_encrypted: str | None = None) -> str:
    db = _db()
    try:
        source = ImportSource(
            owner_id=owner_id,
            name='Worker Test Confluence',
            base_url=BASE_URL,
            server_kind='cloud',
            api_base_path='/wiki/api/v2',
            auth_type=ImportAuthType.CLOUD_BASIC,
            auth_username='importer@example.com',
            credential_encrypted=credential_encrypted or security.encrypt_import_credential('api-token'),
        )
        db.add(source)
        db.commit()
        return source.id
    finally:
        db.close()


def _make_run(
    owner_id: str,
    source_id: str | None,
    *,
    scope_type: str = 'page',
    scope_value: str = '100',
    options: dict | None = None,
    **overrides,
) -> str:
    base_options = {
        'max_pages': 50,
        'max_depth': 10,
        'include_attachments': True,
        'ocr_attachments': False,
        'ocr_profile_id': None,
        'folder': '',
        'subfolder': '',
        'tags': [],
        'email': '',
    }
    base_options.update(options or {})
    frontier = [[scope_value, 0]] if scope_type == 'page' else []
    db = _db()
    try:
        run = ImportRun(
            source_id=source_id,
            owner_id=owner_id,
            kind='confluence',
            scope_type=scope_type,
            scope_value=scope_value,
            options=base_options,
            state={'frontier': frontier, 'visited': {}, 'errors': []},
            **overrides,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _get_run(run_id: str) -> ImportRun:
    db = _db()
    try:
        run = db.get(ImportRun, run_id)
        db.refresh(run)
        db.expunge(run)
        return run
    finally:
        db.close()


def _run_jobs(run_id: str) -> list[Job]:
    db = _db()
    try:
        jobs = db.scalars(select(Job).where(Job.import_run_id == run_id).order_by(Job.created_at.asc())).all()
        for job in jobs:
            db.refresh(job)
            db.expunge(job)
        return jobs
    finally:
        db.close()


def _drain_continuations(sent: list[tuple[str, list]], max_rounds: int = 20) -> list[tuple[str, list]]:
    """Replay captured import_confluence continuations (simulating the
    queue); returns the non-continuation sends (e.g. process_job)."""
    others: list[tuple[str, list]] = []
    rounds = 0
    while sent:
        name, args = sent.pop(0)
        if name == 'import_confluence':
            rounds += 1
            assert rounds <= max_rounds, 'continuation loop did not terminate'
            import_confluence(*args)
        else:
            others.append((name, args))
    return others


# --- Registration -------------------------------------------------------------

def test_import_confluence_registered_in_celery_app() -> None:
    # tasks.py must import the module for `celery -A app.workers.tasks` to
    # see it; importing worker_tasks (done at module top) is the trigger.
    assert 'import_confluence' in celery_app.tasks
    assert worker_tasks is not None


# --- Happy path ----------------------------------------------------------------

def test_happy_path_page_tree_imports_all_pages(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    import_confluence(run_id, 0)
    others = _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.finished_at is not None
    assert run.started_at is not None
    assert run.pages_imported == 3
    assert run.pages_discovered == 3
    assert run.pages_failed == 0
    assert run.attachments_saved == 2
    assert run.artifact_bytes == len(PNG_BYTES) + len(PDF_BYTES)
    expected_content = sum(len(p.html.encode('utf-8')) for p in client.pages.values())
    assert run.content_bytes == expected_content
    assert run.root_page_title == 'Root Page'
    assert run.current_page_title == ''
    assert others == []  # ocr_attachments off: no child OCR enqueues

    jobs = _run_jobs(run_id)
    assert len(jobs) == 3
    by_title = {}
    for job in jobs:
        assert job.status == JobStatus.FINISHED
        assert job.owner_id == owner.id
        assert job.upload_path.endswith(f'/{job.id}.html')
        assert job.upload_mime_type == 'text/html'
        assert job.upload_content  # original export_view HTML retained
        assert job.result_markdown.startswith('---\n')
        settings_info = job.processing_info['settings']
        assert settings_info['mode'] == 'import'
        # Default folder groups the run for the folder browser.
        assert settings_info['folder'] == 'imports/root-page'
        assert settings_info['import']['source_page_id'] in client.pages
        assert job.processing_info['execution'] == {'status': 'finished', 'engine': 'confluence-import'}
        by_title[settings_info['import']['source_page_id']] = job

    root_job = by_title['100']
    assert root_job.original_filename == 'root-page.md'
    assert 'title: Root Page' in root_job.result_markdown
    # Inline image rewritten to the stored artifact reference.
    assert 'artifacts/diagram.png' in root_job.result_markdown
    # Cross-page link rewritten to the internal job link by the finalize pass.
    assert f'/jobs/{by_title["101"].id}' in root_job.result_markdown
    assert '/pages/101/' not in root_job.result_markdown

    db = _db()
    try:
        artifacts = db.scalars(select(JobArtifact).where(JobArtifact.job_id == root_job.id)).all()
        assert [(a.filename, a.kind, a.content_type) for a in artifacts] == [('diagram.png', 'image', 'image/png')]
        pdf_artifacts = db.scalars(select(JobArtifact).where(JobArtifact.job_id == by_title['101'].id)).all()
        assert [(a.filename, a.kind, a.content_type) for a in pdf_artifacts] == [
            ('report.pdf', 'attachment', 'application/pdf')
        ]
    finally:
        db.close()


def test_space_scope_resolves_root_on_first_chunk(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), scope_type='space', scope_value='KEY')

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 3
    jobs = _run_jobs(run_id)
    # Space runs default the folder to the space-key slug.
    assert all(j.processing_info['settings']['folder'] == 'imports/key' for j in jobs)


# --- Lease claim / reclaim / duplicate delivery --------------------------------

def test_stale_lease_reclaim_resumes_partially_progressed_run(sent, client_holder) -> None:
    # The spec-3 resume contract against a run with REAL partial progress:
    # page 100 was already imported and committed by the dead execution, the
    # persisted frontier holds the remaining children. The reclaim must
    # import exactly the remaining pages -- no duplicate Job for a visited
    # page, no re-fetch of it, counters advancing from the persisted values.
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    imported_job_id = str(uuid.uuid4())
    db = _db()
    try:
        db.add(
            Job(
                id=imported_job_id,
                original_filename='root-page.md',
                upload_path=f'imports/root-page/{imported_job_id}/{imported_job_id}.html',
                status=JobStatus.FINISHED,
                owner_id=owner.id,
                import_run_id=run_id,
                result_markdown='---\ntitle: Root Page\n---\n\nbody\n',
                processing_info={'settings': {'mode': 'import', 'import': {'source_page_id': '100'}}},
            )
        )
        db.commit()
    finally:
        db.close()

    # Simulate a worker that claimed chunk 0 (chunk_seq now 1), imported page
    # 100 + discovered its children, then died: the redelivered message still
    # carries chunk_seq=0, and the heartbeat is old.
    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_run_seconds * 2)
    db = _db()
    try:
        db.execute(
            update(ImportRun)
            .where(ImportRun.id == run_id)
            .values(
                status=ImportRunStatus.RUNNING,
                chunk_seq=1,
                started_at=stale,
                updated_at=stale,
                pages_imported=1,
                pages_discovered=3,
                root_page_title='Root Page',
                state={'frontier': [['101', 1], ['102', 1]], 'visited': {'100': imported_job_id}, 'errors': []},
            )
        )
        db.commit()
    finally:
        db.close()

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 3
    assert run.pages_failed == 0
    assert run.chunk_seq >= 2  # reclaim re-incremented the lease
    # The already-imported page was neither re-fetched nor re-imported.
    assert client.fetched == ['101', '102']
    jobs = _run_jobs(run_id)
    assert len(jobs) == 3
    page_100_jobs = [
        j for j in jobs if j.processing_info['settings']['import']['source_page_id'] == '100'
    ]
    assert [j.id for j in page_100_jobs] == [imported_job_id]


def test_duplicate_delivery_with_live_owner_is_a_noop(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    db = _db()
    try:
        db.execute(
            update(ImportRun)
            .where(ImportRun.id == run_id)
            .values(status=ImportRunStatus.RUNNING, chunk_seq=1, updated_at=datetime.now(timezone.utc))
        )
        db.commit()
    finally:
        db.close()

    import_confluence(run_id, 0)  # stale message while the owner is healthy

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.RUNNING
    assert run.chunk_seq == 1
    assert client.fetched == []  # no page was touched
    assert sent == []
    assert _run_jobs(run_id) == []


def test_run_flipped_terminal_mid_chunk_stops_execution_without_resurrection(sent, client_holder) -> None:
    # The API cap-reaper can flip a stale-looking run to FAILED while its
    # worker is actually alive. The next lease-guarded commit must observe
    # that (status no longer 'running'), abandon all pending writes, and
    # never resurrect the run to FINISHED.
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    def reap(page_id: str) -> None:
        if page_id == '101':
            db = _db()
            try:
                db.execute(
                    update(ImportRun)
                    .where(ImportRun.id == run_id)
                    .values(status=ImportRunStatus.FAILED, error_message='worker lost; run stalled')
                )
                db.commit()
            finally:
                db.close()

    client.on_fetch = reap

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FAILED  # not resurrected
    assert run.error_message == 'worker lost; run stalled'
    # Page 100 was committed before the flip; page 101's partial work was
    # rolled back by the lease guard.
    jobs = _run_jobs(run_id)
    assert [j.processing_info['settings']['import']['source_page_id'] for j in jobs] == ['100']


# --- Soft time limit ------------------------------------------------------------

def test_soft_time_limit_requeues_and_reimports_in_flight_page(sent, client_holder) -> None:
    # Spec 3 resume contract: the page that was in flight when the soft limit
    # fired must be re-imported by the continuation, not silently dropped
    # (it was already popped from the in-memory frontier).
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    armed = {'fire': True}

    def explode(page_id: str) -> None:
        if page_id == '101' and armed['fire']:
            armed['fire'] = False
            raise SoftTimeLimitExceeded()

    client.on_fetch = explode

    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.RUNNING
    assert run.pages_imported == 1
    # The in-flight page is back at the head of the persisted frontier and
    # not falsely marked visited.
    assert run.state['frontier'][0] == ['101', 1]
    assert '101' not in run.state['visited']
    assert sent == [('import_confluence', [run_id, 1])]

    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 3
    assert run.pages_failed == 0
    assert client.fetched.count('101') == 2  # aborted attempt + re-import
    jobs = _run_jobs(run_id)
    page_ids = [j.processing_info['settings']['import']['source_page_id'] for j in jobs]
    assert sorted(page_ids) == ['100', '101', '102']  # exactly once each


# --- Cancel --------------------------------------------------------------------

def test_cancel_before_first_chunk_cancels_without_fetching(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), cancel_requested=True)

    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.CANCELLED
    assert run.finished_at is not None
    assert client.fetched == []


def test_cancel_mid_run_stops_between_pages_and_keeps_jobs(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    def flip_cancel(page_id: str) -> None:
        if page_id == '100':
            db = _db()
            try:
                db.execute(update(ImportRun).where(ImportRun.id == run_id).values(cancel_requested=True))
                db.commit()
            finally:
                db.close()

    client.on_fetch = flip_cancel

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.CANCELLED
    assert run.finished_at is not None
    assert run.pages_imported == 1  # root imported, children never fetched
    assert client.fetched == ['100']
    assert len(_run_jobs(run_id)) == 1  # created jobs are kept


# --- Caps ----------------------------------------------------------------------

def test_page_cap_stops_discovery_and_finishes(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), options={'max_pages': 2})

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 2
    assert run.pages_discovered == 2
    assert len(_run_jobs(run_id)) == 2


def test_byte_cap_finishes_partially_with_note(sent, client_holder, monkeypatch) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))
    monkeypatch.setattr(settings, 'import_run_max_total_bytes', 10)

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    # Terminal state per spec 5.4: the cap ends discovery gracefully -- the
    # run FINISHES (not fails) with a note in state.errors.
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 0
    assert run.state['frontier'] == []
    assert any('byte cap' in e['error'] for e in run.state['errors'])


def test_max_depth_zero_imports_only_the_root(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), options={'max_depth': 0})

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 1


# --- Chunking ------------------------------------------------------------------

def test_chunk_continuation_re_enqueues_with_new_seq(sent, client_holder, monkeypatch) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))
    monkeypatch.setattr(settings, 'import_chunk_pages', 1)

    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.RUNNING
    assert run.pages_imported == 1
    assert run.chunk_seq == 1
    assert sent == [('import_confluence', [run_id, 1])]

    _drain_continuations(sent)
    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 3
    # 3 pages at 1 page/chunk: seq incremented once per chunk execution.
    assert run.chunk_seq >= 3


# --- Attachments ---------------------------------------------------------------

def test_ocr_attachment_spawns_child_job(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(
        owner.id,
        _make_source(owner.id),
        options={'ocr_attachments': True, 'ocr_profile_id': 'ppocrv6_small'},
    )

    import_confluence(run_id, 0)
    others = _drain_continuations(sent)

    jobs = _run_jobs(run_id)
    children = [j for j in jobs if j.processing_info['settings']['mode'] == 'import_attachment']
    assert len(children) == 1
    child = children[0]
    assert child.status == JobStatus.PENDING
    assert child.original_filename == 'report.pdf'
    assert child.upload_path.endswith(f'/{child.id}.pdf')  # real extension: _resolve_upload_path needs the suffix
    assert child.upload_content == PDF_BYTES
    assert child.upload_mime_type == 'application/pdf'
    parent_id = child.processing_info['settings']['import']['parent_job_id']
    parent = next(j for j in jobs if j.id == parent_id)
    assert parent.processing_info['settings']['import']['source_page_id'] == '101'
    # Enqueued through the existing OCR pipeline once at the page commit and
    # once more by the finalize backstop sweep for still-PENDING children
    # (idempotent: process_job's PENDING->RUNNING claim absorbs duplicates).
    assert others == [('process_job', [child.id, 'ppocrv6_small', 'import_attachment', '', None])] * 2
    # The png inline image is stored as kind='image' and does NOT spawn a child.
    db = _db()
    try:
        image_kinds = db.scalars(select(JobArtifact.kind).where(JobArtifact.filename == 'diagram.png')).all()
        assert 'image' in image_kinds
    finally:
        db.close()


def test_attachment_rules_svg_never_image_and_magic_bytes_enforced(sent, client_holder) -> None:
    pages = {'200': _page('200', 'Attachment Rules', '<p>body</p>')}
    attachments = {
        '200': [
            _attachment('200', 'vector.svg', SVG_BYTES),
            _attachment('200', 'fake.png', b'not-a-real-png'),
            _attachment('200', 'notes.txt', b'plain text notes'),
        ]
    }
    client = FakeClient(pages, {}, attachments)
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), scope_value='200')

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    job = _run_jobs(run_id)[0]
    db = _db()
    try:
        artifacts = db.scalars(select(JobArtifact).where(JobArtifact.job_id == job.id)).all()
        stored = {a.filename: (a.kind, a.content_type) for a in artifacts}
    finally:
        db.close()
    # SVG stored, but never as kind='image' (inline-SVG XSS vector).
    assert stored['vector.svg'] == ('attachment', 'image/svg+xml')
    # .png whose bytes are not a PNG is skipped entirely.
    assert 'fake.png' not in stored
    assert stored['notes.txt'] == ('attachment', 'text/plain')
    assert run.attachments_saved == 2


def test_include_attachments_false_skips_storage_and_keeps_absolute_image_urls(sent, client_holder) -> None:
    client = _three_page_tree()
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), options={'include_attachments': False})

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.attachments_saved == 0
    assert run.artifact_bytes == 0
    jobs = _run_jobs(run_id)
    root = next(j for j in jobs if j.processing_info['settings']['import']['source_page_id'] == '100')
    # No artifacts will ever exist for this run, so the inline image keeps
    # its absolute source URL instead of a dangling artifacts/ reference.
    assert 'artifacts/' not in root.result_markdown
    assert f'{BASE_URL}/wiki/download/attachments/100/diagram.png' in root.result_markdown
    db = _db()
    try:
        assert db.scalars(select(JobArtifact).where(JobArtifact.job_id.in_([j.id for j in jobs]))).all() == []
    finally:
        db.close()


def test_colliding_sanitized_filenames_deduped_with_visible_note(sent, client_holder) -> None:
    # 'a b.png' and 'a_b.png' both sanitize to 'a_b.png'; the second is
    # stored under a '-2' suffix which the converted markdown cannot know
    # about, so the rename must at least be surfaced in state.errors.
    pages = {'400': _page('400', 'Dupes', '<p>body</p>')}
    attachments = {
        '400': [
            _attachment('400', 'a b.png', PNG_BYTES),
            _attachment('400', 'a_b.png', PNG_BYTES),
        ]
    }
    client = FakeClient(pages, {}, attachments)
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), scope_value='400')

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    job = _run_jobs(run_id)[0]
    db = _db()
    try:
        stored = sorted(db.scalars(select(JobArtifact.filename).where(JobArtifact.job_id == job.id)).all())
    finally:
        db.close()
    assert stored == ['a_b-2.png', 'a_b.png']
    assert any("'a_b-2.png'" in e['error'] for e in run.state['errors'])


def test_oversized_attachment_skipped_with_error_note(sent, client_holder, monkeypatch) -> None:
    pages = {'300': _page('300', 'Big Attachment', '<p>body</p>')}
    attachments = {'300': [_attachment('300', 'big.pdf', PDF_BYTES, size_bytes=10_000_000)]}
    client = FakeClient(pages, {}, attachments)
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id), scope_value='300')
    monkeypatch.setattr(settings, 'import_attachment_max_bytes', 1_000_000)

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED  # skips never fail the page
    assert run.attachments_saved == 0
    assert any('per-attachment limit' in e['error'] for e in run.state['errors'])


# --- Per-page failure ----------------------------------------------------------

def test_missing_page_fails_that_page_only(sent, client_holder) -> None:
    client = _three_page_tree()
    del client.pages['102']  # 404s when fetched
    client_holder['client'] = client
    owner = _make_owner()
    run_id = _make_run(owner.id, _make_source(owner.id))

    import_confluence(run_id, 0)
    _drain_continuations(sent)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 2
    assert run.pages_failed == 1
    assert any(e['page_id'] == '102' for e in run.state['errors'])
    assert run.state['visited']['102'] is None  # not retried on resume


# --- Setup failures ------------------------------------------------------------

def test_deleted_source_fails_run_cleanly(sent, client_holder) -> None:
    client_holder['client'] = _three_page_tree()
    owner = _make_owner()
    run_id = _make_run(owner.id, None)

    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FAILED
    assert 'source was deleted' in run.error_message
    assert run.finished_at is not None
    assert sent == []


def test_undecryptable_credential_fails_run_with_actionable_message(sent, client_holder) -> None:
    client_holder['client'] = _three_page_tree()
    owner = _make_owner()
    source_id = _make_source(owner.id, credential_encrypted='not-a-fernet-token')
    run_id = _make_run(owner.id, source_id)

    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FAILED
    assert 're-enter' in run.error_message
    # The credential ciphertext never leaks into the error surface.
    assert 'not-a-fernet-token' not in run.error_message


# --- Worker-ready backstop ------------------------------------------------------

def test_worker_restart_requeues_stale_import_runs(sent, client_holder, monkeypatch) -> None:
    owner = _make_owner()
    stale_run_id = _make_run(owner.id, _make_source(owner.id))
    fresh_run_id = _make_run(owner.id, _make_source(owner.id))
    # A PENDING run whose creation message was lost (send_task failed):
    # updated_at is still its creation time, chunk_seq still 0.
    lost_pending_run_id = _make_run(owner.id, _make_source(owner.id))
    stale = datetime.now(timezone.utc) - timedelta(seconds=settings.import_stale_run_seconds * 2)
    db = _db()
    try:
        db.execute(
            update(ImportRun)
            .where(ImportRun.id == stale_run_id)
            .values(status=ImportRunStatus.RUNNING, chunk_seq=3, updated_at=stale)
        )
        db.execute(
            update(ImportRun)
            .where(ImportRun.id == fresh_run_id)
            .values(status=ImportRunStatus.RUNNING, chunk_seq=1, updated_at=datetime.now(timezone.utc))
        )
        db.execute(
            update(ImportRun).where(ImportRun.id == lost_pending_run_id).values(updated_at=stale)
        )
        db.commit()
    finally:
        db.close()
    # Keep the pre-existing RUNNING-job recovery from touching the broker.
    monkeypatch.setattr(worker_tasks.process_job, 'delay', lambda *args, **kwargs: None)

    worker_tasks.requeue_running_jobs_after_restart()

    # Stale RUNNING: replayed with the PREVIOUS seq so the stale-lease
    # reclaim path claims it. Stale PENDING: replayed with the CURRENT seq
    # (the normal claim path accepts pending directly). The healthy run is
    # left alone.
    assert ('import_confluence', [stale_run_id, 2]) in sent
    assert ('import_confluence', [lost_pending_run_id, 0]) in sent
    assert all(args[0] != fresh_run_id for name, args in sent if name == 'import_confluence')
