"""Rental comparables and stored rental estimates (Slice 3).

Feature tables live in their own `models_<feature>.py` module and are
discovered by `model_modules.load_all()`, so adding a feature never contends
over a shared import list.

Rental evidence is kept separate from `transactions` on purpose: a lease and a
sale are different instruments, they arrive from different sources with
different legal bases, and conflating them would let a rent leak into the
comparable set behind a fair value.
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreoi_persistence.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class RentalComparable(Base):
    """A signed lease used as rental evidence for the yield engine.

    `annual_rent` is always annualised at load time so that nothing downstream
    has to infer a contract term.
    """

    __tablename__ = "rental_comparables"
    __table_args__ = (
        Index("ix_rental_comparables_location", "location", postgresql_using="gist"),
        Index("ix_rental_comparables_lookup", "district_id", "contract_date", "property_class"),
        Index("ix_rental_comparables_ingested", "ingested_at"),
        CheckConstraint("area_sqm > 0", name="ck_rental_comparable_area_positive"),
        CheckConstraint("annual_rent > 0", name="ck_rental_comparable_rent_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"))
    district_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("districts.id"), nullable=True)
    location: Mapped[object] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    annual_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    area_sqm: Mapped[float] = mapped_column(Numeric(10, 2))
    contract_date: Mapped[date] = mapped_column(Date)
    property_class: Mapped[str] = mapped_column(String(32))
    build_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # When we learned of it, as distinct from when it was signed. The alerting
    # "new relevant comparable" trigger needs the former; the recency kernel
    # needs the latter.
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RentalEstimateRow(Base):
    """Append-only rental estimate for one opportunity.

    Like `opportunity_scores` this is inserted and never updated: a stored
    yield must stay reproducible against the assumption set that produced it,
    which is why `assumptions` is written alongside the numbers rather than
    read from configuration at display time.
    """

    __tablename__ = "rental_estimates"
    __table_args__ = (Index("ix_rental_estimates_opportunity", "opportunity_id", "computed_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    annual_rent: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    annual_rent_low: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    annual_rent_high: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    rent_per_sqm_year: Mapped[float] = mapped_column(Numeric(12, 2))
    comparable_count: Mapped[int] = mapped_column(Integer)
    effective_n: Mapped[float] = mapped_column(Numeric(8, 3))
    comparable_quality: Mapped[float] = mapped_column(Numeric(5, 4))
    confidence: Mapped[float] = mapped_column(Numeric(5, 4))
    # NULL when the yield was refused -- never zero, which would read as a fact.
    gross_yield: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    net_yield: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    yield_refused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    opex_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    method_version: Mapped[str] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    comparables: Mapped[list[RentalEstimateComparable]] = relationship(back_populates="estimate")


class RentalEstimateComparable(Base):
    """The leases actually cited, with their weights. Always shown to users."""

    __tablename__ = "rental_estimate_comparables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    rental_estimate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rental_estimates.id"))
    rental_comparable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rental_comparables.id"))
    weight: Mapped[float] = mapped_column(Numeric(6, 5))
    distance_m: Mapped[float] = mapped_column(Numeric(10, 2))
    rent_per_sqm_year: Mapped[float] = mapped_column(Numeric(12, 2))
    weight_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB)
    excluded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    estimate: Mapped[RentalEstimateRow] = relationship(back_populates="comparables")
    comparable: Mapped[RentalComparable] = relationship()
