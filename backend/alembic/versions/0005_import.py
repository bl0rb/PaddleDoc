"""add confluence import: import_sources, import_runs, job_artifacts, jobs.import_run_id

Revision ID: 0005_import
Revises: 0004_auth
Create Date: 2026-08-04

sqlite-compatible on purpose (plain op.create_table / batch_alter_table, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0004_auth. Note sqlite never enables
PRAGMA foreign_keys (see app/database/session.py), so the SET NULL on
jobs.import_run_id is only self-executing on postgres -- the run-delete
endpoint nulls the column with an explicit UPDATE for dialect parity.
"""

from alembic import op
import sqlalchemy as sa


revision = '0005_import'
down_revision = '0004_auth'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'import_sources',
        sa.Column('id', sa.String(length=36), primary_key=True),
        # CASCADE (not SET NULL like jobs.owner_id): an encrypted credential
        # must not survive its owner.
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('base_url', sa.String(length=1024), nullable=False),
        sa.Column('server_kind', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('api_base_path', sa.String(length=64), nullable=False, server_default=''),
        sa.Column(
            'auth_type',
            sa.Enum('cloud_basic', 'pat_bearer', name='import_auth_type', native_enum=False, validate_strings=True),
            nullable=False,
        ),
        sa.Column('auth_username', sa.String(length=320), nullable=False, server_default=''),
        sa.Column('credential_encrypted', sa.Text(), nullable=False),
        sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_test_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_index('ix_import_sources_owner_id', 'import_sources', ['owner_id'], if_not_exists=True)

    op.create_table(
        'import_runs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'source_id',
            sa.String(length=36),
            sa.ForeignKey('import_sources.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='confluence'),
        sa.Column(
            'status',
            sa.Enum(
                'pending', 'running', 'finished', 'failed', 'cancelled',
                name='import_run_status', native_enum=False, validate_strings=True,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('scope_type', sa.String(length=16), nullable=False),
        sa.Column('scope_value', sa.String(length=512), nullable=False),
        sa.Column('root_page_title', sa.String(length=512), nullable=False, server_default=''),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('chunk_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pages_discovered', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pages_imported', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pages_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('attachments_saved', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('artifact_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('content_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('current_page_title', sa.String(length=512), nullable=False, server_default=''),
        sa.Column('state', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.create_index('ix_import_runs_owner_id', 'import_runs', ['owner_id'], if_not_exists=True)
    op.create_index('ix_import_runs_source_id', 'import_runs', ['source_id'], if_not_exists=True)

    op.create_table(
        'job_artifacts',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('filename', sa.String(length=512), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('source_url', sa.String(length=2048), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('job_id', 'filename', name='uq_job_artifacts_job_id_filename'),
        if_not_exists=True,
    )
    op.create_index('ix_job_artifacts_job_id', 'job_artifacts', ['job_id'], if_not_exists=True)

    # Same batch_alter_table pattern 0004_auth uses for jobs.owner_id: works
    # on sqlite (table rebuild) AND postgres, so both dialects get the FK
    # with SET NULL semantics.
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('import_run_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_jobs_import_run_id', 'import_runs', ['import_run_id'], ['id'], ondelete='SET NULL'
        )
    op.create_index('ix_jobs_import_run_id', 'jobs', ['import_run_id'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_jobs_import_run_id', table_name='jobs')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_constraint('fk_jobs_import_run_id', type_='foreignkey')
        batch_op.drop_column('import_run_id')

    op.drop_index('ix_job_artifacts_job_id', table_name='job_artifacts')
    op.drop_table('job_artifacts')

    op.drop_index('ix_import_runs_source_id', table_name='import_runs')
    op.drop_index('ix_import_runs_owner_id', table_name='import_runs')
    op.drop_table('import_runs')

    op.drop_index('ix_import_sources_owner_id', table_name='import_sources')
    op.drop_table('import_sources')
