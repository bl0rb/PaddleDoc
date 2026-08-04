import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
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
    owner: Mapped['User | None'] = relationship(back_populates='owned_jobs')


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
