"""parallel tracks: rental, alerts, documents, memos, quality

Revision ID: 00c99f8ffd05
Revises: 1072e9b65dd2
Create Date: 2026-09-04 17:13:38.355794
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects import postgresql

revision = '00c99f8ffd05'
down_revision = '1072e9b65dd2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('backtest_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('valuation_method_version', sa.String(length=32), nullable=False),
    sa.Column('sample_seed', sa.Integer(), nullable=False),
    sa.Column('requested_sample', sa.Integer(), nullable=False),
    sa.Column('min_history_days', sa.Integer(), nullable=False),
    sa.Column('eligible_count', sa.Integer(), nullable=False),
    sa.Column('corpus_count', sa.Integer(), nullable=False),
    sa.Column('held_out_count', sa.Integer(), nullable=False),
    sa.Column('refused_count', sa.Integer(), nullable=False),
    sa.Column('earliest_as_of', sa.Date(), nullable=True),
    sa.Column('latest_as_of', sa.Date(), nullable=True),
    sa.Column('evidence_is_synthetic', sa.Boolean(), nullable=False),
    sa.Column('caveat', sa.Text(), nullable=False),
    sa.Column('median_abs_pct_error', sa.Numeric(precision=8, scale=5), nullable=True),
    sa.Column('mean_abs_pct_error', sa.Numeric(precision=8, scale=5), nullable=True),
    sa.Column('interval_coverage', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('median_interval_width_pct', sa.Numeric(precision=8, scale=5), nullable=True),
    sa.Column('brier_score', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('brier_skill', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('expected_calibration_error', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('point_error_verdict', sa.String(length=16), nullable=False),
    sa.Column('coverage_verdict', sa.String(length=16), nullable=False),
    sa.Column('calibration_verdict', sa.String(length=20), nullable=False),
    sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_ms', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.CheckConstraint('held_out_count >= 0', name='ck_backtest_holdout_non_negative'),
    sa.CheckConstraint('refused_count >= 0 AND refused_count <= held_out_count', name='ck_backtest_refused_within_holdout'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_backtest_runs_started', 'backtest_runs', ['started_at'], unique=False)
    op.create_table('quality_snapshots',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('overall_status', sa.String(length=8), nullable=False),
    sa.Column('evidence_is_synthetic', sa.Boolean(), nullable=False),
    sa.Column('opportunity_count', sa.Integer(), nullable=False),
    sa.Column('property_count', sa.Integer(), nullable=False),
    sa.Column('field_completeness', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('mean_data_confidence', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('insufficient_data_rate', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('refused_discount_rate', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('duplicate_resolution_rate', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('agent_disagreement_rate', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('verification_pass_rate', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('stalest_source_age_seconds', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_quality_snapshots_captured', 'quality_snapshots', ['captured_at'], unique=False)
    op.create_table('watchlists',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_ref', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_watchlists_owner', 'watchlists', ['owner_ref'], unique=False)
    op.create_table('rental_comparables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('district_id', sa.UUID(), nullable=True),
    sa.Column('location', geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography', nullable=False), nullable=False),
    sa.Column('annual_rent', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('area_sqm', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('contract_date', sa.Date(), nullable=False),
    sa.Column('property_class', sa.String(length=32), nullable=False),
    sa.Column('build_year', sa.Integer(), nullable=True),
    sa.Column('floor', sa.Integer(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('annual_rent > 0', name='ck_rental_comparable_rent_positive'),
    sa.CheckConstraint('area_sqm > 0', name='ck_rental_comparable_area_positive'),
    sa.ForeignKeyConstraint(['district_id'], ['districts.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_rental_comparables_ingested', 'rental_comparables', ['ingested_at'], unique=False)
    op.create_index('ix_rental_comparables_location', 'rental_comparables', ['location'], unique=False, postgresql_using='gist')
    op.create_index('ix_rental_comparables_lookup', 'rental_comparables', ['district_id', 'contract_date', 'property_class'], unique=False)
    op.create_table('watch_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('watchlist_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('districts', postgresql.ARRAY(sa.String(length=120)), nullable=False),
    sa.Column('opportunity_types', postgresql.ARRAY(sa.String(length=40)), nullable=False),
    sa.Column('property_class', sa.String(length=32), nullable=True),
    sa.Column('max_true_acquisition_cost', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('min_discount_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('min_score', sa.Numeric(precision=7, scale=4), nullable=True),
    sa.Column('min_gross_yield', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('polygon', geoalchemy2.types.Geography(geometry_type='POLYGON', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeogFromText', name='geography'), nullable=True),
    sa.Column('triggers', postgresql.ARRAY(sa.String(length=32)), nullable=False),
    sa.Column('channels', postgresql.ARRAY(sa.String(length=16)), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_evaluated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('version >= 1', name='ck_watch_rule_version_positive'),
    sa.ForeignKeyConstraint(['watchlist_id'], ['watchlists.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_watch_rules_polygon', 'watch_rules', ['polygon'], unique=False, postgresql_using='gist')
    op.create_table('backtest_results',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('district_id', sa.UUID(), nullable=True),
    sa.Column('district_name', sa.String(length=120), nullable=True),
    sa.Column('as_of', sa.Date(), nullable=False),
    sa.Column('realised_price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('area_sqm', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('refused', sa.Boolean(), nullable=False),
    sa.Column('refusal_reason', sa.Text(), nullable=True),
    sa.Column('comparable_count', sa.Integer(), nullable=False),
    sa.Column('effective_n', sa.Numeric(precision=8, scale=3), nullable=False),
    sa.Column('predicted_base', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('predicted_low', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('predicted_high', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('signed_pct_error', sa.Numeric(precision=10, scale=5), nullable=True),
    sa.Column('inside_interval', sa.Boolean(), nullable=True),
    sa.CheckConstraint('NOT refused OR predicted_base IS NULL', name='ck_backtest_result_refusal_has_no_prediction'),
    sa.CheckConstraint('refused OR predicted_base IS NOT NULL', name='ck_backtest_result_valued_has_prediction'),
    sa.ForeignKeyConstraint(['district_id'], ['districts.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['backtest_runs.id'], ),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_backtest_results_run', 'backtest_results', ['run_id'], unique=False)
    op.create_table('documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('filename', sa.String(length=260), nullable=False),
    sa.Column('mime_type', sa.String(length=120), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.Integer(), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=False),
    sa.Column('document_class', sa.String(length=32), nullable=False),
    sa.Column('classification_confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('classification_evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('pii_removed', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('pages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_record_id', sa.UUID(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('byte_size > 0', name='ck_documents_byte_size_positive'),
    sa.CheckConstraint('page_count >= 1', name='ck_documents_page_count_positive'),
    sa.ForeignKeyConstraint(['source_record_id'], ['source_records.id'], name='fk_documents_source_record_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_sha256', name='uq_documents_content_sha256')
    )
    op.create_index('ix_documents_class', 'documents', ['document_class'], unique=False)
    op.create_table('document_extractions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('agent_run_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('group_key', sa.String(length=60), nullable=True),
    sa.Column('field_name', sa.String(length=60), nullable=False),
    sa.Column('value_text', sa.Text(), nullable=True),
    sa.Column('value_numeric', sa.Numeric(precision=16, scale=4), nullable=True),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('page_number', sa.Integer(), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(btrim(excerpt)) > 0', name='ck_document_extractions_excerpt'),
    sa.CheckConstraint('page_number >= 1', name='ck_document_extractions_page_cited'),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.id'], name='fk_document_extractions_agent_run_id'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name='fk_document_extractions_document_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_document_extractions_doc', 'document_extractions', ['document_id', 'kind'], unique=False)
    op.create_table('alerts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('watchlist_id', sa.UUID(), nullable=False),
    sa.Column('watch_rule_id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('rule_version', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("btrim(reason) <> ''", name='ck_alert_has_reason'),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.ForeignKeyConstraint(['watch_rule_id'], ['watch_rules.id'], ),
    sa.ForeignKeyConstraint(['watchlist_id'], ['watchlists.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key', name='uq_alert_dedupe_key')
    )
    op.create_index('ix_alerts_opportunity', 'alerts', ['opportunity_id'], unique=False)
    op.create_index('ix_alerts_rule_time', 'alerts', ['watch_rule_id', 'created_at'], unique=False)
    op.create_table('rental_estimates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('annual_rent', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('annual_rent_low', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('annual_rent_high', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('rent_per_sqm_year', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('comparable_count', sa.Integer(), nullable=False),
    sa.Column('effective_n', sa.Numeric(precision=8, scale=3), nullable=False),
    sa.Column('comparable_quality', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('gross_yield', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('net_yield', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('yield_refused_reason', sa.Text(), nullable=True),
    sa.Column('opex_total', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('assumptions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('method_version', sa.String(length=32), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_rental_estimates_opportunity', 'rental_estimates', ['opportunity_id', 'computed_at'], unique=False)
    op.create_table('alert_feedback',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('alert_id', sa.UUID(), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("action IN ('ACKNOWLEDGED','USEFUL','NOT_USEFUL')", name='ck_alert_feedback_action'),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('investment_memos',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('opportunity_id', sa.UUID(), nullable=False),
    sa.Column('locale', sa.String(length=2), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('score_id', sa.UUID(), nullable=True),
    sa.Column('valuation_id', sa.UUID(), nullable=True),
    sa.Column('cost_id', sa.UUID(), nullable=True),
    sa.Column('score_total', sa.Numeric(precision=7, scale=4), nullable=True),
    sa.Column('data_confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('scoring_method_version', sa.String(length=32), nullable=True),
    sa.Column('valuation_method_version', sa.String(length=32), nullable=True),
    sa.Column('cost_method_version', sa.String(length=32), nullable=True),
    sa.Column('memo_method_version', sa.String(length=32), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('weight_profile_version', sa.String(length=32), nullable=True),
    sa.Column('target_margin', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('max_recommended_purchase_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('decision', sa.String(length=32), nullable=True),
    sa.Column('sections', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('figures', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('agent_run_id', sa.UUID(), nullable=True),
    sa.Column('provider', sa.String(length=48), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("locale IN ('en', 'ar')", name='ck_memo_locale_known'),
    sa.CheckConstraint("status <> 'GENERATED' OR (sections IS NOT NULL AND figures IS NOT NULL AND max_recommended_purchase_price IS NOT NULL AND decision IS NOT NULL)", name='ck_memo_generated_is_complete'),
    sa.CheckConstraint("status = 'GENERATED' OR reason IS NOT NULL", name='ck_memo_absence_has_a_reason'),
    sa.CheckConstraint("status IN ('GENERATED', 'NOT_GENERATED', 'REJECTED')", name='ck_memo_status_known'),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.id'], ),
    sa.ForeignKeyConstraint(['cost_id'], ['true_acquisition_costs.id'], ),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.ForeignKeyConstraint(['score_id'], ['opportunity_scores.id'], ),
    sa.ForeignKeyConstraint(['valuation_id'], ['valuations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_investment_memos_lookup', 'investment_memos', ['opportunity_id', 'locale', 'generated_at'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('alert_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('alert_id', 'channel', name='uq_notification_alert_channel')
    )
    op.create_index('ix_notifications_status', 'notifications', ['status'], unique=False)
    op.create_table('rental_estimate_comparables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rental_estimate_id', sa.UUID(), nullable=False),
    sa.Column('rental_comparable_id', sa.UUID(), nullable=False),
    sa.Column('weight', sa.Numeric(precision=6, scale=5), nullable=False),
    sa.Column('distance_m', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('rent_per_sqm_year', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('weight_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('excluded_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['rental_comparable_id'], ['rental_comparables.id'], ),
    sa.ForeignKeyConstraint(['rental_estimate_id'], ['rental_estimates.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('rental_estimate_comparables')
    op.drop_index('ix_notifications_status', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_investment_memos_lookup', table_name='investment_memos')
    op.drop_table('investment_memos')
    op.drop_table('alert_feedback')
    op.drop_index('ix_rental_estimates_opportunity', table_name='rental_estimates')
    op.drop_table('rental_estimates')
    op.drop_index('ix_alerts_rule_time', table_name='alerts')
    op.drop_index('ix_alerts_opportunity', table_name='alerts')
    op.drop_table('alerts')
    op.drop_index('ix_document_extractions_doc', table_name='document_extractions')
    op.drop_table('document_extractions')
    op.drop_index('ix_documents_class', table_name='documents')
    op.drop_table('documents')
    op.drop_index('ix_backtest_results_run', table_name='backtest_results')
    op.drop_table('backtest_results')
    op.drop_index('ix_watch_rules_polygon', table_name='watch_rules', postgresql_using='gist')
    op.drop_table('watch_rules')
    op.drop_index('ix_rental_comparables_lookup', table_name='rental_comparables')
    op.drop_index('ix_rental_comparables_location', table_name='rental_comparables', postgresql_using='gist')
    op.drop_index('ix_rental_comparables_ingested', table_name='rental_comparables')
    op.drop_table('rental_comparables')
    op.drop_index('ix_watchlists_owner', table_name='watchlists')
    op.drop_table('watchlists')
    op.drop_index('ix_quality_snapshots_captured', table_name='quality_snapshots')
    op.drop_table('quality_snapshots')
    op.drop_index('ix_backtest_runs_started', table_name='backtest_runs')
    op.drop_table('backtest_runs')
