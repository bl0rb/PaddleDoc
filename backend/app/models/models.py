import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


job_tags = Table(
    'job_tags',
    Base.metadata,
    Column('job_id', String(36), ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', String(36), ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
)


class JobStatus(str, enum.Enum):
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    FINISHED = 'FINISHED'
    FAILED = 'FAILED'


class Job(Base):
    __tablename__ = 'jobs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    upload_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    upload_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    upload_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upload_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    result_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # NULL = legacy job predating auth (2026-08 migration); visible to admins
    # only until claimed via POST /auth/admin/jobs/claim-ownerless.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # Set on jobs created by a Confluence import (one Job per imported page,
    # plus attachment-OCR children); the run outlives worker restarts, the
    # jobs outlive the run (SET NULL on run delete).
    import_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('import_runs.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # Set on jobs created by POST /benchmarks (one Job per requested variant:
    # VL connections + optional OCR profile) -- see app/api/benchmarks.py.
    # These children bypass the duplicate-409/version-chain logic in
    # create_job_from_upload entirely (document_version is always 1,
    # previous_job_id always None) and are excluded from every normal job
    # surface by construction -- GET /jobs, GET /search,
    # _find_predecessor_job, and the _apply_visible_filter-based
    # /stats, /markdown-files, /folders/* and collection queries -- but
    # remain individually fetchable by id like any other job.
    benchmark_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('benchmark_runs.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # Set on jobs created as attachment children of a mail ingest (one Job
    # per supported attachment on POST /api/v1/mail/messages) -- see
    # app/services/mail_ingest.py and MailMessage.jobs. Mirrors
    # import_run_id/benchmark_run_id: SET NULL so deleting the message
    # doesn't cascade-delete an already-useful OCR result. DELETE
    # /mail/messages explicitly NULLs this column itself before deleting the
    # message row -- SQLite here runs without PRAGMA foreign_keys, so the
    # ON DELETE SET NULL cascade alone is inert (see delete_import_run for
    # the same reasoning applied to import_run_id).
    mail_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('mail_messages.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # sha256 hex of the raw upload bytes, computed once at upload time.
    # Drives duplicate-content detection and document versioning (see
    # create_job_from_upload in app/api/routes.py) -- NULL for jobs created
    # outside the two upload endpoints (e.g. Confluence-imported pages).
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 1 for a document's first upload; incremented on every subsequent
    # re-upload of a same-named file that isn't byte-identical to the
    # latest visible version. server_default keeps old raw-SQL/test inserts
    # that don't set it explicitly NOT NULL-safe.
    document_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1', nullable=False)
    # Chain pointer to the prior version's Job row (NULL for version 1 or
    # for jobs with no detected predecessor). SET NULL so deleting an old
    # version doesn't cascade-delete its successors.
    previous_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    tags: Mapped[list['Tag']] = relationship(secondary=job_tags, back_populates='jobs')
    markdown_versions: Mapped[list['JobMarkdownVersion']] = relationship(
        back_populates='job',
        cascade='all, delete-orphan',
        order_by='JobMarkdownVersion.version',
    )
    artifacts: Mapped[list['JobArtifact']] = relationship(
        back_populates='job',
        cascade='all, delete-orphan',
    )
    owner: Mapped['User | None'] = relationship(back_populates='owned_jobs')
    import_run: Mapped['ImportRun | None'] = relationship(back_populates='jobs')
    benchmark_run: Mapped['BenchmarkRun | None'] = relationship(back_populates='jobs')
    mail_message: Mapped['MailMessage | None'] = relationship(back_populates='jobs')


class JobMarkdownVersion(Base):
    """Editor save history for a job's markdown.

    Replaces the old on-disk `.v{n}.md` files: with no shared volume between
    backend and worker pods, every edited version is persisted as a row here
    instead so it stays readable from any pod.
    """

    __tablename__ = 'job_markdown_versions'
    __table_args__ = (
        UniqueConstraint('job_id', 'version', name='uq_job_markdown_versions_job_id_version'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    job: Mapped[Job] = relationship(back_populates='markdown_versions')


class Document(Base):
    __tablename__ = 'documents'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    chunks: Mapped[list['Chunk']] = relationship(back_populates='document', cascade='all, delete-orphan')


class Chunk(Base):
    __tablename__ = 'chunks'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey('documents.id', ondelete='CASCADE'), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_metadata: Mapped[dict] = mapped_column('metadata', JSON, default=dict)

    document: Mapped[Document] = relationship(back_populates='chunks')


class Tag(Base):
    __tablename__ = 'tags'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    jobs: Mapped[list[Job]] = relationship(secondary=job_tags, back_populates='tags')


class UserRole(str, enum.Enum):
    ADMIN = 'admin'
    USER = 'user'


class Team(Base):
    __tablename__ = 'teams'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    users: Mapped[list['User']] = relationship(back_populates='team')


class AuthProvider(Base):
    """OIDC identity provider configuration (Keycloak, Entra ID, ...).

    `client_secret_encrypted` is Fernet-encrypted at rest with a key derived
    via HKDF-SHA256(SECRET_KEY, info="oidc-client-secret") — see
    app/services/security.py. Never expose it (or issuer/client_id) on
    unauthenticated routes; GET /auth/providers only returns slug+display_name.
    """

    __tablename__ = 'auth_providers'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), default='openid profile email', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    users: Mapped[list['User']] = relationship(back_populates='oidc_provider')


class VlConnection(Base):
    """Admin-managed OpenAI-compatible vision-API endpoint, usable as a
    benchmark variant (see BenchmarkRun / app/api/benchmarks.py) alongside
    the env-configured openai_vision profile.

    `api_key_encrypted` is Fernet-encrypted at rest with a key derived via
    HKDF-SHA256(SECRET_KEY, info="vl-connection-api-key") -- see
    app/services/security.py. Write-only at the API: no response schema
    carries it (only a `has_api_key` boolean), decrypted only inside the
    admin /test endpoint and app/workers/tasks.py's benchmark job dispatch.
    Global resource (not owned by a user), so there is no owner FK.
    """

    __tablename__ = 'vl_connections'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default='', server_default='', nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default='1', nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('oidc_provider_id', 'oidc_subject', name='uq_users_oidc_provider_subject'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Stored lowercased by the service layer; unique constraint is a plain
    # column constraint since usernames are already normalized on write.
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Uniqueness is enforced via the case-insensitive functional index
    # ix_users_email_lower below, not a plain column constraint, so
    # Foo@example.com and foo@example.com can't coexist.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # NULL => OIDC-only account (no local password login possible).
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # native_enum=False: plain VARCHAR + CHECK constraint on every dialect
    # (sqlite has no native enum type; this also sidesteps the manual
    # CREATE TYPE / DROP TYPE dance used for jobstatus in 0001_init).
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name='user_role', native_enum=False, validate_strings=True),
        default=UserRole.USER,
        nullable=False,
    )
    team_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('teams.id', ondelete='SET NULL'), nullable=True, index=True
    )
    oidc_provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('auth_providers.id', ondelete='SET NULL'), nullable=True
    )
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    team: Mapped[Team | None] = relationship(back_populates='users')
    oidc_provider: Mapped[AuthProvider | None] = relationship(back_populates='users')
    sessions: Mapped[list['Session']] = relationship(back_populates='user', cascade='all, delete-orphan')
    owned_jobs: Mapped[list[Job]] = relationship(back_populates='owner')
    owned_collections: Mapped[list['Collection']] = relationship(back_populates='owner')
    import_sources: Mapped[list['ImportSource']] = relationship(back_populates='owner', cascade='all, delete-orphan')
    import_runs: Mapped[list['ImportRun']] = relationship(back_populates='owner')
    benchmark_runs: Mapped[list['BenchmarkRun']] = relationship(back_populates='owner')
    api_tokens: Mapped[list['ApiToken']] = relationship(back_populates='user', cascade='all, delete-orphan')
    mail_messages: Mapped[list['MailMessage']] = relationship(back_populates='owner')


# Case-insensitive uniqueness on email. Declared after the class body (not in
# __table_args__) because it needs the real InstrumentedAttribute/Column for
# func.lower(...); expression indexes are supported on both sqlite (3.9+)
# and postgres.
Index('ix_users_email_lower', func.lower(User.email), unique=True)


class Session(Base):
    """Opaque, DB-backed session token. Never store the raw token — only
    its sha256 hex digest, so a DB read alone can't be replayed as a cookie.
    """

    __tablename__ = 'sessions'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(back_populates='sessions')


class ApiToken(Base):
    """Personal bearer token for programmatic (non-cookie) API access.

    Same never-store-the-raw-value discipline as Session: only
    sha256(token) is persisted in token_hash (see
    app/services/security.hash_session_token, reused here). token_prefix
    (first 8 chars of the raw 'pd_...' token, itself not secret) lets the
    owner recognize a token in the list UI without ever re-displaying the
    full value, which is only ever returned once, at creation time, by
    POST /api/v1/auth/tokens.
    """

    __tablename__ = 'api_tokens'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    # Touched at most once/60s by deps.get_current_user's bearer path, to
    # bound write volume for tokens used on every request of a hot script.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates='api_tokens')


class Collection(Base):
    """Persistent replacement for the in-memory `_COLLECTIONS` dict in
    app/api/routes.py (survives restart + works across multiple replicas).
    Field shape mirrors what that dict currently carries; `name` is new,
    for a future user-facing label.
    """

    __tablename__ = 'collections'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), default='', nullable=False)
    department: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    folder: Mapped[str] = mapped_column(String(1024), default='', nullable=False)
    subfolder: Mapped[str] = mapped_column(String(1024), default='', nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    owner: Mapped[User | None] = relationship(back_populates='owned_collections')


class ImportAuthType(str, enum.Enum):
    CLOUD_BASIC = 'cloud_basic'  # Confluence Cloud: Basic base64(email:api_token)
    PAT_BEARER = 'pat_bearer'    # Server/DC >= 7.9 personal access token


class ImportRunStatus(str, enum.Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    FINISHED = 'finished'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class ImportSource(Base):
    """A saved Confluence connection: base URL + write-only credential.

    `credential_encrypted` is Fernet-encrypted at rest with a key derived via
    HKDF-SHA256(SECRET_KEY, info="import-source-credential") -- see
    app/services/security.py. The credential is write-only at the API: no
    response schema carries it (only a `has_credential` boolean), and it is
    decrypted only inside the /test endpoint and the import worker task.
    Sources are strictly owner-private (a credential is a personal Confluence
    identity), hence CASCADE on owner delete -- unlike jobs' SET NULL, a
    credential must not survive its owner.
    """

    __tablename__ = 'import_sources'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Normalized, no trailing slash, e.g. https://acme.atlassian.net
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 'cloud' | 'datacenter', resolved by POST /import/sources/{id}/test;
    # selects the v2 (Cloud) vs v1 (Server/DC) REST client.
    server_kind: Mapped[str] = mapped_column(String(16), default='', nullable=False)
    # '/wiki/api/v2' (Cloud) or '/rest/api' (Server/DC), resolved on /test.
    api_base_path: Mapped[str] = mapped_column(String(64), default='', nullable=False)
    auth_type: Mapped[ImportAuthType] = mapped_column(
        Enum(ImportAuthType, name='import_auth_type', native_enum=False, validate_strings=True),
        nullable=False,
    )
    # Email for cloud_basic; empty for pat_bearer.
    auth_username: Mapped[str] = mapped_column(String(320), default='', nullable=False)
    credential_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Set by a successful /test only.
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by EVERY /test attempt (success or failure) -- DB-backed cooldown
    # anchor that holds even when the Redis rate limiter fails open.
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    owner: Mapped[User] = relationship(back_populates='import_sources')
    runs: Mapped[list['ImportRun']] = relationship(back_populates='source')


class ImportRun(Base):
    """One crawl execution against an ImportSource.

    Runs are processed by the chunked `import_confluence` Celery task:
    `chunk_seq` is an optimistic lease incremented by each chunk execution's
    claim UPDATE (with a stale-lease reclaim for lost workers), and `state`
    persists the crawl frontier/visited map so a run survives worker restarts
    and resumes idempotently. A 'running' run whose `updated_at` is older
    than IMPORT_STALE_RUN_SECONDS is considered stale (worker lost).
    """

    __tablename__ = 'import_runs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('import_sources.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # NULL = legacy/admin-only, mirrors jobs.owner_id semantics.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # 'confluence' today; discriminator for a future 'website' importer.
    kind: Mapped[str] = mapped_column(String(16), default='confluence', nullable=False)
    status: Mapped[ImportRunStatus] = mapped_column(
        Enum(ImportRunStatus, name='import_run_status', native_enum=False, validate_strings=True),
        default=ImportRunStatus.PENDING,
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'space' | 'page'
    scope_value: Mapped[str] = mapped_column(String(512), nullable=False)  # space key or page id
    root_page_title: Mapped[str] = mapped_column(String(512), default='', nullable=False)
    # Snapshot of the request options (max_pages, max_depth,
    # include_attachments, ocr_attachments, ocr_profile_id, folder,
    # subfolder, tags, email) -- NOT credentials; the worker re-reads the
    # source at each chunk start.
    options: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Optimistic lease for the chunked task (see app/workers/import_tasks.py).
    chunk_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attachments_saved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # job_artifacts payload bytes for this run.
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Page export_view HTML stored in jobs.upload_content for this run.
    content_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_page_title: Mapped[str] = mapped_column(String(512), default='', nullable=False)
    # {'frontier': [[page_id, depth], ...], 'visited': {page_id: job_id|None},
    #  'errors': [{'page_id': ..., 'title': ..., 'error': ...}]}
    state: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[ImportSource | None] = relationship(back_populates='runs')
    owner: Mapped[User | None] = relationship(back_populates='import_runs')
    jobs: Mapped[list[Job]] = relationship(back_populates='import_run')


class BenchmarkRun(Base):
    """One multi-variant document-conversion comparison: a single uploaded
    file processed by 2-7 variants (VL connections and/or one OCR profile),
    each as its own Job row linked via Job.benchmark_run_id. See
    app/api/benchmarks.py for creation/report/export logic.

    Child jobs bypass the normal duplicate-409/version-chain path entirely
    (always document_version=1, previous_job_id=None) and are excluded from
    GET /jobs and GET /search by construction -- see
    app/api/routes.py's _apply_job_filters and _find_predecessor_job.
    """

    __tablename__ = 'benchmark_runs'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # NULL = owning user was later deleted (SET NULL), mirrors
    # ImportRun.owner_id/Job.owner_id semantics.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    owner: Mapped[User | None] = relationship(back_populates='benchmark_runs')
    # No cascade -- ondelete=SET NULL at the DB level, same pattern as
    # ImportRun.jobs. DELETE /benchmarks/{id} explicitly deletes each child
    # Job itself before deleting the run (see app/api/benchmarks.py).
    jobs: Mapped[list[Job]] = relationship(back_populates='benchmark_run')


class MailMessage(Base):
    """One ingested raw RFC-822 email (see docs/integrations/mail-ingestion.md).

    POST /api/v1/mail/messages parses the raw bytes with stdlib `email`
    (BytesParser + policy.default) and stores everything here -- the
    verbatim .eml, the decoded envelope, the rendered body markdown and a
    per-part manifest -- following the same DB-is-the-source-of-truth
    convention as Job.upload_content/result_markdown (disk under storage/
    is a best-effort cache the worker rehydrates from the DB, never relied
    on; the Helm chart's shared RWX PVC is optional and no code path
    depends on it). Each supported attachment becomes an ordinary Job
    linked back via Job.mail_message_id.

    content_sha256 (sha256 over the raw .eml bytes) is the idempotency key
    -- the same primitive as Job.content_sha256, lifted to message level.
    A replayed POST with identical bytes returns the existing row (200)
    instead of re-ingesting, which is what makes sender-side retry loops
    (gateway outbox, n8n retry-on-fail) safe. rfc_message_id (the parsed
    Message-ID header) is a lookup convenience only, never the dedup key --
    it is sender-controlled, spoofable, and not always present.
    """

    __tablename__ = 'mail_messages'
    __table_args__ = (
        # Dedup scope = owning user. Postgres/SQLite both treat NULLs as
        # distinct, so rows orphaned by user deletion (owner_id NULL) never
        # collide with each other here -- the scoped lookup in the ingest
        # handler (own + team + admin visibility), not this constraint, is
        # what catches same-team duplicates. This is only the last-resort
        # backstop for a concurrent duplicate racing to commit within one
        # owner (caught as IntegrityError -> rollback -> re-fetch -> 200).
        UniqueConstraint('owner_id', 'content_sha256', name='uq_mail_messages_owner_id_content_sha256'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # NULL = owning user was later deleted (SET NULL), mirrors
    # Job.owner_id/ImportRun.owner_id/BenchmarkRun.owner_id semantics.
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    # sha256 hex over the raw .eml bytes -- see class docstring. Indexed on
    # its own (in addition to the composite unique constraint above)
    # because the dedup lookup and the ?sha256= retrieval filter are scoped
    # by the caller's full visibility (own + team + admin), not just
    # owner_id.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Parsed Message-ID header. NULL when absent -- not every sender sets
    # one, and identity never rests on this (see docstring).
    rfc_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(998), default='', server_default='', nullable=False)
    from_address: Mapped[str] = mapped_column(String(998), default='', server_default='', nullable=False)
    # {"to": [...], "cc": [...]} decoded address lists.
    recipients: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'api' for POST /mail/messages, 'upload' for .eml files uploaded via UI.
    # Not validated against a fixed set; free-form extensible for future origins.
    source: Mapped[str] = mapped_column(String(64), default='api', server_default='api', nullable=False)
    # The original .eml, verbatim -- served by the raw-download endpoint.
    # Must be deferred on every list/lookup query except that one (mirror
    # _JOB_BLOB_DEFER_OPTIONS / _ARTIFACT_BLOB_DEFER_OPTIONS via
    # _MAIL_BLOB_DEFER_OPTIONS = (defer(MailMessage.raw_content),)).
    raw_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'text/plain' | 'text/html' -- which MIME part body_markdown came
    # from. NULL for a body-less (attachment-only) message.
    body_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Rendered body incl. YAML frontmatter (source/subject/from/to/date/
    # message_id/content_sha256/ingested_by/ingested_at). NULL for a
    # body-less message -- that is a valid ingest, not an error.
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-part manifest in MIME-tree walk order:
    # [{index, filename, content_type, size_bytes,
    #   outcome: 'job'|'inline'|'skipped', job_id?, skip_reason?}, ...].
    # Authoritative -- the part-content endpoint re-walks the MIME tree and
    # cross-checks filename/content_type against this manifest before
    # serving.
    parts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Reserved for future use: normal parse failures reject the POST (422)
    # without storing a row at all, so this stays NULL in practice today.
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    owner: Mapped[User | None] = relationship(back_populates='mail_messages')
    # No cascade -- ondelete=SET NULL at the DB level, same pattern as
    # ImportRun.jobs/BenchmarkRun.jobs. DELETE /mail/messages explicitly
    # NULLs jobs.mail_message_id itself before deleting this row (SQLite
    # here runs without PRAGMA foreign_keys, so relying on the FK's SET
    # NULL alone is inert -- delete_import_run does exactly this, for
    # exactly this reason).
    jobs: Mapped[list[Job]] = relationship(back_populates='mail_message')


class JobArtifact(Base):
    """Binary payload (inline image or attachment) captured for an imported
    page's Job. Stored in the DB (BYTEA on postgres) because there is no
    shared filesystem between backend and worker pods.

    `content` must be deferred/excluded from every listing query (mirror the
    `_JOB_BLOB_DEFER_OPTIONS` pattern in routes.py); only the single-artifact
    content endpoint selects the blob.
    """

    __tablename__ = 'job_artifacts'
    __table_args__ = (
        UniqueConstraint('job_id', 'filename', name='uq_job_artifacts_job_id_filename'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # 'image' | 'attachment'
    # Sanitized and de-duplicated per job (suffix "-2", "-3", ...).
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Our validated classification (extension + magic bytes), never the
    # remote server's header.
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Original Confluence download URL (provenance only, never re-fetched).
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    job: Mapped[Job] = relationship(back_populates='artifacts')


class WorkerLogEntry(Base):
    """A single Celery worker log record, mirrored here by
    app.workers.log_capture.WorkerLogDBHandler so the admin UI can tail
    worker container logs without docker.sock/kubectl access -- the EKS/k8s
    deployment runs the worker as a Deployment with N replicas + an HPA (see
    charts/paddledoc/templates/worker-deployment.yaml), so there is no
    single node-local log file to read either, and compose has no shared
    volume mounted read-only into the backend for the same purpose.

    Retention is a row-count cap (settings.worker_log_retention_max_rows),
    pruned opportunistically by the handler itself on write -- there is no
    beat/cron worker in this deployment to run a scheduled prune job.
    """

    __tablename__ = 'worker_log_entries'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # DEBUG/INFO/WARNING/ERROR/CRITICAL
    logger_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Container/pod hostname (socket.gethostname()) -- unique per worker
    # replica in the k8s Deployment; stable for the container's lifetime in
    # compose, changes across a restart.
    worker_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    exc_text: Mapped[str | None] = mapped_column(Text, nullable=True)
