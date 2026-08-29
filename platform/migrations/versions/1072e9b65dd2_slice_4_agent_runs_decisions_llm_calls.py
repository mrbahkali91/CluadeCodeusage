"""slice 4: agent runs, decisions, llm calls

Revision ID: 1072e9b65dd2
Revises: 5b809391cfe9
Create Date: 2026-08-29 10:52:09.802344
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
import geoalchemy2
from sqlalchemy.dialects import postgresql

revision = '1072e9b65dd2'
down_revision = '5b809391cfe9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('agent', sa.String(length=48), nullable=False),
    sa.Column('subject_type', sa.String(length=32), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=True),
    sa.Column('input_hash', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('duration_ms', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('injection_flagged', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_runs_agent_time', 'agent_runs', ['agent', 'started_at'], unique=False)
    op.create_table('agent_decisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('agent_run_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('llm_calls',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('agent_run_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=48), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('schema_valid', sa.Boolean(), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_llm_calls_time', 'llm_calls', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_llm_calls_time', table_name='llm_calls')
    op.drop_table('llm_calls')
    op.drop_table('agent_decisions')
    op.drop_index('ix_agent_runs_agent_time', table_name='agent_runs')
    op.drop_table('agent_runs')
