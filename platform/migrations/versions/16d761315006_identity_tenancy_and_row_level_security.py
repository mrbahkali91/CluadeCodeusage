"""identity, tenancy and row-level security

Revision ID: 16d761315006
Revises: 00c99f8ffd05
Create Date: 2026-09-04 17:28:07.219741
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
import geoalchemy2


revision = '16d761315006'
down_revision = '00c99f8ffd05'
branch_labels = None
depends_on = None




TENANT_TABLES = (
    "watchlists",
    "watch_rules",
    "alerts",
    "notifications",
    "alert_feedback",
    "investment_memos",
    "documents",
    "document_extractions",
)

DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"


def _bootstrap_default_organization() -> None:
    """Rows that predate tenancy need an owner before NOT NULL can hold."""
    op.execute(
        f"""
        INSERT INTO organizations
            (id, slug, name, weight_profile_version, is_active, created_at)
        VALUES
            ('{DEFAULT_ORG_ID}', 'default', 'Default organisation',
             'default-v1', true, now())
        ON CONFLICT (slug) DO NOTHING
        """
    )
    for table in TENANT_TABLES:
        op.execute(
            f"UPDATE {table} SET organization_id = '{DEFAULT_ORG_ID}' "
            "WHERE organization_id IS NULL"
        )
    for table in TENANT_TABLES:
        op.alter_column(
            table,
            "organization_id",
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_ORG_ID}'"),
        )


def _enable_rls() -> None:
    from sreoi_persistence.rls import enable_row_level_security

    enable_row_level_security(op.get_bind())


def _disable_rls() -> None:
    from sreoi_persistence.rls import disable_row_level_security

    disable_row_level_security(op.get_bind())


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=True),
    sa.Column('actor_subject', sa.String(length=255), nullable=True),
    sa.Column('actor_role', sa.String(length=24), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=200), nullable=True),
    sa.Column('outcome', sa.String(length=24), nullable=False),
    sa.Column('detail', sa.String(length=1000), nullable=True),
    sa.Column('client_ip', sa.String(length=64), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_events_actor', 'audit_events', ['actor_subject'], unique=False)
    op.create_index('ix_audit_events_org_time', 'audit_events', ['organization_id', 'occurred_at'], unique=False)
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('weight_profile_version', sa.String(length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('subject')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prefix')
    )
    op.create_index('ix_api_keys_org', 'api_keys', ['organization_id'], unique=False)
    op.create_table('memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'organization_id', name='uq_membership_user_org')
    )
    op.create_index('ix_memberships_org', 'memberships', ['organization_id'], unique=False)
    op.add_column('alert_feedback', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_alert_feedback_organization_id'), 'alert_feedback', ['organization_id'], unique=False)
    op.add_column('alerts', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_alerts_organization_id'), 'alerts', ['organization_id'], unique=False)
    op.add_column('document_extractions', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_document_extractions_organization_id'), 'document_extractions', ['organization_id'], unique=False)
    op.add_column('documents', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_documents_organization_id'), 'documents', ['organization_id'], unique=False)
    op.add_column('investment_memos', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_investment_memos_organization_id'), 'investment_memos', ['organization_id'], unique=False)
    op.add_column('notifications', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_notifications_organization_id'), 'notifications', ['organization_id'], unique=False)
    op.add_column('watch_rules', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_watch_rules_organization_id'), 'watch_rules', ['organization_id'], unique=False)
    op.add_column('watchlists', sa.Column('organization_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_watchlists_organization_id'), 'watchlists', ['organization_id'], unique=False)
    _bootstrap_default_organization()
    _enable_rls()


def downgrade() -> None:
    _disable_rls()
    op.drop_index(op.f('ix_watchlists_organization_id'), table_name='watchlists')
    op.drop_column('watchlists', 'organization_id')
    op.drop_index(op.f('ix_watch_rules_organization_id'), table_name='watch_rules')
    op.drop_column('watch_rules', 'organization_id')
    op.drop_index(op.f('ix_notifications_organization_id'), table_name='notifications')
    op.drop_column('notifications', 'organization_id')
    op.drop_index(op.f('ix_investment_memos_organization_id'), table_name='investment_memos')
    op.drop_column('investment_memos', 'organization_id')
    op.drop_index(op.f('ix_documents_organization_id'), table_name='documents')
    op.drop_column('documents', 'organization_id')
    op.drop_index(op.f('ix_document_extractions_organization_id'), table_name='document_extractions')
    op.drop_column('document_extractions', 'organization_id')
    op.drop_index(op.f('ix_alerts_organization_id'), table_name='alerts')
    op.drop_column('alerts', 'organization_id')
    op.drop_index(op.f('ix_alert_feedback_organization_id'), table_name='alert_feedback')
    op.drop_column('alert_feedback', 'organization_id')
    op.drop_index('ix_memberships_org', table_name='memberships')
    op.drop_table('memberships')
    op.drop_index('ix_api_keys_org', table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_table('users')
    op.drop_table('organizations')
    op.drop_index('ix_audit_events_org_time', table_name='audit_events')
    op.drop_index('ix_audit_events_actor', table_name='audit_events')
    op.drop_table('audit_events')
