"""add VL connections (admin-managed vision-API endpoints) and benchmark runs
(multi-variant document conversion comparisons)

Revision ID: 0008_vl_benchmarks
Revises: 0007_versioning_tokens
Create Date: 2026-08-10

sqlite-compatible on purpose (plain op.create_table / batch_alter_table, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0004_auth/0005_import/0006_worker_logs/
0007_versioning_tokens.
"""

from alembic import op
import sqlalchemy as sa


revision = '0008_vl_benchmarks'
down_revision = '0007_versioning_tokens'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'vl_connections',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('base_url', sa.String(length=1024), nullable=False),
        sa.Column('model', sa.String(length=255), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=False, server_default=''),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )

    op.create_table(
        'benchmark_runs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('content_sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index('ix_benchmark_runs_owner_id', 'benchmark_runs', ['owner_id'], if_not_exists=True)
    op.create_index('ix_benchmark_runs_content_sha256', 'benchmark_runs', ['content_sha256'], if_not_exists=True)

    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('benchmark_run_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_jobs_benchmark_run_id', 'benchmark_runs', ['benchmark_run_id'], ['id'], ondelete='SET NULL'
        )
    op.create_index('ix_jobs_benchmark_run_id', 'jobs', ['benchmark_run_id'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_jobs_benchmark_run_id', table_name='jobs')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_constraint('fk_jobs_benchmark_run_id', type_='foreignkey')
        batch_op.drop_column('benchmark_run_id')

    op.drop_index('ix_benchmark_runs_content_sha256', table_name='benchmark_runs')
    op.drop_index('ix_benchmark_runs_owner_id', table_name='benchmark_runs')
    op.drop_table('benchmark_runs')

    op.drop_table('vl_connections')
