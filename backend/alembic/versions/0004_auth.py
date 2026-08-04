"""add auth foundation: teams, users, sessions, auth_providers, collections

Revision ID: 0004_auth
Revises: 0003_job_markdown_versions
Create Date: 2026-08-04

sqlite-compatible on purpose (plain op.create_table / op.add_column, no
postgres-only DDL) so the migration can be exercised against the sqlite test
database as well as postgres in prod.
"""

from alembic import op
import sqlalchemy as sa


revision = '0004_auth'
down_revision = '0003_job_markdown_versions'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'teams',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('name', name='uq_teams_name'),
        if_not_exists=True,
    )

    op.create_table(
        'auth_providers',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('issuer_url', sa.String(length=1024), nullable=False),
        sa.Column('client_id', sa.String(length=255), nullable=False),
        sa.Column('client_secret_encrypted', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('scopes', sa.String(length=255), nullable=False, server_default='openid profile email'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('slug', name='uq_auth_providers_slug'),
        if_not_exists=True,
    )

    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('username', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        # native_enum=False -> plain VARCHAR + CHECK constraint on every
        # dialect (no CREATE TYPE/DROP TYPE step needed on postgres either).
        sa.Column(
            'role',
            sa.Enum('admin', 'user', name='user_role', native_enum=False, validate_strings=True),
            nullable=False,
            server_default='user',
        ),
        sa.Column('team_id', sa.String(length=36), sa.ForeignKey('teams.id', ondelete='SET NULL'), nullable=True),
        sa.Column(
            'oidc_provider_id',
            sa.String(length=36),
            sa.ForeignKey('auth_providers.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('oidc_subject', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('username', name='uq_users_username'),
        sa.UniqueConstraint('oidc_provider_id', 'oidc_subject', name='uq_users_oidc_provider_subject'),
        if_not_exists=True,
    )
    op.create_index('ix_users_team_id', 'users', ['team_id'], if_not_exists=True)
    # Case-insensitive uniqueness on email (Foo@x.com and foo@x.com can't
    # coexist). Expression index, supported on sqlite 3.9+ and postgres.
    op.create_index(
        'ix_users_email_lower',
        'users',
        [sa.text('lower(email)')],
        unique=True,
        if_not_exists=True,
    )

    op.create_table(
        'sessions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.UniqueConstraint('token_hash', name='uq_sessions_token_hash'),
        if_not_exists=True,
    )
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'], if_not_exists=True)
    op.create_index('ix_sessions_expires_at', 'sessions', ['expires_at'], if_not_exists=True)

    op.create_table(
        'collections',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('email', sa.String(length=320), nullable=False, server_default=''),
        sa.Column('department', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('folder', sa.String(length=1024), nullable=False, server_default=''),
        sa.Column('subfolder', sa.String(length=1024), nullable=False, server_default=''),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_index('ix_collections_owner_id', 'collections', ['owner_id'], if_not_exists=True)

    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('owner_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_jobs_owner_id_users', 'users', ['owner_id'], ['id'], ondelete='SET NULL'
        )
    op.create_index('ix_jobs_owner_id', 'jobs', ['owner_id'], if_not_exists=True)


def downgrade() -> None:
    op.drop_index('ix_jobs_owner_id', table_name='jobs')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_constraint('fk_jobs_owner_id_users', type_='foreignkey')
        batch_op.drop_column('owner_id')

    op.drop_index('ix_collections_owner_id', table_name='collections')
    op.drop_table('collections')

    op.drop_index('ix_sessions_expires_at', table_name='sessions')
    op.drop_index('ix_sessions_user_id', table_name='sessions')
    op.drop_table('sessions')

    op.drop_index('ix_users_email_lower', table_name='users')
    op.drop_index('ix_users_team_id', table_name='users')
    op.drop_table('users')

    op.drop_table('auth_providers')

    op.drop_table('teams')
