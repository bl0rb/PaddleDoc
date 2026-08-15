"""add mail ingestion (mail_messages table + jobs.mail_message_id)

Revision ID: 0009_mail_ingestion
Revises: 0008_vl_benchmarks
Create Date: 2026-08-15

sqlite-compatible on purpose (plain op.create_table / batch_alter_table, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0004_auth/.../0008_vl_benchmarks.
"""

from alembic import op
import sqlalchemy as sa


revision = '0009_mail_ingestion'
down_revision = '0008_vl_benchmarks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mail_messages',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('content_sha256', sa.String(length=64), nullable=False),
        sa.Column('rfc_message_id', sa.String(length=998), nullable=True),
        sa.Column('subject', sa.String(length=998), nullable=False, server_default=''),
        sa.Column('from_address', sa.String(length=998), nullable=False, server_default=''),
        sa.Column('recipients', sa.JSON(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source', sa.String(length=64), nullable=False, server_default='api'),
        sa.Column('raw_content', sa.LargeBinary(), nullable=False),
        sa.Column('raw_size_bytes', sa.Integer(), nullable=False),
        sa.Column('body_format', sa.String(length=32), nullable=True),
        sa.Column('body_markdown', sa.Text(), nullable=True),
        sa.Column('parts', sa.JSON(), nullable=False),
        sa.Column('parse_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('owner_id', 'content_sha256', name='uq_mail_messages_owner_id_content_sha256'),
        if_not_exists=True,
    )
    op.create_index('ix_mail_messages_owner_id', 'mail_messages', ['owner_id'], if_not_exists=True)
    op.create_index('ix_mail_messages_content_sha256', 'mail_messages', ['content_sha256'], if_not_exists=True)
    op.create_index('ix_mail_messages_rfc_message_id', 'mail_messages', ['rfc_message_id'], if_not_exists=True)

    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('mail_message_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_jobs_mail_message_id', 'mail_messages', ['mail_message_id'], ['id'], ondelete='SET NULL'
        )
    op.create_index('ix_jobs_mail_message_id', 'jobs', ['mail_message_id'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_jobs_mail_message_id', table_name='jobs')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_constraint('fk_jobs_mail_message_id', type_='foreignkey')
        batch_op.drop_column('mail_message_id')

    op.drop_index('ix_mail_messages_rfc_message_id', table_name='mail_messages')
    op.drop_index('ix_mail_messages_content_sha256', table_name='mail_messages')
    op.drop_index('ix_mail_messages_owner_id', table_name='mail_messages')
    op.drop_table('mail_messages')
