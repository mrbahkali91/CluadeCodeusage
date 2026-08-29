"""API contracts.

Response invariant: a monetary or derived field never serialises as a bare
number. `ProvenancedMoney` carries value, basis, confidence and sources, so an
endpoint physically cannot return an unattributed price (ADR-007).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ProvenancedMoney(BaseModel):
    value: Decimal | None
    unit: str = "SAR"
    basis: str
    confidence: float
    sources: list[str] = Field(default_factory=list)


class CostLineItemOut(BaseModel):
    kind: str
    amount: ProvenancedMoney
    material: bool
    note: str | None = None


class TrueCostOut(BaseModel):
    total: Decimal
    is_complete: bool
    completeness: float
    line_items: list[CostLineItemOut]
    method_version: str


class ComparableOut(BaseModel):
    transaction_id: uuid.UUID
    price: Decimal
    area_sqm: float
    price_per_sqm: float
    adjusted_price_per_sqm: float
    transacted_on: str
    distance_m: float
    weight: float
    weight_breakdown: dict[str, float]
    included: bool
    excluded_reason: str | None = None


class ValuationOut(BaseModel):
    fair_value_low: Decimal
    fair_value_base: Decimal
    fair_value_high: Decimal
    base_price_per_sqm: float
    comparable_count: int
    effective_n: float
    comparable_quality: float
    confidence: float
    index_tier: str
    method_version: str
    is_synthetic_evidence: bool = Field(
        description="True when comparables come from the synthetic demonstration corpus"
    )


class ScoreComponentOut(BaseModel):
    dimension: str
    raw_value: float | None
    normalized_score: float
    weight: float
    contribution: float
    inputs: dict[str, float | str | None]


class ConfidenceGap(BaseModel):
    """What is holding data confidence down, largest shortfall first."""

    input_name: str
    value: float
    weight: float
    shortfall: float
    explanation: str


class ScoreOut(BaseModel):
    total: float
    classification: str
    classification_label: str
    data_confidence: float
    capped: bool
    discount_fraction: float | None
    discount_refused_reason: str | None
    weight_profile_version: str
    method_version: str
    components: list[ScoreComponentOut]
    confidence_gaps: list[ConfidenceGap]


class OpportunitySummary(BaseModel):
    id: uuid.UUID
    title: str
    opportunity_type: str
    district: str | None
    property_class: str
    area_sqm: float
    true_acquisition_cost: Decimal | None
    fair_value_base: Decimal | None
    discount_percent: float | None
    discount_refused_reason: str | None
    price_per_sqm: float | None
    score: float | None
    classification: str | None
    data_confidence: float | None


class OpportunityDetail(OpportunitySummary):
    location_precision: str
    source_key: str
    source_is_synthetic: bool
    valuation: ValuationOut | None
    cost: TrueCostOut
    score_detail: ScoreOut | None
    comparables: list[ComparableOut]


class VerificationCheckOut(BaseModel):
    check_type: str
    check_class: str
    status: str
    summary: str
    evidence: dict[str, Any] | None = None
    checked_at: str


class VerificationOut(BaseModel):
    verification_score: float
    internal_score: float
    official_score: float
    official_available: bool
    ceiling_reason: str | None
    method_version: str
    headline: str | None
    concerns: list[str]
    checks: list[VerificationCheckOut]


class ProvenanceEntry(BaseModel):
    entity_table: str
    field_name: str
    value: str | None
    basis: str
    confidence: float
    source_key: str | None
    evidence: dict[str, str] | None
    observed_at: str


class SubmissionIn(BaseModel):
    """Analyst / broker opportunity submission."""

    external_id: str | None = None
    title: str | None = None
    opportunity_type: str
    property_class: str
    district: str
    area_sqm: float
    bedrooms: int | None = None
    floor: int | None = None
    build_year: int | None = None
    developer_name: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    seller_payment: Decimal | None = None
    remaining_installments: Decimal | None = None
    auction_commission: Decimal | None = None
    brokerage: Decimal | None = None
    transfer_tax: Decimal | None = None
    registration: Decimal | None = None
    renovation: Decimal | None = None
    known_liability: Decimal | None = None
    description: str | None = None

    @field_validator("area_sqm")
    @classmethod
    def _area_range(cls, v: float) -> float:
        # Out of range is an error, never silently clamped.
        if not 20 <= v <= 10_000:
            raise ValueError("area_sqm must be between 20 and 10000")
        return v


class SourceOut(BaseModel):
    key: str
    name: str
    legal_access_method: str
    data_license: str
    availability_label: str
    source_confidence: float
    is_synthetic: bool
    enabled: bool
    record_count: int


class SourceHealthOut(BaseModel):
    source_key: str
    healthy: bool
    checked_at: str
    latency_ms: float | None
    detail: str | None
