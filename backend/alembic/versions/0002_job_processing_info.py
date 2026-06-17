"""add job processing info

Revision ID: 0002_job_processing_info
Revises: 0001_init
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = '0002_job_processing_info'
down_revision = '0001_init'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text(
        "DO $$ BEGIN ALTER TABLE jobs ADD COLUMN processing_info jsonb; "
        "EXCEPTION WHEN duplicate_column THEN NULL; END $$;"
    ))


def downgrade() -> None:
    op.drop_column('jobs', 'processing_info')
