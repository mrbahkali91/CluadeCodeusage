"""Read models. Assembles API responses from persisted artifacts."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from sreoi_agents.checkers import METHOD_VERSION as VERIFICATION_METHOD
from sreoi_agents.verification import INTERNAL_WEIGHT_CAP
from sreoi_api.schemas import (
    ComparableOut,
    ConfidenceGap,
    CostLineItemOut,
    OpportunityDetail,
    OpportunitySummary,
    ProvenancedMoney,
    ProvenanceEntry,
    ScoreComponentOut,
    ScoreOut,
    TrueCostOut,
    ValuationOut,
    VerificationCheckOut,
    VerificationOut,
)
from sreoi_domain.scoring import Classification
from sreoi_persistence.models import (
    DataProvenance,
    Opportunity,
    OpportunityScoreRow,
    Source,
    SourceRecord,
    TrueAcquisitionCostRow,
    Valuation,
    VerificationCheck,
)

SYNTHETIC_SOURCE_KEY = "synthetic_fixture"

# Weights of the data-confidence formula (domain scoring spec section 5.3).
_UNAVAILABLE_SUMMARY = {
    "rega_advertisement_licence": "REGA advertisement-licence inquiry not performed.",
    "wafi_project_licence": "Wafi project verification not performed.",
    "developer_registry": "Developer registry lookup not integrated.",
}

CONFIDENCE_WEIGHTS = {
    "valuation_confidence": 0.30,
    "cost_completeness": 0.20,
    "verification_score": 0.20,
    "source_confidence": 0.15,
    "field_completeness": 0.15,
}
CONFIDENCE_EXPLANATIONS = {
    "valuation_confidence": "Comparable evidence is thin or dispersed; more or closer "
    "transactions would raise this.",
    "cost_completeness": "One or more material cost line items is unknown.",
    "verification_score": "No official verification has been performed. The verification "
    "agent lands in Slice 4; until then this input is structurally zero.",
    "source_confidence": "The originating source carries limited trust.",
    "field_completeness": "Property attributes are partly unknown.",
}


def _confidence_gaps(inputs: dict[str, object]) -> list[ConfidenceGap]:
    """Rank what is holding confidence down. A refusal should name what is missing."""
    gaps: list[ConfidenceGap] = []
    for name, weight in CONFIDENCE_WEIGHTS.items():
        raw = inputs.get(name)
        if not isinstance(raw, int | float):
            continue
        value = float(raw)
        shortfall = weight * (1.0 - value)
        if shortfall <= 0.001:
            continue
        gaps.append(
            ConfidenceGap(
                input_name=name,
                value=value,
                weight=weight,
                shortfall=shortfall,
                explanation=CONFIDENCE_EXPLANATIONS[name],
            )
        )
    return sorted(gaps, key=lambda g: -g.shortfall)


def _latest_score(session: Session, opportunity_id: uuid.UUID) -> OpportunityScoreRow | None:
    return session.scalar(
        select(OpportunityScoreRow)
        .where(
            OpportunityScoreRow.opportunity_id == opportunity_id,
            OpportunityScoreRow.superseded_at.is_(None),
        )
        .order_by(OpportunityScoreRow.computed_at.desc())
        .limit(1)
        .options(selectinload(OpportunityScoreRow.components))
    )


def _latest_valuation(session: Session, opportunity_id: uuid.UUID) -> Valuation | None:
    return session.scalar(
        select(Valuation)
        .where(Valuation.opportunity_id == opportunity_id)
        .order_by(Valuation.computed_at.desc())
        .limit(1)
        .options(selectinload(Valuation.comparables))
    )


def _latest_cost(session: Session, opportunity_id: uuid.UUID) -> TrueAcquisitionCostRow | None:
    return session.scalar(
        select(TrueAcquisitionCostRow)
        .where(TrueAcquisitionCostRow.opportunity_id == opportunity_id)
        .order_by(TrueAcquisitionCostRow.computed_at.desc())
        .limit(1)
        .options(selectinload(TrueAcquisitionCostRow.line_items))
    )


def summarize(session: Session, opportunity: Opportunity) -> OpportunitySummary:
    score = _latest_score(session, opportunity.id)
    valuation = _latest_valuation(session, opportunity.id)
    cost = _latest_cost(session, opportunity.id)
    district = opportunity.property.district

    return OpportunitySummary(
        id=opportunity.id,
        title=opportunity.title,
        opportunity_type=opportunity.opportunity_type,
        district=district.name_en if district else None,
        property_class=opportunity.property.property_class,
        area_sqm=float(opportunity.property.built_area_sqm),
        true_acquisition_cost=Decimal(cost.total) if cost and cost.is_complete else None,
        fair_value_base=Decimal(valuation.fair_value_base) if valuation else None,
        discount_percent=(
            float(score.discount_fraction) * 100
            if score and score.discount_fraction is not None
            else None
        ),
        discount_refused_reason=score.discount_refused_reason if score else None,
        price_per_sqm=float(valuation.base_price_per_sqm) if valuation else None,
        score=float(score.total_score) if score else None,
        classification=score.classification if score else None,
        data_confidence=float(score.data_confidence) if score else None,
    )


def detail(session: Session, opportunity: Opportunity) -> OpportunityDetail:
    base = summarize(session, opportunity)
    score = _latest_score(session, opportunity.id)
    valuation = _latest_valuation(session, opportunity.id)
    cost = _latest_cost(session, opportunity.id)
    record = session.get(SourceRecord, opportunity.source_record_id)
    source_key = record.source.key if record is not None else "unknown"

    # Which source ids are synthetic, resolved once rather than per comparable.
    synthetic_ids = set(
        session.scalars(select(Source.id).where(Source.is_synthetic.is_(True))).all()
    )

    comparables: list[ComparableOut] = []
    synthetic_evidence = False
    if valuation is not None:
        for comp in sorted(valuation.comparables, key=lambda c: -float(c.weight)):
            txn = comp.transaction
            if txn.source_id in synthetic_ids:
                synthetic_evidence = True
            comparables.append(
                ComparableOut(
                    transaction_id=txn.id,
                    price=Decimal(txn.price),
                    area_sqm=float(txn.area_sqm),
                    price_per_sqm=float(txn.price) / float(txn.area_sqm),
                    adjusted_price_per_sqm=float(comp.adjusted_price_per_sqm),
                    transacted_on=txn.transacted_on.isoformat(),
                    distance_m=float(comp.distance_m),
                    weight=float(comp.weight),
                    weight_breakdown={k: float(v) for k, v in comp.weight_breakdown.items()},
                    included=comp.excluded_reason is None,
                    excluded_reason=comp.excluded_reason,
                )
            )

    cost_out = TrueCostOut(
        total=Decimal(cost.total) if cost else Decimal("0"),
        is_complete=cost.is_complete if cost else False,
        completeness=float(cost.completeness) if cost else 0.0,
        method_version=cost.method_version if cost else "n/a",
        line_items=[
            CostLineItemOut(
                kind=item.kind,
                amount=ProvenancedMoney(
                    value=Decimal(item.amount) if item.amount is not None else None,
                    basis=item.basis,
                    confidence=1.0 if item.basis == "ACTUAL" else 0.6,
                    sources=[source_key],
                ),
                material=item.material,
                note=item.note,
            )
            for item in (cost.line_items if cost else [])
        ],
    )

    return OpportunityDetail(
        **base.model_dump(),
        location_precision=opportunity.property.location_precision,
        source_key=source_key,
        source_is_synthetic=bool(record.source.is_synthetic) if record is not None else False,
        valuation=(
            ValuationOut(
                fair_value_low=Decimal(valuation.fair_value_low),
                fair_value_base=Decimal(valuation.fair_value_base),
                fair_value_high=Decimal(valuation.fair_value_high),
                base_price_per_sqm=float(valuation.base_price_per_sqm),
                comparable_count=valuation.comparable_count,
                effective_n=float(valuation.effective_n),
                comparable_quality=float(valuation.comparable_quality),
                confidence=float(valuation.confidence),
                index_tier=valuation.index_tier,
                method_version=valuation.method_version,
                is_synthetic_evidence=synthetic_evidence,
            )
            if valuation
            else None
        ),
        cost=cost_out,
        score_detail=(
            ScoreOut(
                total=float(score.total_score),
                classification=score.classification,
                classification_label=Classification(score.classification).label,
                data_confidence=float(score.data_confidence),
                capped=score.capped,
                discount_fraction=(
                    float(score.discount_fraction) if score.discount_fraction is not None else None
                ),
                discount_refused_reason=score.discount_refused_reason,
                weight_profile_version=score.weight_profile_version,
                method_version=score.method_version,
                confidence_gaps=_confidence_gaps(
                    next(
                        (c.inputs for c in score.components if c.dimension == "CONFIDENCE"),
                        {},
                    )
                ),
                components=[
                    ScoreComponentOut(
                        dimension=c.dimension,
                        raw_value=float(c.raw_value) if c.raw_value is not None else None,
                        normalized_score=float(c.normalized_score),
                        weight=float(c.weight),
                        contribution=float(c.normalized_score) * float(c.weight),
                        inputs=c.inputs,
                    )
                    for c in sorted(score.components, key=lambda c: -float(c.weight))
                ],
            )
            if score
            else None
        ),
        comparables=comparables,
    )


def verification(session: Session, opportunity: Opportunity) -> VerificationOut | None:
    """Latest verification outcome per check type."""
    rows = session.scalars(
        select(VerificationCheck)
        .where(VerificationCheck.opportunity_id == opportunity.id)
        .order_by(VerificationCheck.checked_at.desc())
    ).all()
    if not rows:
        return None

    latest: dict[str, VerificationCheck] = {}
    for row in rows:
        latest.setdefault(row.check_type, row)

    checks: list[VerificationCheckOut] = []
    internal_applicable = internal_verified = 0
    official_applicable = official_verified = 0
    for row in latest.values():
        evidence = row.evidence or {}
        check_class = str(evidence.get("check_class") or "OFFICIAL")
        counts = row.status in {"VERIFIED", "FAILED", "CONFLICTED"}
        if check_class == "INTERNAL" and counts:
            internal_applicable += 1
            internal_verified += row.status == "VERIFIED"
        elif check_class == "OFFICIAL" and counts:
            official_applicable += 1
            official_verified += row.status == "VERIFIED"
        checks.append(
            VerificationCheckOut(
                check_type=row.check_type,
                check_class=check_class,
                status=row.status,
                summary=str(
                    evidence.get("summary") or _UNAVAILABLE_SUMMARY.get(row.check_type, "")
                ),
                evidence=evidence.get("detail"),
                checked_at=row.checked_at.isoformat(),
            )
        )

    internal = internal_verified / internal_applicable if internal_applicable else 0.0
    official = official_verified / official_applicable if official_applicable else 0.0
    score = INTERNAL_WEIGHT_CAP * internal + (1 - INTERNAL_WEIGHT_CAP) * official

    return VerificationOut(
        verification_score=round(score, 4),
        internal_score=round(internal, 4),
        official_score=round(official, 4),
        official_available=bool(official_applicable),
        ceiling_reason=(
            None
            if official_applicable
            else f"No official register is integrated yet, so verification is capped "
            f"at {INTERNAL_WEIGHT_CAP:.0%} of its full weight."
        ),
        method_version=VERIFICATION_METHOD,
        headline=None,
        concerns=[c.summary for c in checks if c.status in {"FAILED", "CONFLICTED"}],
        checks=sorted(checks, key=lambda c: (c.check_class, c.check_type)),
    )


def provenance(session: Session, opportunity: Opportunity) -> list[ProvenanceEntry]:
    """Every field traces to a source, a basis and a confidence."""
    cost = _latest_cost(session, opportunity.id)
    valuation = _latest_valuation(session, opportunity.id)
    entity_ids = [opportunity.property_id]
    if cost:
        entity_ids.append(cost.id)
    if valuation:
        entity_ids.append(valuation.id)

    rows = session.scalars(
        select(DataProvenance)
        .where(DataProvenance.entity_id.in_(entity_ids))
        .order_by(DataProvenance.entity_table, DataProvenance.field_name)
    )
    out: list[ProvenanceEntry] = []
    for row in rows:
        record = session.get(SourceRecord, row.source_record_id) if row.source_record_id else None
        out.append(
            ProvenanceEntry(
                entity_table=row.entity_table,
                field_name=row.field_name,
                value=row.value_text,
                basis=row.basis,
                confidence=float(row.confidence),
                source_key=record.source.key if record else None,
                evidence=row.evidence,
                observed_at=row.observed_at.isoformat(),
            )
        )
    return out
