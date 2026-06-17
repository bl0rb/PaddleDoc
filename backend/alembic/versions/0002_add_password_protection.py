"""add password protection to jobs

Revision ID: 0002_add_password_protection
Revises: 0001_init
Create Date: 2026-06-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = '0002_add_password_protection'
down_revision = '0002_job_blob_tags'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(text(
        "DO $$ BEGIN ALTER TABLE jobs ADD COLUMN password_hash varchar(255); "
        "EXCEPTION WHEN duplicate_column THEN NULL; END $$;"
    ))


def downgrade() -> None:
    op.drop_column('jobs', 'password_hash')
