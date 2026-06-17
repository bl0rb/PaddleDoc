"""store uploads/results in db and add tags

Revision ID: 0002_job_blob_tags
Revises: 0001_init
Create Date: 2026-06-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = '0002_job_blob_tags'
down_revision = '0002_job_processing_info'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for col_sql in [
        "ALTER TABLE jobs ADD COLUMN upload_content bytea",
        "ALTER TABLE jobs ADD COLUMN upload_mime_type varchar(128)",
        "ALTER TABLE jobs ADD COLUMN upload_size_bytes integer",
        "ALTER TABLE jobs ADD COLUMN result_markdown text",
    ]:
        bind.execute(text(
            f"DO $$ BEGIN {col_sql}; EXCEPTION WHEN duplicate_column THEN NULL; END $$;"
        ))

    op.create_table(
        'tags',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False, unique=True),
        if_not_exists=True,
    )
    bind.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_tags_name ON tags (name);"
    ))
    op.create_table(
        'job_tags',
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tag_id', sa.String(length=36), sa.ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table('job_tags')
    op.drop_index('ix_tags_name', table_name='tags')
    op.drop_table('tags')
    op.drop_column('jobs', 'result_markdown')
    op.drop_column('jobs', 'upload_size_bytes')
    op.drop_column('jobs', 'upload_mime_type')
    op.drop_column('jobs', 'upload_content')
