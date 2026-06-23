"""initial schema

Revision ID: 0001_init
Revises:
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


revision = '0001_init'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum type only if it doesn't already exist (safe re-run)
    bind = op.get_bind()
    bind.execute(text(
        "DO $$ BEGIN "
        "  CREATE TYPE jobstatus AS ENUM ('PENDING', 'RUNNING', 'FINISHED', 'FAILED'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$;"
    ))

    # Use postgresql.ENUM with create_type=False so Alembic does not issue a
    # second CREATE TYPE statement when building the table DDL.
    jobstatus_enum = postgresql.ENUM(
        'PENDING', 'RUNNING', 'FINISHED', 'FAILED',
        name='jobstatus',
        create_type=False,
    )

    op.create_table(
        'jobs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('upload_path', sa.String(length=1024), nullable=False),
        sa.Column('result_path', sa.String(length=1024), nullable=True),
        sa.Column('status', jobstatus_enum, nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        'documents',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        'chunks',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('document_id', sa.String(length=36), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('chunk_type', sa.String(length=64), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table('chunks')
    op.drop_table('documents')
    op.drop_table('jobs')
    sa.Enum(name='jobstatus').drop(op.get_bind(), checkfirst=True)
