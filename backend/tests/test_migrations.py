"""Verifies the 0004_auth and 0005_import migrations' upgrade/downgrade round-trips.

0001_init / 0002_job_processing_info / 0002_add_password_protection use
postgres-only DDL (`DO $$ ... END $$` blocks, native ENUM) and cannot be
applied to a fresh sqlite database, so we can't just `alembic upgrade head`
from an empty db here. Instead we hand-build the pre-0004 schema (mirroring
exactly what those revisions produce) directly with sqlalchemy core, stamp
alembic to 0003, and drive 0004 itself through the real alembic machinery.
0004 was written to be sqlite-compatible (plain op.create_table/add_column,
no postgres-specific DDL) specifically so this works.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)

from app.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _build_legacy_metadata() -> MetaData:
    """The pre-0004 (i.e. post-0003) schema, built independently of the
    current ORM models (which already include the 0004 additions)."""
    metadata = MetaData()
    Table(
        'jobs', metadata,
        Column('id', String(36), primary_key=True),
        Column('original_filename', String(255), nullable=False),
        Column('upload_path', String(1024), nullable=False),
        Column('upload_content', LargeBinary, nullable=True),
        Column('upload_mime_type', String(128), nullable=True),
        Column('upload_size_bytes', Integer, nullable=True),
        Column('result_path', String(1024), nullable=True),
        Column('result_markdown', Text, nullable=True),
        Column('status', String(32), nullable=False),
        Column('error_message', Text, nullable=True),
        Column('processing_info', JSON, nullable=True),
        Column('password_hash', String(255), nullable=True),
        Column('created_at', DateTime(timezone=True), nullable=False),
        Column('updated_at', DateTime(timezone=True), nullable=False),
    )
    Table(
        'documents', metadata,
        Column('id', String(36), primary_key=True),
        Column('filename', String(255), nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False),
    )
    Table(
        'chunks', metadata,
        Column('id', String(36), primary_key=True),
        Column('document_id', String(36), ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        Column('content', Text, nullable=False),
        Column('chunk_type', String(64), nullable=False),
        Column('metadata', JSON, nullable=False),
    )
    Table(
        'tags', metadata,
        Column('id', String(36), primary_key=True),
        Column('name', String(64), nullable=False, unique=True),
    )
    Table(
        'job_tags', metadata,
        Column('job_id', String(36), ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True),
        Column('tag_id', String(36), ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )
    Table(
        'job_markdown_versions', metadata,
        Column('id', String(36), primary_key=True),
        Column('job_id', String(36), ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        Column('version', Integer, nullable=False),
        Column('content', Text, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False),
        UniqueConstraint('job_id', 'version', name='uq_job_markdown_versions_job_id_version'),
    )
    return metadata


def _alembic_config() -> Config:
    # Built programmatically (not Config('alembic.ini')) so it doesn't
    # depend on the process cwd -- the ini's `script_location = alembic` is
    # only correct relative to the backend/ directory, and tests may be run
    # from the repo root.
    cfg = Config()
    cfg.set_main_option('script_location', str(BACKEND_DIR / 'alembic'))
    return cfg


def test_0004_auth_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade: 0004 should add the auth tables + jobs.owner_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert expected in tables, f'{expected} missing after upgrade'

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'owner_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks
    ), job_fks

    sessions_indexes = {ix['name'] for ix in insp.get_indexes('sessions')}
    assert {'ix_sessions_user_id', 'ix_sessions_expires_at'} <= sessions_indexes

    # sqlite reflection can't describe expression-based indexes ("Skipped
    # unsupported reflection of expression-based index"), so confirm the
    # case-insensitive-unique-email index directly via sqlite_master and by
    # exercising the constraint.
    with engine.begin() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_users_email_lower'")
        ).scalar_one()
        assert 'UNIQUE' in ddl.upper()
        assert 'lower(email)' in ddl

        conn.execute(text(
            "INSERT INTO users (id, username, email, role, is_active, created_at, updated_at) "
            "VALUES ('u1', 'alice', 'Alice@Example.com', 'user', 1, '2026-01-01', '2026-01-01')"
        ))
        try:
            conn.execute(text(
                "INSERT INTO users (id, username, email, role, is_active, created_at, updated_at) "
                "VALUES ('u2', 'bob', 'alice@example.com', 'user', 1, '2026-01-01', '2026-01-01')"
            ))
        except Exception:
            pass
        else:
            raise AssertionError('case-insensitive duplicate email was not rejected')

    # --- downgrade: everything 0004 added should disappear ---
    command.downgrade(cfg, '0003_job_markdown_versions')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert removed not in tables, f'{removed} still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'owner_id' not in job_columns

    # --- re-upgrade: should cleanly re-apply from the 0003 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert expected in tables


def test_0005_import_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0005.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004 to 0005): import tables + jobs.import_run_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('import_sources', 'import_runs', 'job_artifacts'):
        assert expected in tables, f'{expected} missing after upgrade'

    source_columns = {c['name'] for c in insp.get_columns('import_sources')}
    assert {
        'id', 'owner_id', 'name', 'base_url', 'server_kind', 'api_base_path', 'auth_type',
        'auth_username', 'credential_encrypted', 'last_validated_at', 'last_test_at',
        'created_at', 'updated_at',
    } <= source_columns
    run_columns = {c['name'] for c in insp.get_columns('import_runs')}
    assert {
        'id', 'source_id', 'owner_id', 'kind', 'status', 'scope_type', 'scope_value',
        'root_page_title', 'options', 'error_message', 'cancel_requested', 'chunk_seq',
        'pages_discovered', 'pages_imported', 'pages_failed', 'attachments_saved',
        'artifact_bytes', 'content_bytes', 'current_page_title', 'state',
        'created_at', 'updated_at', 'started_at', 'finished_at',
    } <= run_columns
    artifact_columns = {c['name'] for c in insp.get_columns('job_artifacts')}
    assert {
        'id', 'job_id', 'kind', 'filename', 'content_type', 'content', 'size_bytes',
        'source_url', 'sha256', 'created_at',
    } <= artifact_columns

    source_fks = insp.get_foreign_keys('import_sources')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in source_fks
    ), source_fks
    run_fks = insp.get_foreign_keys('import_runs')
    assert any(
        fk['referred_table'] == 'import_sources' and fk['constrained_columns'] == ['source_id'] for fk in run_fks
    ), run_fks
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in run_fks
    ), run_fks
    artifact_fks = insp.get_foreign_keys('job_artifacts')
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['job_id'] for fk in artifact_fks
    ), artifact_fks

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'import_run_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'import_runs' and fk['constrained_columns'] == ['import_run_id']
        for fk in job_fks
    ), job_fks
    # The batch_alter_table rebuild must not have dropped 0004's FK.
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks
    ), job_fks

    assert 'ix_import_sources_owner_id' in {ix['name'] for ix in insp.get_indexes('import_sources')}
    run_indexes = {ix['name'] for ix in insp.get_indexes('import_runs')}
    assert {'ix_import_runs_owner_id', 'ix_import_runs_source_id'} <= run_indexes
    assert 'ix_job_artifacts_job_id' in {ix['name'] for ix in insp.get_indexes('job_artifacts')}
    jobs_indexes = {ix['name'] for ix in insp.get_indexes('jobs')}
    assert {'ix_jobs_import_run_id', 'ix_jobs_owner_id'} <= jobs_indexes

    artifact_uniques = insp.get_unique_constraints('job_artifacts')
    assert any(
        uc['name'] == 'uq_job_artifacts_job_id_filename' and set(uc['column_names']) == {'job_id', 'filename'}
        for uc in artifact_uniques
    ), artifact_uniques

    # --- downgrade one revision: only the 0005 additions should disappear ---
    command.downgrade(cfg, '0004_auth')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('import_sources', 'import_runs', 'job_artifacts'):
        assert removed not in tables, f'{removed} still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'import_run_id' not in job_columns
    # 0004's schema must survive a 0005-only downgrade untouched.
    assert 'owner_id' in job_columns
    for kept in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert kept in tables, f'{kept} unexpectedly dropped by 0005 downgrade'

    # --- re-upgrade: should cleanly re-apply from the 0004 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('import_sources', 'import_runs', 'job_artifacts'):
        assert expected in tables
    assert 'import_run_id' in {c['name'] for c in insp.get_columns('jobs')}


def test_0006_worker_logs_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0006.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004/0005 to 0006): worker_log_entries ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    assert 'worker_log_entries' in insp.get_table_names()

    columns = {c['name'] for c in insp.get_columns('worker_log_entries')}
    assert {
        'id', 'created_at', 'level', 'logger_name', 'worker_name',
        'task_id', 'task_name', 'message', 'exc_text',
    } <= columns

    indexes = {ix['name'] for ix in insp.get_indexes('worker_log_entries')}
    assert {
        'ix_worker_log_entries_created_at',
        'ix_worker_log_entries_level',
        'ix_worker_log_entries_worker_name',
    } <= indexes

    # --- downgrade one revision: only the 0006 addition should disappear ---
    command.downgrade(cfg, '0005_import')

    insp = inspect(engine)
    assert 'worker_log_entries' not in insp.get_table_names()
    # 0005's schema must survive a 0006-only downgrade untouched.
    assert 'import_sources' in insp.get_table_names()

    # --- re-upgrade: should cleanly re-apply from the 0005 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    assert 'worker_log_entries' in insp.get_table_names()
