"""add per-account login throttling (users.failed_login_count/locked_until)

Revision ID: 0010_login_lockout
Revises: 0009_mail_ingestion
Create Date: 2026-08-18

Before this, the only brake on password guessing was the general 60/min
per-client rate limiter -- which is generous for a targeted attack on one
known account, and deliberately fails open when Redis is unavailable. These
two columns give the login its own counter that lives in the database the
login already needs, so it keeps working (and keeps refusing) during a Redis
outage.

sqlite-compatible on purpose, but deliberately NOT via batch_alter_table for
the upgrade: on sqlite that rebuilds the table, and the rebuild silently drops
the functional index ix_users_email_lower (created in 0004_auth over
lower(email)) because SQLAlchemy cannot reflect an expression index. The
earlier migrations only ever batch-altered `jobs`, which carries no such
index. Plain ADD COLUMN needs no rebuild; the downgrade has to rebuild to drop
columns, so it recreates the index afterwards.
"""

from alembic import op
import sqlalchemy as sa


revision = '0010_login_lockout'
down_revision = '0009_mail_ingestion'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('failed_login_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('last_failed_login_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('locked_until')
        batch.drop_column('last_failed_login_at')
        batch.drop_column('failed_login_count')

    # The batch rebuild above loses the expression index from 0004_auth
    # (SQLAlchemy cannot reflect lower(email)); recreate it so downgrading
    # leaves the schema exactly as 0009 had it.
    op.create_index(
        'ix_users_email_lower',
        'users',
        [sa.text('lower(email)')],
        unique=True,
        if_not_exists=True,
    )
