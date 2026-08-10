"""add document versioning (jobs.content_sha256/document_version/previous_job_id)
and personal API bearer tokens (api_tokens)

Revision ID: 0007_versioning_tokens
Revises: 0006_worker_logs
Create Date: 2026-08-10

sqlite-compatible on purpose (plain op.create_table / batch_alter_table, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0004_auth/0005_import/0006_worker_logs.
"""

from alembic import op
import sqlalchemy as sa


revision = '0007_versioning_tokens'
down_revision = '0006_worker_logs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same batch_alter_table pattern 0004_auth/0005_import use for
    # jobs.owner_id/import_run_id: works on sqlite (table rebuild) AND
    # postgres. previous_job_id is self-referential (jobs.id), which batch
    # mode handles the same way as any other FK.
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('content_sha256', sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column('document_version', sa.Integer(), nullable=False, server_default='1')
        )
        batch_op.add_column(sa.Column('previous_job_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_jobs_previous_job_id', 'jobs', ['previous_job_id'], ['id'], ondelete='SET NULL'
        )
    op.create_index('ix_jobs_content_sha256', 'jobs', ['content_sha256'], if_not_exists=True)
    op.create_index('ix_jobs_previous_job_id', 'jobs', ['previous_job_id'], if_not_exists=True)

    op.create_table(
        'api_tokens',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('token_prefix', sa.String(length=12), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('token_hash', name='uq_api_tokens_token_hash'),
        if_not_exists=True,
    )
    op.create_index('ix_api_tokens_user_id', 'api_tokens', ['user_id'], if_not_exists=True)
    op.create_index('ix_api_tokens_token_hash', 'api_tokens', ['token_hash'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_api_tokens_token_hash', table_name='api_tokens')
    op.drop_index('ix_api_tokens_user_id', table_name='api_tokens')
    op.drop_table('api_tokens')

    op.drop_index('ix_jobs_previous_job_id', table_name='jobs')
    op.drop_index('ix_jobs_content_sha256', table_name='jobs')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_constraint('fk_jobs_previous_job_id', type_='foreignkey')
        batch_op.drop_column('previous_job_id')
        batch_op.drop_column('document_version')
        batch_op.drop_column('content_sha256')
