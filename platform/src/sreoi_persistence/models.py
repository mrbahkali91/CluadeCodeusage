"""SQLAlchemy models. PostgreSQL 16 + PostGIS (ADR-002).

Two invariants are structural here rather than conventional:
  * append-only history -- listing/score history is inserted, never updated,
    because the *sequence* of price changes is itself the opportunity signal;
  * verification requires evidence -- enforced by a CHECK constraint, because
    an agent's output is not a trustworthy place to put a safety property.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # NOT NULL by design: a source without a recorded legal basis cannot exist.
    legal_access_method: Mapped[str] = mapped_column(String(40))
    data_license: Mapped[str] = mapped_column(Text)
    availability_label: Mapped[str] = mapped_column(String(32))
    source_confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0.5)
    is_synthetic: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    records: Mapped[list[SourceRecord]] = relationship(back_populates="source")


class SourceRecord(Base):
    """Immutable raw payload. Stored before anything interprets it."""

    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_source_record_content"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    verification_status: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")

    source: Mapped[Source] = relationship(back_populates="records")


class District(Base):
    __tablename__ = "districts"
    __table_args__ = (
        Index("ix_districts_boundary", "boundary", postgresql_using="gist"),
        Index("ix_districts_centroid", "centroid", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    city: Mapped[str] = mapped_column(String(80))
    name_en: Mapped[str] = mapped_column(String(120))
    name_ar: Mapped[str] = mapped_column(String(120))
    centroid: Mapped[object] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    boundary: Mapped[object | None] = mapped_column(
        Geography("POLYGON", srid=4326, spatial_index=False), nullable=True
    )
    boundary_precision: Mapped[str] = mapped_column(String(24), default="APPROXIMATE")
    liquidity_score: Mapped[float] = mapped_column(Numeric(5, 2), default=50)
    location_score: Mapped[float] = mapped_column(Numeric(5, 2), default=50)


class Property(Base):
    __tablename__ = "properties"
    __table_args__ = (
        Index("ix_properties_location", "location", postgresql_using="gist"),
        CheckConstraint("built_area_sqm > 0", name="ck_property_area_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    property_class: Mapped[str] = mapped_column(String(32))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"), nullable=True)
    location: Mapped[object] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    location_precision: Mapped[str] = mapped_column(String(16), default="DISTRICT")
    built_area_sqm: Mapped[float] = mapped_column(Numeric(10, 2))
    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    build_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    developer_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    unit_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Normalised text used for trigram similarity during entity resolution.
    match_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Explicitly named: an unnamed self-referential FK cannot be dropped, which
    # silently breaks migration reversibility.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("properties.id", name="fk_properties_merged_into_id"), nullable=True
    )
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_records.id"), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    district: Mapped[District | None] = relationship()


class Transaction(Base):
    """A registered sale. Evidence for the comparable engine."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_location", "location", postgresql_using="gist"),
        Index("ix_transactions_lookup", "district_id", "transacted_on", "property_class"),
        CheckConstraint("area_sqm > 0", name="ck_transaction_area_positive"),
        CheckConstraint("price > 0", name="ck_transaction_price_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"), nullable=True)
    location: Mapped[object] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    area_sqm: Mapped[float] = mapped_column(Numeric(10, 2))
    transacted_on: Mapped[date] = mapped_column(Date)
    property_class: Mapped[str] = mapped_column(String(32))
    build_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class PriceIndexPoint(Base):
    """Time-adjustment series (KAPSARC / GASTAT)."""

    __tablename__ = "price_index_points"
    __table_args__ = (UniqueConstraint("tier", "scope", "sector", "period", name="uq_index_point"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    tier: Mapped[str] = mapped_column(String(16))  # DISTRICT | CITY | NATIONAL
    scope: Mapped[str] = mapped_column(String(80))  # e.g. "Saudi Arabia"
    sector: Mapped[str] = mapped_column(String(120))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    value: Mapped[float] = mapped_column(Numeric(10, 3))


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    source_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_records.id"))
    opportunity_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    property: Mapped[Property] = relationship()
    valuations: Mapped[list[Valuation]] = relationship(back_populates="opportunity")
    scores: Mapped[list[OpportunityScoreRow]] = relationship(back_populates="opportunity")
    costs: Mapped[list[TrueAcquisitionCostRow]] = relationship(back_populates="opportunity")


class Valuation(Base):
    __tablename__ = "valuations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    fair_value_low: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    fair_value_base: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    fair_value_high: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    base_price_per_sqm: Mapped[float] = mapped_column(Numeric(12, 2))
    comparable_count: Mapped[int] = mapped_column(Integer)
    effective_n: Mapped[float] = mapped_column(Numeric(8, 3))
    comparable_quality: Mapped[float] = mapped_column(Numeric(5, 4))
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    index_tier: Mapped[str] = mapped_column(String(16))
    method_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    opportunity: Mapped[Opportunity] = relationship(back_populates="valuations")
    comparables: Mapped[list[ValuationComparable]] = relationship(back_populates="valuation")


class ValuationComparable(Base):
    """The actual comparables cited, with their weights. Always shown to users."""

    __tablename__ = "valuation_comparables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    valuation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("valuations.id"))
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    weight: Mapped[float] = mapped_column(Numeric(6, 5))
    distance_m: Mapped[float] = mapped_column(Numeric(10, 2))
    adjusted_price_per_sqm: Mapped[float] = mapped_column(Numeric(12, 2))
    weight_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB)
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    valuation: Mapped[Valuation] = relationship(back_populates="comparables")
    transaction: Mapped[Transaction] = relationship()


class TrueAcquisitionCostRow(Base):
    __tablename__ = "true_acquisition_costs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    is_complete: Mapped[bool] = mapped_column()
    completeness: Mapped[float] = mapped_column(Numeric(5, 4))
    method_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    opportunity: Mapped[Opportunity] = relationship(back_populates="costs")
    line_items: Mapped[list[CostLineItemRow]] = relationship(back_populates="cost")


class CostLineItemRow(Base):
    __tablename__ = "cost_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    cost_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("true_acquisition_costs.id"))
    kind: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    basis: Mapped[str] = mapped_column(String(16))
    material: Mapped[bool] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    cost: Mapped[TrueAcquisitionCostRow] = relationship(back_populates="line_items")


class OpportunityScoreRow(Base):
    """Append-only. A re-score inserts and supersedes; it never updates."""

    __tablename__ = "opportunity_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    total_score: Mapped[float] = mapped_column(Numeric(7, 4))
    classification: Mapped[str] = mapped_column(String(24))
    data_confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    capped: Mapped[bool] = mapped_column(default=False)
    discount_fraction: Mapped[float | None] = mapped_column(Numeric(7, 5), nullable=True)
    discount_refused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_profile_version: Mapped[str] = mapped_column(String(32))
    method_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opportunity: Mapped[Opportunity] = relationship(back_populates="scores")
    components: Mapped[list[ScoreComponentRow]] = relationship(back_populates="score")


class ScoreComponentRow(Base):
    __tablename__ = "score_components"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    score_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunity_scores.id"))
    dimension: Mapped[str] = mapped_column(String(24))
    raw_value: Mapped[float | None] = mapped_column(Numeric(12, 5), nullable=True)
    normalized_score: Mapped[float] = mapped_column(Numeric(7, 4))
    weight: Mapped[float] = mapped_column(Numeric(5, 4))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)

    score: Mapped[OpportunityScoreRow] = relationship(back_populates="components")


class DataProvenance(Base):
    """Field-level provenance (ADR-007). Answers 'where did this number come from?'"""

    __tablename__ = "data_provenance"
    __table_args__ = (
        Index("ix_provenance_entity", "entity_table", "entity_id"),
        CheckConstraint(
            "basis <> 'UNKNOWN' OR value_text IS NULL",
            name="ck_unknown_has_no_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    entity_table: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field_name: Mapped[str] = mapped_column(String(64))
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    basis: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_records.id"), nullable=True
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VerificationCheck(Base):
    """VERIFIED requires evidence -- enforced by the database, not by an agent."""

    __tablename__ = "verification_checks"
    __table_args__ = (
        CheckConstraint(
            "status <> 'VERIFIED' OR evidence IS NOT NULL",
            name="ck_verified_requires_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    check_type: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16))  # VERIFIED | UNVERIFIED | CONFLICTED | FAILED
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Listing(Base):
    """An advertisement of a property on one source."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_listing_source_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    snapshots: Mapped[list[ListingSnapshot]] = relationship(back_populates="listing")


class ListingSnapshot(Base):
    """Append-only. A price change is a new row, never an update.

    The sequence is the product signal: 950k -> 920k -> 875k -> "urgent" is the
    opportunity. A mutable price column would destroy exactly that.
    """

    __tablename__ = "listing_snapshots"
    __table_args__ = (Index("ix_listing_snapshots_listing_time", "listing_id", "observed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listings.id"))
    asking_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    signal_tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    listing: Mapped[Listing] = relationship(back_populates="snapshots")


class PropertyTimelineEvent(Base):
    """Everything that ever happened to a property, in order."""

    __tablename__ = "property_timeline"
    __table_args__ = (Index("ix_timeline_property_time", "property_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_records.id"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PropertyMerge(Base):
    """A resolution decision. Reversible: `reversed_at` undoes it."""

    __tablename__ = "property_merges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    winner_property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    candidate_property_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Numeric(7, 6))
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    method_version: Mapped[str] = mapped_column(String(32))
    decided_by: Mapped[str] = mapped_column(String(32), default="SYSTEM")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceHealthCheck(Base):
    """A silently dead connector is the failure mode that matters most."""

    __tablename__ = "source_health_checks"
    __table_args__ = (Index("ix_health_source_time", "source_id", "checked_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    healthy: Mapped[bool] = mapped_column()
    latency_ms: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AgentRun(Base):
    """Every agent execution, recorded so it can be reconstructed afterwards."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_agent_time", "agent", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent: Mapped[str] = mapped_column(String(48))
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))  # SUCCEEDED|FAILED|ABORTED|CACHED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    duration_ms: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    injection_flagged: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    decisions: Mapped[list[AgentDecision]] = relationship(back_populates="run")
    llm_calls: Mapped[list[LLMCall]] = relationship(back_populates="run")


class AgentDecision(Base):
    """What the agent concluded, and on what basis."""

    __tablename__ = "agent_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(48))
    outcome: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun] = relationship(back_populates="decisions")


class LLMCall(Base):
    """Cost control is a product requirement, so every call is accounted for."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_time", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"))
    provider: Mapped[str] = mapped_column(String(48))
    model: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    schema_valid: Mapped[bool] = mapped_column(default=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[AgentRun] = relationship(back_populates="llm_calls")
