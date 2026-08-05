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
