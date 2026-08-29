"""The evaluation pipeline: normalized record -> scored opportunity.

Deterministic end to end. Each stage writes a typed artifact and its provenance,
so the resulting opportunity page can answer "where did this number come from?"
for every field it displays.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from sreoi_domain.cost import (
    CostKind,
    CostLineItem,
    Discount,
    DiscountRefused,
    TrueAcquisitionCost,
    build_cost,
    compute_discount,
)
from sreoi_domain.provenance import Basis, Money, Provenanced, actual, estimate, rule, unknown
from sreoi_domain.risk import RiskAssessment, RiskDimension, RiskLevel
from sreoi_domain.scoring import (
    DEFAULT_WEIGHTS,
    ConfidenceInputs,
    OpportunityScore,
    WeightProfile,
    score_opportunity,
)
from sreoi_domain.valuation import (
    FairValue,
    IndexTier,
    InsufficientComparablesError,
    SubjectProperty,
    value_property,
)
from sreoi_persistence.models import (
    CostLineItemRow,
    DataProvenance,
    Opportunity,
    OpportunityScoreRow,
    Property,
    ScoreComponentRow,
    SourceRecord,
    TrueAcquisitionCostRow,
    Valuation,
    ValuationComparable,
)
from sreoi_persistence.repositories import (
    ComparableRepository,
    PriceIndexRepository,
    PropertyRepository,
    SourceRepository,
)

# The sector series used for time adjustment. Residential apartments are the
# MVP scope; other classes map to their own series as coverage expands.
SECTOR_BY_CLASS = {
    "APARTMENT": "Residential: Apartment",
    "VILLA": "Residential: Villa",
    "RESIDENTIAL_PLOT": "Residential: Residential Plot",
}
FALLBACK_SECTOR = "Residential: Total"


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    opportunity_id: uuid.UUID
    fair_value: FairValue | None
    cost: TrueAcquisitionCost
    discount: Discount | DiscountRefused
    score: OpportunityScore | None
    failure: str | None = None


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _money(value: Any, basis_if_known: Basis, *, kind: str) -> Money:
    """Turn a submitted figure into a provenanced money value.

    A missing figure becomes UNKNOWN, never zero. That distinction is what
    stops a discount being computed against an incomplete cost.
    """
    if value in (None, "", "unknown"):
        return unknown(f"{kind} not supplied")
    amount = Decimal(str(value))
    if basis_if_known is Basis.RULE:
        return rule(amount, rule_id=f"cost-rule/{kind}")
    if basis_if_known is Basis.ESTIMATE:
        return estimate(amount, confidence=0.6, method=f"cost-estimate/{kind}")
    return actual(amount)


def build_cost_from_submission(data: dict[str, Any]) -> TrueAcquisitionCost:
    """Assemble the itemised acquisition cost from an analyst submission."""
    items: list[CostLineItem] = []

    payment = data.get("seller_payment")
    if data.get("opportunity_type") == "AUCTION":
        amount = _money(payment, Basis.ACTUAL, kind="winning_bid")
        items.append(CostLineItem(CostKind.WINNING_BID, amount))
    else:
        amount = _money(payment, Basis.ACTUAL, kind="seller_payment")
        items.append(CostLineItem(CostKind.SELLER_PAYMENT, amount))

    # The assignment killer. Off-plan and assignment opportunities carry a
    # remaining balance to the developer; omitting it produces an absurd discount.
    needs_installments = data.get("opportunity_type") in {"ASSIGNMENT", "OFF_PLAN_RESALE"}
    installments = data.get("remaining_installments")
    if needs_installments or installments not in (None, ""):
        items.append(
            CostLineItem(
                CostKind.REMAINING_INSTALLMENTS,
                _money(installments, Basis.ACTUAL, kind="remaining_installments"),
                material=True,
            )
        )

    for kind, key, basis in (
        (CostKind.AUCTION_COMMISSION, "auction_commission", Basis.RULE),
        (CostKind.BROKERAGE, "brokerage", Basis.RULE),
        (CostKind.TRANSFER_TAX, "transfer_tax", Basis.RULE),
        (CostKind.REGISTRATION, "registration", Basis.RULE),
        (CostKind.RENOVATION, "renovation", Basis.ESTIMATE),
        (CostKind.KNOWN_LIABILITY, "known_liability", Basis.ACTUAL),
    ):
        value = data.get(key)
        if value in (None, ""):
            continue
        items.append(CostLineItem(kind, _money(value, basis, kind=key), material=False))

    return build_cost(items)


def _record_provenance(
    session: Session,
    *,
    table: str,
    entity_id: uuid.UUID,
    field_name: str,
    prov: Provenanced[Any],
    source_record_id: uuid.UUID | None = None,
) -> None:
    session.add(
        DataProvenance(
            entity_table=table,
            entity_id=entity_id,
            field_name=field_name,
            value_text=None if prov.value is None else str(prov.value),
            basis=prov.basis.value,
            confidence=prov.confidence,
            source_record_id=source_record_id,
            evidence=(
                {"kind": prov.evidence.kind, "locator": prov.evidence.locator}
                if prov.evidence
                else None
            ),
        )
    )


def evaluate_opportunity(
    session: Session,
    opportunity: Opportunity,
    submission: dict[str, Any],
    *,
    as_of: date | None = None,
    profile: WeightProfile = DEFAULT_WEIGHTS,
) -> EvaluationResult:
    """Run the full evaluation DAG for one opportunity."""
    as_of = as_of or datetime.now(UTC).date()
    prop: Property = opportunity.property

    comparables_repo = ComparableRepository(session)
    index_repo = PriceIndexRepository(session)

    longitude, latitude = PropertyRepository(session).coordinates(prop.id)

    subject = SubjectProperty(
        property_class=prop.property_class,
        area_sqm=float(prop.built_area_sqm),
        district_id=prop.district_id,
        project_id=prop.project_id,
        build_year=prop.build_year,
        floor=prop.floor,
    )

    comps, radius_used = comparables_repo.find(
        longitude=longitude,
        latitude=latitude,
        property_class=prop.property_class,
        area_sqm=subject.area_sqm,
        as_of=as_of,
    )

    # Time adjustment: index each comparable forward to today.
    sector = SECTOR_BY_CLASS.get(prop.property_class, FALLBACK_SECTOR)
    series = index_repo.series(sector) or index_repo.series(FALLBACK_SECTOR)
    index_tier = IndexTier.NATIONAL if series else IndexTier.NONE
    index_now = None
    index_at: dict[uuid.UUID, float] = {}
    if series:
        latest_period = max(series)
        index_now = series[latest_period]
        for comp in comps:
            period = f"{comp.transacted_on.year:04d}-{comp.transacted_on.month:02d}"
            value = series.get(period) or _nearest_period(series, period)
            if value is not None:
                index_at[comp.id] = value

    # Confidence is penalised when the search had to reach beyond the neighbourhood.
    completeness = _field_completeness(prop)
    fair_value: FairValue | None = None
    failure: str | None = None
    try:
        fair_value = value_property(
            subject,
            comps,
            as_of=as_of,
            index_now=index_now,
            index_at=index_at,
            index_tier=index_tier,
            subject_completeness=completeness,
        )
    except InsufficientComparablesError as exc:
        failure = str(exc)

    cost = build_cost_from_submission(submission)

    discount: Discount | DiscountRefused
    if fair_value is None:
        discount = DiscountRefused(tuple(i.kind for i in cost.unknown_material_items))
    else:
        discount = compute_discount(fair_value.base, cost)

    # Persist the cost with every line item and its basis.
    cost_row = TrueAcquisitionCostRow(
        opportunity_id=opportunity.id,
        total=cost.total,
        is_complete=cost.is_complete,
        completeness=cost.completeness,
        method_version=cost.method_version,
    )
    session.add(cost_row)
    session.flush()
    for item in cost.line_items:
        session.add(
            CostLineItemRow(
                cost_id=cost_row.id,
                kind=item.kind.value,
                amount=item.amount.value,
                basis=item.amount.basis.value,
                material=item.is_material,
                note=item.note,
            )
        )
        _record_provenance(
            session,
            table="cost_line_items",
            entity_id=cost_row.id,
            field_name=item.kind.value,
            prov=item.amount,
            source_record_id=opportunity.source_record_id,
        )

    valuation_row: Valuation | None = None
    if fair_value is not None:
        valuation_row = Valuation(
            opportunity_id=opportunity.id,
            property_id=prop.id,
            fair_value_low=fair_value.low,
            fair_value_base=fair_value.base,
            fair_value_high=fair_value.high,
            base_price_per_sqm=fair_value.base_price_per_sqm,
            comparable_count=fair_value.comparable_count,
            effective_n=fair_value.effective_n,
            comparable_quality=fair_value.comparable_quality,
            confidence=fair_value.confidence,
            index_tier=fair_value.index_tier.value,
            method_version=fair_value.method_version,
        )
        session.add(valuation_row)
        session.flush()
        for wc in fair_value.comparables:
            session.add(
                ValuationComparable(
                    valuation_id=valuation_row.id,
                    transaction_id=wc.comparable.id,
                    weight=round(wc.weight, 5),
                    distance_m=wc.comparable.distance_m,
                    adjusted_price_per_sqm=wc.adjusted_price_per_sqm,
                    weight_breakdown={k: round(v, 5) for k, v in wc.weight_breakdown.items()},
                    excluded_reason=wc.excluded_reason,
                )
            )
        _record_provenance(
            session,
            table="valuations",
            entity_id=valuation_row.id,
            field_name="fair_value_base",
            prov=estimate(
                fair_value.base,
                confidence=fair_value.confidence,
                method=f"{fair_value.method_version}/radius={radius_used:.0f}m",
            ),
        )

    district = prop.district
    risk = _assess_risk(fair_value, cost, district_known=district is not None)

    source_confidence = _source_confidence(session, opportunity)
    confidence_inputs = ConfidenceInputs(
        valuation_confidence=fair_value.confidence if fair_value else 0.0,
        cost_completeness=cost.completeness,
        verification_score=0.0,  # Slice 4 wires the verification agent
        source_confidence=source_confidence,
        field_completeness=completeness,
    )

    score = score_opportunity(
        discount_fraction=discount.fraction if isinstance(discount, Discount) else None,
        gross_yield=None,  # rental engine lands in Slice 3
        liquidity=float(district.liquidity_score) if district else 50.0,
        location=float(district.location_score) if district else 50.0,
        developer=50.0 if not prop.developer_name else 65.0,
        risk_score=risk.score,
        confidence=confidence_inputs,
        profile=profile,
    )

    score_row = OpportunityScoreRow(
        opportunity_id=opportunity.id,
        total_score=score.total,
        classification=score.classification.value,
        data_confidence=score.data_confidence,
        capped=score.capped,
        discount_fraction=discount.fraction if isinstance(discount, Discount) else None,
        discount_refused_reason=(
            discount.reason if isinstance(discount, DiscountRefused) else None
        ),
        weight_profile_version=score.weight_profile_version,
        method_version=score.method_version,
    )
    session.add(score_row)
    session.flush()
    for component in score.components:
        session.add(
            ScoreComponentRow(
                score_id=score_row.id,
                dimension=component.dimension.value,
                raw_value=component.raw_value,
                normalized_score=component.normalized_score,
                weight=component.weight,
                inputs=component.inputs,
            )
        )

    return EvaluationResult(
        opportunity_id=opportunity.id,
        fair_value=fair_value,
        cost=cost,
        discount=discount,
        score=score,
        failure=failure,
    )


def _nearest_period(series: dict[str, float], period: str) -> float | None:
    """Quarterly series do not have every month; fall back to the closest prior."""
    candidates = sorted(p for p in series if p <= period)
    if candidates:
        return series[candidates[-1]]
    later = sorted(series)
    return series[later[0]] if later else None


def _field_completeness(prop: Property) -> float:
    fields = [prop.bedrooms, prop.floor, prop.build_year, prop.district_id, prop.developer_name]
    return sum(1 for f in fields if f is not None) / len(fields)


def _source_confidence(session: Session, opportunity: Opportunity) -> float:
    record = session.get(SourceRecord, opportunity.source_record_id)
    if record is None:
        return 0.5
    source = SourceRepository(session).by_key(record.source.key)
    return float(source.source_confidence) if source else 0.5


def _assess_risk(
    fair_value: FairValue | None, cost: TrueAcquisitionCost, *, district_known: bool
) -> RiskAssessment:
    levels: dict[RiskDimension, RiskLevel] = {
        RiskDimension.LEGAL: RiskLevel.LOW,
        RiskDimension.OCCUPANCY: RiskLevel.MEDIUM,
        RiskDimension.DEVELOPER: RiskLevel.MEDIUM,
        RiskDimension.CONSTRUCTION: RiskLevel.LOW,
        RiskDimension.LIQUIDITY: RiskLevel.LOW if district_known else RiskLevel.MEDIUM,
        RiskDimension.MARKET: RiskLevel.MEDIUM,
        RiskDimension.VALUATION_UNCERTAINTY: (
            RiskLevel.HIGH
            if fair_value is None
            else RiskLevel.LOW
            if fair_value.confidence >= 0.75
            else RiskLevel.MEDIUM
        ),
        RiskDimension.DATA_QUALITY: RiskLevel.LOW if cost.is_complete else RiskLevel.HIGH,
        RiskDimension.AUCTION: RiskLevel.MEDIUM,
    }
    return RiskAssessment(levels=levels)
