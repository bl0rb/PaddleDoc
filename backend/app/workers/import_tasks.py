"""Chunked Confluence-import Celery task (spec §3).

One execution processes at most `import_chunk_pages` pages, persists the
crawl frontier/visited map on the ImportRun row, and re-enqueues itself for
the next chunk -- so OCR jobs interleave on the shared queue and each
execution stays well under the Celery time limits. `chunk_seq` is an
optimistic lease: each execution's claim UPDATE increments it, a stale-lease
reclaim recovers runs whose worker died mid-chunk (acks_late redelivery),
and duplicate deliveries while the owner is alive no-op silently.

Registered from app/workers/tasks.py (the `celery -A app.workers.tasks`
entrypoint) via an explicit import; the API enqueues by name only
(`import_confluence`).
"""

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select, update
from sqlalchemy.orm import defer

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import ImportRun, ImportRunStatus, ImportSource, Job, JobArtifact, JobStatus, Tag
from app.services import security
from app.services.confluence import AttachmentMeta, ConfluenceError, Page, PageSource, create_client
from app.services.confluence_markdown import convert_page, rewrite_cross_page_links, sanitize_filename
from app.workers.celery_app import celery_app


logger = logging.getLogger(__name__)

IMPORT_TASK_NAME = 'import_confluence'

_ERROR_MESSAGE_MAX_CHARS = 2000
_ERROR_ENTRY_MAX_CHARS = 500
_ERROR_LIST_MAX_ENTRIES = 200

# Our classification only (extension + leading magic bytes); the remote
# Content-Type header / API media type is never trusted (§2.2 rule 5).
_IMAGE_SPECS: dict[str, tuple[str, tuple[bytes, ...]]] = {
    '.png': ('image/png', (b'\x89PNG\r\n\x1a\n',)),
    '.jpg': ('image/jpeg', (b'\xff\xd8\xff',)),
    '.jpeg': ('image/jpeg', (b'\xff\xd8\xff',)),
    '.gif': ('image/gif', (b'GIF87a', b'GIF89a')),
    '.webp': ('image/webp', ()),  # RIFF....WEBP, checked specially
}
# The existing OCR upload allowlist minus the inline-image types (which rule 2
# claims first); these may additionally spawn a child OCR job.
_OCR_DOC_SPECS: dict[str, tuple[str, tuple[bytes, ...]]] = {
    '.pdf': ('application/pdf', (b'%PDF',)),
    '.docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', (b'PK\x03\x04',)),
    '.pptx': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', (b'PK\x03\x04',)),
    '.xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', (b'PK\x03\x04',)),
    '.xls': ('application/vnd.ms-excel', (b'\xd0\xcf\x11\xe0',)),
}
_TEXT_SPECS: dict[str, str] = {
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.log': 'text/plain',
    '.json': 'application/json',
}


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + '...'


def _slug(value: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9]+', '-', value).strip('-').lower()
    return (slug or 'page')[:80]


def _classify_attachment(filename: str, data: bytes) -> tuple[str, str, str] | None:
    """(kind, content_type, extension) per the ordered §2.2 attachment rules,
    or None to skip. An extension that claims a validated binary type but
    fails its magic-byte check is skipped outright rather than stored under
    a type it demonstrably is not."""
    name = filename.rsplit('/', 1)[-1]
    dot = name.rfind('.')
    ext = name[dot:].lower() if dot > 0 else ''

    if ext in _IMAGE_SPECS:
        content_type, magics = _IMAGE_SPECS[ext]
        if ext == '.webp':
            valid = data[:4] == b'RIFF' and data[8:12] == b'WEBP'
        else:
            valid = any(data.startswith(magic) for magic in magics)
        return ('image', content_type, ext) if valid else None

    if ext in _OCR_DOC_SPECS:
        content_type, magics = _OCR_DOC_SPECS[ext]
        if any(data.startswith(magic) for magic in magics):
            return 'attachment', content_type, ext
        return None

    if ext == '.svg':
        # Never kind='image' (inline-SVG XSS vector, §2.2 rule 2): stored as a
        # plain attachment, served attachment-disposition + nosniff.
        return 'attachment', 'image/svg+xml', ext

    if ext in _TEXT_SPECS:
        return 'attachment', _TEXT_SPECS[ext], ext

    return None


def _dedupe_filename(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition('.')
    base, extension = (stem, f'.{ext}') if dot else (name, '')
    counter = 2
    while f'{base}-{counter}{extension}' in used:
        counter += 1
    final = f'{base}-{counter}{extension}'
    used.add(final)
    return final


def _attach_tags(db, job: Job, tags: list[str]) -> None:
    # Minimal local copy of routes._attach_tags: the worker must not import
    # the API module.
    names = [tag for tag in tags if isinstance(tag, str) and tag]
    if not names:
        return
    existing = {tag.name: tag for tag in db.scalars(select(Tag).where(Tag.name.in_(names))).all()}
    for tag_name in names:
        tag_obj = existing.get(tag_name)
        if tag_obj is None:
            tag_obj = Tag(name=tag_name)
            db.add(tag_obj)
            existing[tag_name] = tag_obj
        if tag_obj not in job.tags:
            job.tags.append(tag_obj)


def _default_folder(run: ImportRun) -> str:
    # Spec §1.4: folder defaults to imports/<space-or-root-slug> so the
    # folder browser groups a run.
    base = run.scope_value if run.scope_type == 'space' else (run.root_page_title or run.scope_value)
    return f'imports/{_slug(base)}'


class _RunState:
    """Mutable view over ImportRun.state (plain JSON column: whole-dict
    reassignment on every persist, per the model contract)."""

    def __init__(self, run: ImportRun) -> None:
        raw = run.state if isinstance(run.state, dict) else {}
        frontier = raw.get('frontier') if isinstance(raw.get('frontier'), list) else []
        self.frontier: list[list] = [list(entry) for entry in frontier if isinstance(entry, (list, tuple)) and len(entry) == 2]
        visited = raw.get('visited') if isinstance(raw.get('visited'), dict) else {}
        self.visited: dict[str, str | None] = dict(visited)
        errors = raw.get('errors') if isinstance(raw.get('errors'), list) else []
        self.errors: list[dict] = [entry for entry in errors if isinstance(entry, dict)]

    def add_error(self, page_id: str, title: str, error: str) -> None:
        if len(self.errors) < _ERROR_LIST_MAX_ENTRIES:
            self.errors.append(
                {
                    'page_id': str(page_id),
                    'title': _truncate(str(title), _ERROR_ENTRY_MAX_CHARS),
                    'error': _truncate(str(error), _ERROR_ENTRY_MAX_CHARS),
                }
            )

    def persist(self, run: ImportRun) -> None:
        # Copies, not references: run.state must never share list/dict objects
        # with this instance, or later in-place mutations (frontier.pop/append,
        # add_error) would mutate the committed value too and SQLAlchemy's
        # equality-based change detection would silently skip the UPDATE.
        run.state = {
            'frontier': [list(entry) for entry in self.frontier],
            'visited': dict(self.visited),
            'errors': [dict(entry) for entry in self.errors],
        }


class _LeaseLost(Exception):
    """Another execution reclaimed this run's chunk lease, or an API backstop
    force-terminated the run. Every further write from this execution must
    be abandoned -- the run's state belongs to someone else now."""


def _claim_chunk(db, run_id: str, expected_seq: int, now: datetime) -> int | None:
    """Optimistic lease claim + stale-lease reclaim (mirrors process_job's
    stale-RUNNING recovery). Commits and returns the claimed chunk_seq on
    success (every later write is guarded on it via _commit_owned); None when
    another live execution owns the run or it is terminal."""
    claimed = db.execute(
        update(ImportRun)
        .where(ImportRun.id == run_id)
        .where(ImportRun.chunk_seq == expected_seq)
        .where(ImportRun.status.in_([ImportRunStatus.PENDING, ImportRunStatus.RUNNING]))
        .values(
            chunk_seq=ImportRun.chunk_seq + 1,
            status=ImportRunStatus.RUNNING,
            started_at=func.coalesce(ImportRun.started_at, now),
            updated_at=now,
        )
    )
    if claimed.rowcount:
        db.commit()
        return expected_seq + 1
    # The previous execution claimed chunk expected+1 and then died
    # (acks_late redelivered this message with the old seq). Reclaim only
    # when the heartbeat is stale -- a fresh updated_at means another
    # live execution owns the run and this delivery must no-op.
    stale_cutoff = now - timedelta(seconds=settings.import_stale_run_seconds)
    claimed = db.execute(
        update(ImportRun)
        .where(ImportRun.id == run_id)
        .where(ImportRun.chunk_seq == expected_seq + 1)
        .where(ImportRun.status == ImportRunStatus.RUNNING)
        .where(ImportRun.updated_at < stale_cutoff)
        .values(chunk_seq=ImportRun.chunk_seq + 1, updated_at=now)
    )
    if claimed.rowcount:
        db.commit()
        return expected_seq + 2
    db.rollback()
    return None


def _commit_owned(db, run_id: str, claimed_seq: int) -> None:
    """Flush + commit the session's pending work, but only while this
    execution still owns the chunk lease AND the run is still 'running' in
    the DB. Doubles as the staleness heartbeat (touches updated_at inside the
    same transaction). no_autoflush so the guard reads the committed DB row,
    not this session's pending terminal transition. On a lost lease (stale
    reclaim by another execution, or the API cap-reaper/force-cancel flipping
    the run terminal) everything pending is rolled back and _LeaseLost is
    raised -- a superseded execution can never write."""
    with db.no_autoflush:
        owned = db.execute(
            update(ImportRun)
            .where(ImportRun.id == run_id)
            .where(ImportRun.chunk_seq == claimed_seq)
            .where(ImportRun.status == ImportRunStatus.RUNNING)
            .values(updated_at=datetime.now(timezone.utc))
        )
    if not owned.rowcount:
        db.rollback()
        raise _LeaseLost()
    db.commit()


def _fail_run(db, run: ImportRun, state: _RunState, message: str, claimed_seq: int) -> None:
    run.status = ImportRunStatus.FAILED
    run.error_message = _truncate(message, _ERROR_MESSAGE_MAX_CHARS)
    run.finished_at = datetime.now(timezone.utc)
    run.current_page_title = ''
    state.persist(run)
    _commit_owned(db, run.id, claimed_seq)


def _cancel_run(db, run: ImportRun, state: _RunState, claimed_seq: int) -> None:
    run.status = ImportRunStatus.CANCELLED
    run.cancel_requested = True
    run.finished_at = datetime.now(timezone.utc)
    run.current_page_title = ''
    state.persist(run)
    _commit_owned(db, run.id, claimed_seq)


def _finalize_run(db, run: ImportRun, state: _RunState, claimed_seq: int) -> None:
    """End-of-run cross-page link rewrite (§2.2) + terminal transition +
    re-enqueue backstop for stranded attachment-OCR children."""
    mapping = {str(page_id): job_id for page_id, job_id in state.visited.items() if job_id}
    if mapping:
        page_jobs = db.scalars(
            select(Job)
            .where(Job.import_run_id == run.id)
            .where(Job.result_markdown.is_not(None))
            .options(defer(Job.upload_content))
        ).all()
        for job in page_jobs:
            info = job.processing_info if isinstance(job.processing_info, dict) else {}
            job_settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}
            if job_settings.get('mode') != 'import':
                continue
            rewritten = rewrite_cross_page_links(job.result_markdown or '', mapping)
            if rewritten != job.result_markdown:
                job.result_markdown = rewritten

    run.status = ImportRunStatus.FINISHED
    run.finished_at = datetime.now(timezone.utc)
    run.current_page_title = ''
    state.persist(run)
    _commit_owned(db, run.id, claimed_seq)

    # Backstop for child OCR jobs that were committed but never enqueued (a
    # crash between a per-page commit and its send_task, or a heartbeat
    # commit by an execution that later lost its lease): re-send every
    # still-PENDING child of this run. process_job's PENDING->RUNNING claim
    # UPDATE makes a duplicate send for an already-enqueued child a no-op.
    options = run.options if isinstance(run.options, dict) else {}
    pending_children = db.scalars(
        select(Job.id).where(Job.import_run_id == run.id).where(Job.status == JobStatus.PENDING)
    ).all()
    for child_id in pending_children:
        celery_app.send_task(
            'process_job',
            args=[child_id, options.get('ocr_profile_id'), 'import_attachment', options.get('email') or '', None],
        )


def _cancel_requested(db, run_id: str) -> bool:
    # Fresh column read (bypasses the identity map) so a cancel issued by the
    # API between pages is observed without expiring the run instance.
    return bool(db.execute(select(ImportRun.cancel_requested).where(ImportRun.id == run_id)).scalar())


def _store_attachments(
    db,
    run: ImportRun,
    state: _RunState,
    job: Job,
    page: Page,
    client: PageSource,
    options: dict,
    folder: str,
    subfolder: str,
    claimed_seq: int,
) -> list[str]:
    """Apply the ordered §2.2 attachment rules for one page. Returns the ids
    of created child OCR jobs (to enqueue after the page commit). Attachment
    problems are recorded as run errors and never fail the page.

    Commits (lease-guarded) after every download attempt: the attachment
    phase is the one stretch of a page whose duration is unbounded relative
    to import_stale_run_seconds (up to 50 downloads with a per-socket-op
    timeout each), so it must heartbeat -- and the heartbeat's lease guard
    aborts this execution the moment another one reclaims the run. The page
    is already in state.visited by the time this runs, so the partial
    commits can never cause a duplicate re-import."""
    child_job_ids: list[str] = []
    used_names: set[str] = set()
    per_attachment_cap = settings.import_attachment_max_bytes
    total_cap = settings.import_run_max_total_bytes

    try:
        attachments: list[AttachmentMeta] = list(client.iter_attachments(page.id))
    except ConfluenceError as exc:
        state.add_error(page.id, page.title, f'attachment listing failed: {exc}')
        return child_job_ids

    for attachment in attachments:
        display_name = attachment.filename or attachment.id
        claimed_size = attachment.size_bytes or 0
        if claimed_size > per_attachment_cap:
            state.add_error(page.id, page.title, f'attachment {display_name!r} skipped: larger than the per-attachment limit')
            continue
        if run.artifact_bytes + run.content_bytes + claimed_size > total_cap:
            state.add_error(page.id, page.title, f'attachment {display_name!r} skipped: run byte cap reached')
            continue

        try:
            data = attachment.fetch_bytes()
        except ConfluenceError as exc:
            state.add_error(page.id, page.title, f'attachment {display_name!r} download failed: {exc}')
            data = None
        # Heartbeat after every download attempt (success or failure), lease-
        # guarded; raises _LeaseLost if this execution was superseded.
        state.persist(run)
        _commit_owned(db, run.id, claimed_seq)
        if data is None:
            continue
        if len(data) > per_attachment_cap:
            state.add_error(page.id, page.title, f'attachment {display_name!r} skipped: larger than the per-attachment limit')
            continue
        if run.artifact_bytes + run.content_bytes + len(data) > total_cap:
            state.add_error(page.id, page.title, f'attachment {display_name!r} skipped: run byte cap reached')
            continue

        classified = _classify_attachment(attachment.filename, data)
        if classified is None:
            continue  # disallowed/unrecognized type: silently skipped
        kind, content_type, extension = classified

        sanitized_name = sanitize_filename(attachment.filename)
        filename = _dedupe_filename(sanitized_name, used_names)
        if filename != sanitized_name:
            # The converted markdown references artifacts/{sanitized_name};
            # a collision rename is invisible to it, so surface the mismatch.
            state.add_error(
                page.id,
                page.title,
                f'attachment stored as {filename!r}: another attachment on this page already sanitized to '
                f'{sanitized_name!r}, so inline references to that name show the other file',
            )
        db.add(
            JobArtifact(
                job_id=job.id,
                kind=kind,
                filename=filename,
                content_type=content_type,
                content=data,
                size_bytes=len(data),
                source_url=attachment.download_url[:2048],
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        run.attachments_saved += 1
        run.artifact_bytes += len(data)

        if extension in _OCR_DOC_SPECS and options.get('ocr_attachments'):
            child_id = str(uuid.uuid4())
            folder_path = '/'.join(filter(None, [folder, subfolder])) or 'inbox'
            child_storage_folder = f'{folder_path}/{child_id}'
            child = Job(
                id=child_id,
                original_filename=filename,
                # Synthetic relative path: _resolve_upload_path only needs the
                # suffix (it materializes upload_content next to
                # storage_folder in the worker's uploads dir).
                upload_path=f'{child_storage_folder}/{child_id}{extension}',
                upload_content=data,
                upload_mime_type=content_type,
                upload_size_bytes=len(data),
                status=JobStatus.PENDING,
                owner_id=run.owner_id,
                import_run_id=run.id,
                processing_info={
                    'settings': {
                        'mode': 'import_attachment',
                        'email': options.get('email') or '',
                        'department': None,
                        'profile_id': options.get('ocr_profile_id'),
                        'collection_id': None,
                        'folder': folder,
                        'subfolder': subfolder,
                        'storage_folder': child_storage_folder,
                        'import': {'parent_job_id': job.id, 'source_page_id': page.id},
                    },
                },
            )
            db.add(child)
            _attach_tags(db, child, options.get('tags') or [])
            child_job_ids.append(child_id)

    return child_job_ids


def _import_one_page(
    db,
    run: ImportRun,
    state: _RunState,
    client: PageSource,
    page_id: str,
    options: dict,
    claimed_seq: int,
) -> tuple[Page | None, bool]:
    """Fetch + convert + persist one page (per-page commit). Returns
    (page, byte_cap_hit); page is None when the page failed or the cap
    stopped the run before importing it."""
    page = client.fetch_page(page_id)
    html_bytes = page.html.encode('utf-8')

    if run.artifact_bytes + run.content_bytes + len(html_bytes) > settings.import_run_max_total_bytes:
        # §5.4: hitting the total-bytes cap ends discovery gracefully -- the
        # run finishes with a note instead of failing.
        state.add_error(page.id, page.title, 'run byte cap reached; import stopped before this page')
        return None, True

    if not run.root_page_title:
        run.root_page_title = page.title[:512]
    run.current_page_title = page.title[:512]

    conversion = convert_page(
        page.html,
        base_url=run.source.base_url if run.source is not None else '',
        title=page.title,
        page_id=page.id,
        page_url=page.url,
        page_version=page.version,
        import_run_id=run.id,
        # include_attachments off: no artifacts will be stored, so same-host
        # images keep their absolute URL instead of a dangling artifacts/ ref.
        capture_attachments=bool(options.get('include_attachments', True)),
    )

    folder = options.get('folder') or _default_folder(run)
    subfolder = options.get('subfolder') or ''
    folder_path = '/'.join(filter(None, [folder, subfolder])) or 'inbox'
    job_id = str(uuid.uuid4())
    storage_folder = f'{folder_path}/{job_id}'

    job = Job(
        id=job_id,
        original_filename=f'{_slug(page.title)}.md',
        # .html: upload_content is the original export_view HTML (kept for a
        # future re-convert; restart endpoints 409 on mode='import').
        upload_path=f'{storage_folder}/{job_id}.html',
        upload_content=html_bytes,
        upload_mime_type='text/html',
        upload_size_bytes=len(html_bytes),
        result_markdown=conversion.markdown,
        status=JobStatus.FINISHED,
        owner_id=run.owner_id,
        import_run_id=run.id,
        processing_info={
            'settings': {
                'mode': 'import',
                'email': options.get('email') or '',
                'department': None,
                'profile_id': None,
                'collection_id': None,
                'folder': folder,
                'subfolder': subfolder,
                'storage_folder': storage_folder,
                'import': {
                    'source_page_id': page.id,
                    'source_page_version': page.version,
                    'source_url': page.url,
                },
            },
            'execution': {'status': 'finished', 'engine': 'confluence-import'},
        },
    )
    db.add(job)
    _attach_tags(db, job, options.get('tags') or [])

    state.visited[str(page.id)] = job_id
    run.pages_imported += 1
    run.content_bytes += len(html_bytes)

    child_job_ids: list[str] = []
    if options.get('include_attachments', True):
        child_job_ids = _store_attachments(
            db, run, state, job, page, client, options, folder, subfolder, claimed_seq
        )

    state.persist(run)
    # Lease-guarded commit; also the per-page staleness heartbeat.
    _commit_owned(db, run.id, claimed_seq)

    # Enqueue child OCR jobs only after their rows are committed.
    for child_id in child_job_ids:
        celery_app.send_task(
            'process_job',
            args=[child_id, options.get('ocr_profile_id'), 'import_attachment', options.get('email') or '', None],
        )
    return page, False


def _discover_children(
    db,
    run: ImportRun,
    state: _RunState,
    client: PageSource,
    page: Page,
    depth: int,
    max_pages: int,
    claimed_seq: int,
) -> None:
    """Append child page ids to the frontier (bounded by max_pages and the
    frontier link-bomb guard). Failures are recorded, never fatal."""
    frontier_cap = max_pages * 4
    frontier_ids = {entry[0] for entry in state.frontier}
    try:
        for child_id in client.iter_children(page.id):
            if run.pages_discovered >= max_pages or len(state.frontier) >= frontier_cap:
                break
            child_id = str(child_id)
            if child_id in state.visited or child_id in frontier_ids:
                continue
            state.frontier.append([child_id, depth + 1])
            frontier_ids.add(child_id)
            run.pages_discovered += 1
    except ConfluenceError as exc:
        state.add_error(page.id, page.title, f'listing children failed: {exc}')
    state.persist(run)
    _commit_owned(db, run.id, claimed_seq)


@celery_app.task(name=IMPORT_TASK_NAME, bind=True, acks_late=True, reject_on_worker_lost=True)
def import_confluence(self, run_id: str, chunk_seq: int) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        claimed_seq = _claim_chunk(db, run_id, chunk_seq, now)
        if claimed_seq is None:
            # Another live execution owns the run, or it is already terminal.
            return

        run = db.get(ImportRun, run_id)
        if run is None:
            return
        state = _RunState(run)
        options = run.options if isinstance(run.options, dict) else {}

        if run.cancel_requested:
            _cancel_run(db, run, state, claimed_seq)
            return

        # Setup errors (auth, deleted source, undecryptable credential, bad
        # server kind, unresolvable root) fail the run; per-page errors do not.
        source = db.get(ImportSource, run.source_id) if run.source_id else None
        if source is None:
            _fail_run(db, run, state, 'import source was deleted; the run cannot continue', claimed_seq)
            return
        try:
            credential = security.decrypt_import_credential(source.credential_encrypted)
        except ValueError:
            _fail_run(
                db, run, state,
                'stored credential can no longer be decrypted (SECRET_KEY changed?); re-enter it on the source and start a new run',
                claimed_seq,
            )
            return
        try:
            client: PageSource = create_client(
                base_url=source.base_url,
                server_kind=source.server_kind,
                auth_type=source.auth_type,
                auth_username=source.auth_username,
                credential=credential,
                allowed_private_hosts=frozenset(settings.import_private_host_allowlist),
                timeout=float(settings.import_fetch_timeout_seconds),
                max_response_bytes=settings.import_fetch_max_bytes,
                max_attachment_bytes=settings.import_attachment_max_bytes,
            )
        except ConfluenceError as exc:
            _fail_run(db, run, state, str(exc), claimed_seq)
            return

        # Re-clamped here even though the API already clamps at run creation:
        # options are a snapshot and max_depth=0 is a legal value (root only).
        raw_max_pages = options.get('max_pages')
        max_pages = settings.import_max_pages if not raw_max_pages else min(int(raw_max_pages), settings.import_max_pages)
        raw_max_depth = options.get('max_depth')
        max_depth = (
            settings.import_max_depth if raw_max_depth is None else min(int(raw_max_depth), settings.import_max_depth)
        )

        # First chunk: count the seeded frontier as discovered, and resolve
        # the space homepage for space-scoped runs (creation makes no
        # outbound requests, so their frontier starts empty).
        if run.pages_discovered == 0 and not state.visited:
            if not state.frontier and run.scope_type == 'space':
                try:
                    root_id = client.resolve_space_root(run.scope_value)
                except ConfluenceError as exc:
                    _fail_run(db, run, state, f'could not resolve space {run.scope_value!r}: {exc}', claimed_seq)
                    return
                state.frontier = [[str(root_id), 0]]
            if state.frontier:
                run.pages_discovered = len(state.frontier)
                state.persist(run)
                _commit_owned(db, run.id, claimed_seq)

        byte_cap_hit = False
        cancelled = False
        pages_this_chunk = 0
        # The page currently being imported; None between pages. Read by the
        # soft-time-limit handler to keep the §3 resume contract (a crash
        # re-imports at most the in-flight page -- never silently drops it).
        in_flight: tuple[str, int] | None = None
        try:
            while state.frontier and pages_this_chunk < settings.import_chunk_pages:
                if run.pages_imported >= max_pages:
                    break
                if _cancel_requested(db, run_id):
                    cancelled = True
                    break

                page_id, depth = state.frontier.pop(0)
                page_id = str(page_id)
                if page_id in state.visited:
                    continue
                in_flight = (page_id, int(depth))
                pages_this_chunk += 1

                try:
                    page, byte_cap_hit = _import_one_page(db, run, state, client, page_id, options, claimed_seq)
                except (SoftTimeLimitExceeded, _LeaseLost):
                    raise
                except Exception as exc:
                    # Per-page failure: roll back the partial page, record it,
                    # move on. Setup-level problems were handled above.
                    db.rollback()
                    run = db.get(ImportRun, run_id)
                    if run is None:
                        return
                    run.pages_failed += 1
                    state.visited[page_id] = None
                    state.add_error(page_id, '', str(exc))
                    state.persist(run)
                    _commit_owned(db, run.id, claimed_seq)
                    in_flight = None
                    continue

                if byte_cap_hit:
                    state.frontier = []
                    state.persist(run)
                    _commit_owned(db, run.id, claimed_seq)
                    break
                if page is not None and int(depth) < max_depth:
                    _discover_children(db, run, state, client, page, int(depth), max_pages, claimed_seq)
                in_flight = None
        except SoftTimeLimitExceeded:
            # Same continuation path as a full chunk, but first discard the
            # half-imported page's uncommitted rows and put the in-flight
            # page back at the head of the frontier -- otherwise the popped
            # page (and its whole undiscovered subtree) would be silently
            # omitted from the import.
            db.rollback()
            run = db.get(ImportRun, run_id)
            if run is None:
                return
            if in_flight is not None:
                flight_id, flight_depth = in_flight
                committed_job_id = state.visited.get(flight_id)
                if committed_job_id is not None and db.scalar(select(Job.id).where(Job.id == committed_job_id)) is None:
                    # The page's job row was rolled back with the session:
                    # forget the phantom visited entry so the continuation
                    # re-imports the page (idempotent -- nothing was stored).
                    del state.visited[flight_id]
                    committed_job_id = None
                if flight_id not in state.visited:
                    state.frontier.insert(0, [flight_id, flight_depth])
                elif committed_job_id is not None:
                    # Page committed, but its attachment phase / child
                    # discovery may have been cut short; re-importing would
                    # duplicate the page, so record the possible omission
                    # instead of hiding it.
                    state.add_error(
                        flight_id,
                        run.current_page_title,
                        'chunk time limit hit while processing this page; some attachments or child pages may be missing',
                    )
            state.persist(run)
            _commit_owned(db, run.id, claimed_seq)
            self.app.send_task(IMPORT_TASK_NAME, args=[run_id, claimed_seq])
            return

        if cancelled:
            _cancel_run(db, run, state, claimed_seq)
            return
        if state.frontier and run.pages_imported < max_pages and not byte_cap_hit:
            state.persist(run)
            _commit_owned(db, run.id, claimed_seq)
            self.app.send_task(IMPORT_TASK_NAME, args=[run_id, claimed_seq])
            return

        _finalize_run(db, run, state, claimed_seq)
    except _LeaseLost:
        # Another execution (stale reclaim) or an API backstop owns the run
        # now; this execution must neither write nor flip anything terminal.
        db.rollback()
        return
    except Exception as exc:  # pragma: no cover - defensive terminal transition
        logger.exception('import run %s failed: %s', run_id, exc)
        db.rollback()
        run = db.get(ImportRun, run_id)
        if run is not None and run.status in (ImportRunStatus.PENDING, ImportRunStatus.RUNNING):
            run.status = ImportRunStatus.FAILED
            run.error_message = _truncate(str(exc), _ERROR_MESSAGE_MAX_CHARS)
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
