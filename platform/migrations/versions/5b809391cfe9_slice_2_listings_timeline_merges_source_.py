"""slice 2: listings, timeline, merges, source health

Revision ID: 5b809391cfe9
Revises: ea857cbc0279
Create Date: 2026-08-29 10:37:47.457546
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects import postgresql

revision = '5b809391cfe9'
down_revision = 'ea857cbc0279'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('source_health_checks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('healthy', sa.Boolean(), nullable=False),
    sa.Column('latency_ms', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_health_source_time', 'source_health_checks', ['source_id', 'checked_at'], unique=False)
    op.create_table('listings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'external_id', name='uq_listing_source_external')
    )
    op.create_table('property_merges',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('winner_property_id', sa.UUID(), nullable=False),
    sa.Column('candidate_property_id', sa.UUID(), nullable=True),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Numeric(precision=7, scale=6), nullable=False),
    sa.Column('components', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('decided_by', sa.String(length=32), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['candidate_property_id'], ['properties.id'], ),
    sa.ForeignKeyConstraint(['winner_property_id'], ['properties.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('property_timeline',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=40), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_record_id', sa.UUID(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_timeline_property_time', 'property_timeline', ['property_id', 'occurred_at'], unique=False)
    op.create_table('listing_snapshots',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('listing_id', sa.UUID(), nullable=False),
    sa.Column('asking_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('signal_tags', postgresql.ARRAY(sa.String(length=32)), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_listing_snapshots_listing_time', 'listing_snapshots', ['listing_id', 'observed_at'], unique=False)
    op.add_column('properties', sa.Column('unit_number', sa.String(length=40), nullable=True))
    op.add_column('properties', sa.Column('match_text', sa.Text(), nullable=True))
    op.add_column('properties', sa.Column('merged_into_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_properties_merged_into_id', 'properties', 'properties', ['merged_into_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_properties_merged_into_id', 'properties', type_='foreignkey')
    op.drop_column('properties', 'merged_into_id')
    op.drop_column('properties', 'match_text')
    op.drop_column('properties', 'unit_number')
    op.drop_index('ix_listing_snapshots_listing_time', table_name='listing_snapshots')
    op.drop_table('listing_snapshots')
    op.drop_index('ix_timeline_property_time', table_name='property_timeline')
    op.drop_table('property_timeline')
    op.drop_table('property_merges')
    op.drop_table('listings')
    op.drop_index('ix_health_source_time', table_name='source_health_checks')
    op.drop_table('source_health_checks')
