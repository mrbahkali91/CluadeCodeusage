"""Back-test and data-quality tables (backlog E4.7, E8.4).

Discovered via `model_modules.load_all()` rather than imported from
`models.py`, so adding these tables contends with nobody.

Both kinds of row are append-only for the same reason score rows are: the
*sequence* is the signal. A single quality snapshot says little; a series of
them is how drift becomes visible, and how "coverage fell after the estimator
changed" can be told apart from "coverage was always this bad".

`evidence_is_synthetic` is stored on every row, not derived at read time.
A stored metric that outlives the knowledge of what it was measured on is a
number waiting to be quoted out of context.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreoi_persistence.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class BacktestRun(Base):
    """One execution of the back-testing harness."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_started", "started_at"),
        # The caveat is not optional metadata. A run whose evidence provenance
        # is unrecorded cannot be published, so the column is NOT NULL and the
        # sample size must be positive for the metrics to mean anything.
        CheckConstraint("held_out_count >= 0", name="ck_backtest_holdout_non_negative"),
        CheckConstraint(
            "refused_count >= 0 AND refused_count <= held_out_count",
            name="ck_backtest_refused_within_holdout",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    method_version: Mapped[str] = mapped_column(String(32))
    valuation_method_version: Mapped[str] = mapped_column(String(32))
    # Reproducibility: same seed + same sample size + same corpus => same run.
    sample_seed: Mapped[int] = mapped_column(Integer)
    requested_sample: Mapped[int] = mapped_column(Integer)
    min_history_days: Mapped[int] = mapped_column(Integer)
    eligible_count: Mapped[int] = mapped_column(Integer)
    corpus_count: Mapped[int] = mapped_column(Integer)
    held_out_count: Mapped[int] = mapped_column(Integer)
    refused_count: Mapped[int] = mapped_column(Integer)
    earliest_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)

    evidence_is_synthetic: Mapped[bool] = mapped_column(Boolean)
    caveat: Mapped[str] = mapped_column(Text)

    median_abs_pct_error: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    mean_abs_pct_error: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    interval_coverage: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    median_interval_width_pct: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    brier_score: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    brier_skill: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    expected_calibration_error: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    point_error_verdict: Mapped[str] = mapped_column(String(16))
    coverage_verdict: Mapped[str] = mapped_column(String(16))
    calibration_verdict: Mapped[str] = mapped_column(String(20))

    # Whole report, so a stored run can be re-rendered without recomputation
    # and without the reader having to trust the summary columns above.
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    duration_ms: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    results: Mapped[list[BacktestResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BacktestResult(Base):
    """One held-out sale, its prediction, and its error.

    Kept per case rather than only aggregated: an aggregate cannot be
    re-segmented, and the first question anyone asks of a bad coverage figure
    is "which ones did it miss?".
    """

    __tablename__ = "backtest_results"
    __table_args__ = (
        Index("ix_backtest_results_run", "run_id"),
        CheckConstraint(
            "refused OR predicted_base IS NOT NULL",
            name="ck_backtest_result_valued_has_prediction",
        ),
        CheckConstraint(
            "NOT refused OR predicted_base IS NULL",
            name="ck_backtest_result_refusal_has_no_prediction",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backtest_runs.id"))
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"), nullable=True)
    district_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    as_of: Mapped[date] = mapped_column(Date)
    realised_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    area_sqm: Mapped[float] = mapped_column(Numeric(10, 2))

    refused: Mapped[bool] = mapped_column(Boolean, default=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    comparable_count: Mapped[int] = mapped_column(Integer, default=0)
    effective_n: Mapped[float] = mapped_column(Numeric(8, 3), default=0)

    predicted_base: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    predicted_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    predicted_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    signed_pct_error: Mapped[float | None] = mapped_column(Numeric(10, 5), nullable=True)
    inside_interval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    run: Mapped[BacktestRun] = relationship(back_populates="results")


class QualitySnapshot(Base):
    """A periodic data-quality measurement (E8.4).

    Append-only: the point of a snapshot is comparison with the previous one.
    """

    __tablename__ = "quality_snapshots"
    __table_args__ = (Index("ix_quality_snapshots_captured", "captured_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    method_version: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    overall_status: Mapped[str] = mapped_column(String(8))  # OK | WARN | FAIL
    evidence_is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    opportunity_count: Mapped[int] = mapped_column(Integer, default=0)
    property_count: Mapped[int] = mapped_column(Integer, default=0)
    field_completeness: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    mean_data_confidence: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    insufficient_data_rate: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    refused_discount_rate: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    duplicate_resolution_rate: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    agent_disagreement_rate: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    verification_pass_rate: Mapped[float | None] = mapped_column(Numeric(6, 5), nullable=True)
    stalest_source_age_seconds: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
