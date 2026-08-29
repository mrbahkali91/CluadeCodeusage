"""initial schema

PostGIS owns spatial_ref_sys; migrations must never drop or recreate it.

Revision ID: ea857cbc0279
Revises: 
Create Date: 2026-08-29 07:41:58.750966
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects import postgresql

revision = 'ea857cbc0279'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('districts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('city', sa.String(length=80), nullable=False),
    sa.Column('name_en', sa.String(length=120), nullable=False),
    sa.Column('name_ar', sa.String(length=120), nullable=False),
    sa.Column('centroid', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('boundary', geoalchemy2.types.Geography(geometry_type='POLYGON', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('boundary_precision', sa.String(length=24), nullable=False),
    sa.Column('liquidity_score', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('location_score', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_districts_boundary', 'districts', ['boundary'], unique=False, postgresql_using='gist')
    op.create_index('ix_districts_centroid', 'districts', ['centroid'], unique=False, postgresql_using='gist')
    op.create_table('sources',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('legal_access_method', sa.String(length=40), nullable=False),
    sa.Column('data_license', sa.Text(), nullable=False),
    sa.Column('availability_label', sa.String(length=32), nullable=False),
    sa.Column('source_confidence', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.Column('is_synthetic', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_table('price_index_points',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.String(length=80), nullable=False),
    sa.Column('sector', sa.String(length=120), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('value', sa.Numeric(precision=10, scale=3), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tier', 'scope', 'sector', 'period', name='uq_index_point')
    )
    op.create_table('source_records',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('retrieved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('verification_status', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'content_hash', name='uq_source_record_content')
    )
    op.create_table('transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('district_id', sa.UUID(), nullable=True),
    sa.Column('location', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('area_sqm', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('transacted_on', sa.Date(), nullable=False),
    sa.Column('property_class', sa.String(length=32), nullable=False),
    sa.Column('build_year', sa.Integer(), nullable=True),
    sa.Column('floor', sa.Integer(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.CheckConstraint('area_sqm > 0', name='ck_transaction_area_positive'),
    sa.CheckConstraint('price > 0', name='ck_transaction_price_positive'),
    sa.ForeignKeyConstraint(['district_id'], ['districts.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_transactions_location', 'transactions', ['location'], unique=False, postgresql_using='gist')
    op.create_index('ix_transactions_lookup', 'transactions', ['district_id', 'transacted_on', 'property_class'], unique=False)
    op.create_table('data_provenance',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('entity_table', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('field_name', sa.String(length=64), nullable=False),
    sa.Column('value_text', sa.Text(), nullable=True),
    sa.Column('basis', sa.String(length=16), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('source_record_id', sa.UUID(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("basis <> 'UNKNOWN' OR value_text IS NULL", name='ck_unknown_has_no_value'),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_provenance_entity', 'data_provenance', ['entity_table', 'entity_id'], unique=False)
    op.create_table('properties',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('property_class', sa.String(length=32), nullable=False),
    sa.Column('district_id', sa.UUID(), nullable=True),
    sa.Column('location', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('location_precision', sa.String(length=16), nullable=False),
    sa.Column('built_area_sqm', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('bedrooms', sa.Integer(), nullable=True),
    sa.Column('floor', sa.Integer(), nullable=True),
    sa.Column('build_year', sa.Integer(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('developer_name', sa.String(length=160), nullable=True),
    sa.Column('source_record_id', sa.UUID(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('built_area_sqm > 0', name='ck_property_area_positive'),
    sa.ForeignKeyConstraint(['district_id'], ['districts.id'], ),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_properties_location', 'properties', ['location'], unique=False, postgresql_using='gist')
    op.create_table('opportunities',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('source_record_id', sa.UUID(), nullable=False),
    sa.Column('opportunity_type', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('opportunity_scores',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('total_score', sa.Numeric(precision=7, scale=4), nullable=False),
    sa.Column('classification', sa.String(length=24), nullable=False),
    sa.Column('data_confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('capped', sa.Boolean(), nullable=False),
    sa.Column('discount_fraction', sa.Numeric(precision=7, scale=5), nullable=True),
    sa.Column('discount_refused_reason', sa.Text(), nullable=True),
    sa.Column('weight_profile_version', sa.String(length=32), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('true_acquisition_costs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('is_complete', sa.Boolean(), nullable=False),
    sa.Column('completeness', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('valuations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('fair_value_low', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('fair_value_base', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('fair_value_high', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('base_price_per_sqm', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('comparable_count', sa.Integer(), nullable=False),
    sa.Column('effective_n', sa.Numeric(precision=8, scale=3), nullable=False),
    sa.Column('comparable_quality', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('index_tier', sa.String(length=16), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('verification_checks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('check_type', sa.String(length=48), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status <> 'VERIFIED' OR evidence IS NOT NULL", name='ck_verified_requires_evidence'),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cost_line_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('cost_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('basis', sa.String(length=16), nullable=False),
    sa.Column('material', sa.Boolean(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['cost_id'], ['true_acquisition_costs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('score_components',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('score_id', sa.UUID(), nullable=False),
    sa.Column('dimension', sa.String(length=24), nullable=False),
    sa.Column('raw_value', sa.Numeric(precision=12, scale=5), nullable=True),
    sa.Column('normalized_score', sa.Numeric(precision=7, scale=4), nullable=False),
    sa.Column('weight', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['score_id'], ['opportunity_scores.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('valuation_comparables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('valuation_id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('weight', sa.Numeric(precision=6, scale=5), nullable=False),
    sa.Column('distance_m', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('adjusted_price_per_sqm', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('weight_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('excluded_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.ForeignKeyConstraint(['valuation_id'], ['valuations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('valuation_comparables')
    op.drop_table('score_components')
    op.drop_table('cost_line_items')
    op.drop_table('verification_checks')
    op.drop_table('valuations')
    op.drop_table('true_acquisition_costs')
    op.drop_table('opportunity_scores')
    op.drop_table('opportunities')
    op.drop_index('ix_properties_location', table_name='properties', postgresql_using='gist')
    op.drop_table('properties')
    op.drop_index('ix_provenance_entity', table_name='data_provenance')
    op.drop_table('data_provenance')
    op.drop_index('ix_transactions_lookup', table_name='transactions')
    op.drop_index('ix_transactions_location', table_name='transactions', postgresql_using='gist')
    op.drop_table('transactions')
    op.drop_table('source_records')
    op.drop_table('price_index_points')
    op.drop_table('sources')
    op.drop_index('ix_districts_centroid', table_name='districts', postgresql_using='gist')
    op.drop_index('ix_districts_boundary', table_name='districts', postgresql_using='gist')
    op.drop_table('districts')
