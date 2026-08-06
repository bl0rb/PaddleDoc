"""add worker_log_entries: Celery worker log capture for the admin UI

Revision ID: 0006_worker_logs
Revises: 0005_import
Create Date: 2026-08-06

sqlite-compatible on purpose (plain op.create_table / op.create_index, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0005_import.
"""

from alembic import op
import sqlalchemy as sa


revision = '0006_worker_logs'
down_revision = '0005_import'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'worker_log_entries',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('level', sa.String(length=16), nullable=False),
        sa.Column('logger_name', sa.String(length=255), nullable=False),
        sa.Column('worker_name', sa.String(length=255), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=True),
        sa.Column('task_name', sa.String(length=255), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('exc_text', sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.create_index('ix_worker_log_entries_created_at', 'worker_log_entries', ['created_at'], if_not_exists=True)
    op.create_index('ix_worker_log_entries_level', 'worker_log_entries', ['level'], if_not_exists=True)
    op.create_index('ix_worker_log_entries_worker_name', 'worker_log_entries', ['worker_name'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_worker_log_entries_worker_name', table_name='worker_log_entries')
    op.drop_index('ix_worker_log_entries_level', table_name='worker_log_entries')
    op.drop_index('ix_worker_log_entries_created_at', table_name='worker_log_entries')
    op.drop_table('worker_log_entries')
