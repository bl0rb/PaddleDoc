"""add job_markdown_versions table

Revision ID: 0003_job_markdown_versions
Revises: 0002_add_password_protection
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = '0003_job_markdown_versions'
down_revision = '0002_add_password_protection'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'job_markdown_versions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('job_id', 'version', name='uq_job_markdown_versions_job_id_version'),
        if_not_exists=True,
    )
    op.create_index(
        'ix_job_markdown_versions_job_id',
        'job_markdown_versions',
        ['job_id'],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('ix_job_markdown_versions_job_id', table_name='job_markdown_versions')
    op.drop_table('job_markdown_versions')
