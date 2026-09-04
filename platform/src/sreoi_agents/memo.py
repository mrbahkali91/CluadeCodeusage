"""The investment-memo agent.

This is the most expensive stage in the pipeline and the most dangerous one.
It is the only place where the platform speaks in sentences about money, and a
sentence is much easier to over-trust than a table. Two mechanisms therefore
constrain it, and both are enforced rather than asserted.

**A gate.** The memo runs only for opportunities at `score >= 70` and
`data_confidence >= 0.60`. That is simultaneously the cost gate (a memo is
roughly ten times the cost of any other stage, and the average cost per
opportunity target only survives if the population is small) and the
credibility gate (a fluent memo about thin evidence is worse than no memo).
When the gate refuses, the refusal and its reason are recorded: "no memo"
without a reason is indistinguishable from a silent failure.

**A fact table.** Before the agent is called, every figure the memo is allowed
to use is computed from persisted artifacts and put in a fact table keyed by
field reference. The agent's output must cite a field reference for every
figure it reports, *and* every numeral appearing anywhere in its prose must
resolve to a value in that table. A memo that invents a number -- a yield, an
exit price, a rent, a bid -- fails validation, is regenerated once, and is then
abandoned with an error. It is never stored and never displayed.

That includes the headline number. The **maximum recommended purchase price**
is computed here as `fair_value_low * (1 - target_margin)`; the agent narrates
it and cannot move it.

No model is called in this environment. The provider is the offline
`DeterministicProvider` with the rule-based responder below, recorded as
`provider="deterministic-offline"` on every run. The narrative is template
text assembled from the fact table -- it is not model reasoning and must never
be presented as such.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from sreoi_agents.provider import ModelTier
from sreoi_agents.runtime import Agent, AgentContext, AgentError, AgentRuntime, stable_hash
from sreoi_agents.verification import INTERNAL_WEIGHT_CAP
from sreoi_domain.scoring import CONFIDENCE_FLOOR, Classification
from sreoi_persistence.models import (
    AgentRun,
    Listing,
    ListingSnapshot,
    Opportunity,
    OpportunityScoreRow,
    Source,
    SourceRecord,
    Transaction,
    TrueAcquisitionCostRow,
    Valuation,
    ValuationComparable,
    VerificationCheck,
)
from sreoi_persistence.models_memos import InvestmentMemoRow
from sreoi_persistence.models_rental import RentalEstimateRow
from sreoi_sources.redaction import normalize_digits

MEMO_METHOD_VERSION = "memo-v1"
PROMPT_VERSION = "memo-prompt-v1"

# The gate. Both conditions, not either.
SCORE_GATE = 70.0
CONFIDENCE_GATE = CONFIDENCE_FLOOR  # 0.60, the same floor the scoring model uses

# Policy, not a market observation: the margin we insist on between the
# cautious end of the valuation and what we are willing to pay.
DEFAULT_TARGET_MARGIN = 0.15

# A cited figure must match its computed field. The tolerance exists only to
# absorb decimal/float rendering, not to permit disagreement.
FIGURE_TOLERANCE = 0.005

LOCALES = ("en", "ar")
Locale = Literal["en", "ar"]

SECTION_KEYS: tuple[str, ...] = (
    "opportunity",
    "why_now",
    "pricing",
    "comparable_evidence",
    "expected_returns",
    "risks",
    "maximum_purchase_price",
    "questions_before_purchase",
    "decision",
)

# Sections that must cite specific computed fields. A pricing section without
# a price, or a maximum-price section that does not cite the computed maximum,
# is not a memo -- it is prose.
REQUIRED_CITATIONS: dict[str, tuple[str, ...]] = {
    "pricing": ("cost.total", "valuation.fair_value_base"),
    "maximum_purchase_price": ("derived.max_recommended_purchase_price",),
}

DECISIONS = ("PROCEED_TO_DILIGENCE", "INVESTIGATE", "PASS")


class MemoError(RuntimeError):
    """Base class for memo failures."""


class MemoRejectedError(MemoError):
    """The memo could not be produced with every figure resolved. Fail closed."""


class UnresolvableFigureError(ValueError):
    """A memo cited a number that does not resolve to a computed field.

    Raised from `validate_output`, so the runtime records the run as FAILED and
    never writes the output as a decision.
    """


# --------------------------------------------------------------------------
# Structured output


class MemoFigure(BaseModel):
    """A number, and the computed field it came from. Both are required."""

    label: str = Field(max_length=140)
    field_ref: str = Field(max_length=80)
    value: float


class MemoSection(BaseModel):
    key: str = Field(max_length=40)
    heading: str = Field(max_length=140)
    body: list[str] = Field(min_length=1, max_length=8)
    figures: list[MemoFigure] = Field(default_factory=list, max_length=14)


class InvestmentMemo(BaseModel):
    locale: Locale
    decision: Literal["PROCEED_TO_DILIGENCE", "INVESTIGATE", "PASS"]
    sections: list[MemoSection] = Field(min_length=9, max_length=9)


# --------------------------------------------------------------------------
# Inputs, gate and fact table


@dataclass(frozen=True, slots=True)
class MemoInputs:
    """Everything persisted that the memo may draw on."""

    opportunity: Opportunity
    score: OpportunityScoreRow | None
    valuation: Valuation | None
    cost: TrueAcquisitionCostRow | None
    comparables: tuple[ValuationComparable, ...]
    # Track A's rental engine. Read through the persistence layer, which is the
    # sanctioned seam; the memo must reflect a yield once one exists, because a
    # memo that says "no rental data" when there is some is a false memo.
    rental: RentalEstimateRow | None
    checks: tuple[VerificationCheck, ...]
    snapshots: tuple[ListingSnapshot, ...]
    synthetic_evidence: bool
    listing_text: str | None


@dataclass(frozen=True, slots=True)
class MemoGate:
    allowed: bool
    reason: str | None
    score_total: float | None
    data_confidence: float | None


@dataclass(frozen=True, slots=True)
class MemoFacts:
    """The only numbers a memo is permitted to contain."""

    numeric: dict[str, float]
    text: dict[str, str]

    def payload(self) -> dict[str, Any]:
        return {"facts": self.numeric, "text_facts": self.text}


@dataclass(frozen=True, slots=True)
class MemoRecord:
    """Outcome of a memo request, generated or not."""

    row_id: UUID
    opportunity_id: UUID
    locale: str
    status: str
    reason: str | None
    memo: InvestmentMemo | None
    decision: str | None
    max_recommended_purchase_price: Decimal | None
    facts: MemoFacts | None
    attempts: int

    @property
    def generated(self) -> bool:
        return self.status == "GENERATED"


def load_memo_inputs(session: Session, opportunity: Opportunity) -> MemoInputs:
    score = session.scalar(
        select(OpportunityScoreRow)
        .where(
            OpportunityScoreRow.opportunity_id == opportunity.id,
            OpportunityScoreRow.superseded_at.is_(None),
        )
        .order_by(OpportunityScoreRow.computed_at.desc())
        .limit(1)
        .options(selectinload(OpportunityScoreRow.components))
    )
    valuation = session.scalar(
        select(Valuation)
        .where(Valuation.opportunity_id == opportunity.id)
        .order_by(Valuation.computed_at.desc())
        .limit(1)
        .options(selectinload(Valuation.comparables))
    )
    cost = session.scalar(
        select(TrueAcquisitionCostRow)
        .where(TrueAcquisitionCostRow.opportunity_id == opportunity.id)
        .order_by(TrueAcquisitionCostRow.computed_at.desc())
        .limit(1)
        .options(selectinload(TrueAcquisitionCostRow.line_items))
    )
    rental = session.scalar(
        select(RentalEstimateRow)
        .where(RentalEstimateRow.opportunity_id == opportunity.id)
        .order_by(RentalEstimateRow.computed_at.desc())
        .limit(1)
    )
    checks = tuple(
        session.scalars(
            select(VerificationCheck)
            .where(VerificationCheck.opportunity_id == opportunity.id)
            .order_by(VerificationCheck.checked_at.desc())
        ).all()
    )
    snapshots = tuple(
        session.scalars(
            select(ListingSnapshot)
            .join(Listing, Listing.id == ListingSnapshot.listing_id)
            .where(Listing.property_id == opportunity.property_id)
            .order_by(ListingSnapshot.observed_at)
        ).all()
    )

    comparables = tuple(
        sorted(
            (c for c in (valuation.comparables if valuation else []) if c.excluded_reason is None),
            key=lambda c: -float(c.weight),
        )
    )

    record = session.get(SourceRecord, opportunity.source_record_id)
    synthetic = bool(record is not None and record.source.is_synthetic)
    if valuation is not None and not synthetic:
        synthetic = bool(
            session.scalar(
                select(Source.is_synthetic)
                .join(Transaction, Transaction.source_id == Source.id)
                .join(
                    ValuationComparable,
                    ValuationComparable.transaction_id == Transaction.id,
                )
                .where(
                    ValuationComparable.valuation_id == valuation.id,
                    Source.is_synthetic.is_(True),
                )
                .limit(1)
            )
        )

    listing_text = None
    if record is not None:
        payload = record.raw_payload or {}
        parts = [str(payload.get(f) or "") for f in ("title", "description")]
        listing_text = " ".join(p for p in parts if p).strip() or None

    return MemoInputs(
        opportunity=opportunity,
        score=score,
        valuation=valuation,
        cost=cost,
        comparables=comparables,
        rental=rental,
        checks=checks,
        snapshots=snapshots,
        synthetic_evidence=synthetic,
        listing_text=listing_text,
    )


def memo_gate(inputs: MemoInputs) -> MemoGate:
    """Decide whether this opportunity earns a memo, and say why if it does not."""
    score = inputs.score
    if score is None:
        return MemoGate(False, "no opportunity score has been computed", None, None)

    total = float(score.total_score)
    confidence = float(score.data_confidence)

    if confidence < CONFIDENCE_GATE:
        return MemoGate(
            False,
            f"data confidence {confidence:.2f} is below the memo floor of "
            f"{CONFIDENCE_GATE:.2f}; a fluent memo over thin evidence is worse "
            "than no memo",
            total,
            confidence,
        )
    if total < SCORE_GATE:
        return MemoGate(
            False,
            f"opportunity score {total:.1f} is below the memo gate of "
            f"{SCORE_GATE:.0f}; the memo is the most expensive stage and is "
            "reserved for the strongest candidates",
            total,
            confidence,
        )
    if inputs.valuation is None:
        return MemoGate(
            False,
            "no valuation exists, so there is no fair value to anchor a maximum purchase price to",
            total,
            confidence,
        )
    if inputs.cost is None or not inputs.cost.is_complete:
        return MemoGate(
            False,
            "the true acquisition cost is incomplete, so neither a discount "
            "nor a purchase ceiling can be stated",
            total,
            confidence,
        )
    return MemoGate(True, None, total, confidence)


def max_recommended_purchase_price(
    fair_value_low: Decimal, target_margin: float = DEFAULT_TARGET_MARGIN
) -> Decimal:
    """`fair_value_low * (1 - target_margin)`, computed here and nowhere else.

    Deliberately a pure function on a persisted field: the single number a
    reader is most likely to act on must be reproducible without an LLM.
    """
    if fair_value_low <= 0:
        raise ValueError("fair value must be positive to derive a purchase ceiling")
    if not 0.0 <= target_margin < 1.0:
        raise ValueError(f"target margin must be in [0, 1), got {target_margin}")
    factor = Decimal("1") - Decimal(str(target_margin))
    return (fair_value_low * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recommended_decision(
    *, classification: str, cost_total: Decimal, ceiling: Decimal
) -> tuple[str, str]:
    """The decision is deterministic. The agent narrates it, it does not make it.

    An LLM that could upgrade a WATCHLIST property to "proceed" would make the
    classification gate decorative. Returns the decision and a basis *key*, so
    the reason can be rendered in either locale without translating prose.
    """
    if cost_total > ceiling:
        return "PASS", "COST_ABOVE_CEILING"
    if classification in {Classification.EXCEPTIONAL.value, Classification.STRONG.value}:
        return "PROCEED_TO_DILIGENCE", "CLASSIFICATION"
    if classification == Classification.WORTH_REVIEWING.value:
        return "INVESTIGATE", "CLASSIFICATION"
    return "PASS", "CLASSIFICATION"


def _verification_facts(checks: tuple[VerificationCheck, ...]) -> dict[str, float]:
    """Recompute the verification split from the persisted checks.

    Read directly from `verification_checks` rather than through the API read
    model: the agent layer sits below the API layer and must not reach up.
    """
    latest: dict[str, VerificationCheck] = {}
    for row in checks:  # already newest-first
        latest.setdefault(row.check_type, row)

    internal_applicable = internal_verified = 0
    official_applicable = official_verified = 0
    for row in latest.values():
        evidence = row.evidence or {}
        check_class = str(evidence.get("check_class") or "OFFICIAL")
        if row.status not in {"VERIFIED", "FAILED", "CONFLICTED"}:
            continue
        if check_class == "INTERNAL":
            internal_applicable += 1
            internal_verified += row.status == "VERIFIED"
        else:
            official_applicable += 1
            official_verified += row.status == "VERIFIED"

    internal = internal_verified / internal_applicable if internal_applicable else 0.0
    official = official_verified / official_applicable if official_applicable else 0.0
    score = INTERNAL_WEIGHT_CAP * internal + (1 - INTERNAL_WEIGHT_CAP) * official
    return {
        "verification.score_pct": score * 100.0,
        "verification.internal_pct": internal * 100.0,
        "verification.official_pct": official * 100.0,
        "verification.checks_verified": float(internal_verified + official_verified),
        "verification.checks_applicable": float(internal_applicable + official_applicable),
        "verification.checks_total": float(len(latest)),
        "verification.official_applicable": float(official_applicable),
    }


def _listing_facts(snapshots: tuple[ListingSnapshot, ...]) -> dict[str, float]:
    priced = [s for s in snapshots if s.asking_price is not None]
    if len(priced) < 2:
        return {}
    first = Decimal(priced[0].asking_price or 0)
    last = Decimal(priced[-1].asking_price or 0)
    reductions = sum(
        1 for a, b in itertools.pairwise(priced) if (b.asking_price or 0) < (a.asking_price or 0)
    )
    days = (priced[-1].observed_at - priced[0].observed_at).days
    facts = {
        "listing.snapshot_count": float(len(priced)),
        "listing.reduction_count": float(reductions),
        "listing.first_asking_price": float(first),
        "listing.latest_asking_price": float(last),
        "listing.days_observed": float(max(days, 0)),
    }
    if first > 0:
        facts["listing.total_reduction_pct"] = float((first - last) / first) * 100.0
    return facts


def _sanitise_name(value: str | None) -> str:
    """Names are interpolated into memo prose, so digits are stripped from them.

    A district or class name containing a numeral would otherwise be
    indistinguishable, to the numeral validator, from a fabricated figure.
    """
    if not value:
        return "unknown"
    return re.sub(r"\s+", " ", re.sub(r"\d", "", value)).strip() or "unknown"


def build_memo_facts(
    inputs: MemoInputs, *, target_margin: float = DEFAULT_TARGET_MARGIN
) -> MemoFacts:
    """Compute every number the memo may use. Nothing else is permitted."""
    score = inputs.score
    valuation = inputs.valuation
    cost = inputs.cost
    if score is None or valuation is None or cost is None:
        raise MemoError("memo facts require a score, a valuation and a complete cost")

    prop = inputs.opportunity.property
    ceiling = max_recommended_purchase_price(Decimal(valuation.fair_value_low), target_margin)
    cost_total = Decimal(cost.total)
    fair_base = Decimal(valuation.fair_value_base)

    numeric: dict[str, float] = {
        "policy.score_gate": SCORE_GATE,
        "policy.confidence_gate": CONFIDENCE_GATE,
        "policy.target_margin_pct": target_margin * 100.0,
        "policy.internal_verification_cap_pct": INTERNAL_WEIGHT_CAP * 100.0,
        "property.area_sqm": float(prop.built_area_sqm),
        "score.total": float(score.total_score),
        "score.data_confidence": float(score.data_confidence),
        "score.data_confidence_pct": float(score.data_confidence) * 100.0,
        "valuation.fair_value_low": float(valuation.fair_value_low),
        "valuation.fair_value_base": float(fair_base),
        "valuation.fair_value_high": float(valuation.fair_value_high),
        "valuation.base_price_per_sqm": float(valuation.base_price_per_sqm),
        "valuation.comparable_count": float(valuation.comparable_count),
        "valuation.effective_n": float(valuation.effective_n),
        "valuation.comparable_quality": float(valuation.comparable_quality),
        "valuation.confidence": float(valuation.confidence),
        "valuation.confidence_pct": float(valuation.confidence) * 100.0,
        "cost.total": float(cost_total),
        "cost.completeness_pct": float(cost.completeness) * 100.0,
        "derived.max_recommended_purchase_price": float(ceiling),
        # Signed magnitudes are stored absolute; direction lives in a text fact
        # so no rendered numeral ever carries an unresolvable minus sign.
        "derived.headroom_to_max_price": float(abs(ceiling - cost_total)),
        "derived.value_uplift_to_base": float(abs(fair_base - cost_total)),
    }
    if prop.bedrooms is not None:
        numeric["property.bedrooms"] = float(prop.bedrooms)
    if cost_total > 0:
        numeric["derived.value_uplift_pct"] = float((fair_base - cost_total) / cost_total) * 100.0
    if score.discount_fraction is not None:
        numeric["score.discount_fraction"] = float(score.discount_fraction)
        numeric["score.discount_pct"] = float(score.discount_fraction) * 100.0

    for component in score.components:
        prefix = f"score.components.{component.dimension}"
        numeric[f"{prefix}.normalized_score"] = float(component.normalized_score)
        numeric[f"{prefix}.weight"] = float(component.weight)
        numeric[f"{prefix}.contribution"] = float(component.normalized_score) * float(
            component.weight
        )

    for item in cost.line_items:
        if item.amount is not None:
            numeric[f"cost.line_items.{item.kind}"] = float(item.amount)

    rental = inputs.rental
    if rental is not None:
        numeric.update(
            {
                "rental.annual_rent": float(rental.annual_rent),
                "rental.annual_rent_low": float(rental.annual_rent_low),
                "rental.annual_rent_high": float(rental.annual_rent_high),
                "rental.rent_per_sqm_year": float(rental.rent_per_sqm_year),
                "rental.comparable_count": float(rental.comparable_count),
                "rental.effective_n": float(rental.effective_n),
                "rental.confidence": float(rental.confidence),
            }
        )
        if rental.gross_yield is not None:
            numeric["rental.gross_yield_pct"] = float(rental.gross_yield) * 100.0
        if rental.net_yield is not None:
            numeric["rental.net_yield_pct"] = float(rental.net_yield) * 100.0
        if rental.opex_total is not None:
            numeric["rental.opex_total"] = float(rental.opex_total)

    numeric.update(_verification_facts(inputs.checks))
    numeric.update(_listing_facts(inputs.snapshots))

    if inputs.comparables:
        adjusted = [float(c.adjusted_price_per_sqm) for c in inputs.comparables]
        distances = [float(c.distance_m) for c in inputs.comparables]
        numeric.update(
            {
                "comparables.included_count": float(len(inputs.comparables)),
                "comparables.top_weight_pct": float(inputs.comparables[0].weight) * 100.0,
                "comparables.min_adjusted_ppsqm": min(adjusted),
                "comparables.max_adjusted_ppsqm": max(adjusted),
                "comparables.nearest_distance_m": min(distances),
                "comparables.furthest_distance_m": max(distances),
            }
        )

    decision, basis = recommended_decision(
        classification=score.classification, cost_total=cost_total, ceiling=ceiling
    )
    district = inputs.opportunity.property.district
    text: dict[str, str] = {
        "opportunity.type": inputs.opportunity.opportunity_type,
        "property.class": prop.property_class,
        "property.district": _sanitise_name(district.name_en if district else None),
        "property.district_ar": _sanitise_name(district.name_ar if district else None),
        "score.classification": score.classification,
        "score.capped": "yes" if score.capped else "no",
        "cost.is_complete": "yes" if cost.is_complete else "no",
        "evidence.is_synthetic": "yes" if inputs.synthetic_evidence else "no",
        "verification.official_available": (
            "yes" if numeric["verification.official_applicable"] > 0 else "no"
        ),
        "derived.cost_vs_max_price": "below" if cost_total <= ceiling else "above",
        "rental.available": "yes" if inputs.rental is not None else "no",
        "rental.yield_available": (
            "yes" if inputs.rental is not None and inputs.rental.gross_yield is not None else "no"
        ),
        # Free text from the rental engine. Quoted into the memo only when it
        # carries no numeral of its own -- see `_quotable`.
        "rental.yield_refused_reason": (
            (inputs.rental.yield_refused_reason or "") if inputs.rental is not None else ""
        ),
        "policy.decision": decision,
        "policy.decision_basis": basis,
        "versions.scoring": score.method_version,
        "versions.valuation": valuation.method_version,
        "versions.cost": cost.method_version,
        "versions.weight_profile": score.weight_profile_version,
    }
    return MemoFacts(numeric=numeric, text=text)


# --------------------------------------------------------------------------
# Numeral resolution: the mechanism that makes "fails closed" true


_NUMERAL = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _renderings(value: float) -> set[str]:
    """Every string form in which a computed value may legitimately appear."""
    out: set[str] = set()
    for magnitude in {value, abs(value)}:
        for spec in ("{:,.1f}", "{:.1f}", "{:,.2f}", "{:.2f}", "{:.3f}", "{:.4f}"):
            out.add(spec.format(magnitude))
        if abs(magnitude) >= 1:
            out.add(f"{magnitude:,.0f}")
            out.add(f"{magnitude:.0f}")
        if float(magnitude).is_integer():
            out.add(str(int(magnitude)))
    return out


def numerals(text: str) -> list[str]:
    """Numerals in a string, with Eastern Arabic digits normalised first."""
    return _NUMERAL.findall(normalize_digits(text))


def _quotable(text: str) -> str | None:
    """Free text is safe to quote into a memo only if it states no number.

    Reasons written elsewhere in the platform are prose we do not control. A
    numeral inside one cannot be resolved to a computed field, so quoting it
    would (correctly) fail validation; we quote only the digit-free ones and
    fall back to a pointer otherwise.
    """
    cleaned = text.strip()
    if not cleaned or numerals(cleaned):
        return None
    return cleaned


def _allowed_numerals(facts: dict[str, float]) -> set[str]:
    allowed: set[str] = set()
    for value in facts.values():
        allowed |= _renderings(value)
    return allowed


# --------------------------------------------------------------------------
# The agent


class InvestmentMemoAgent(Agent[dict[str, Any], InvestmentMemo]):
    """Writes the memo. Cannot originate a figure, cannot change the decision."""

    name = "investment_memo"
    prompt_version = PROMPT_VERSION
    tier = ModelTier.LARGE
    output_model = InvestmentMemo
    call_budget_usd = Decimal("0.45")
    uses_tools = False  # reads attacker-controlled listing text

    def system_prompt(self) -> str:
        return (
            "You write an investment memo for a Saudi real-estate opportunity. "
            "Every figure you report has already been computed. You must cite, "
            "for each figure, the field reference it came from, and you must "
            "not state any number that is absent from the supplied fact table "
            "-- no yields, no rents, no exit prices, no bids of your own. The "
            "recommended decision and the maximum recommended purchase price "
            "are given to you; narrate them, never change them. Sections must "
            f"appear in exactly this order: {', '.join(SECTION_KEYS)}. "
            "Respond with JSON matching the schema."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return (
            f"Locale: {payload['locale']}\n"
            "Computed facts for one opportunity. These are the only numbers "
            "you may use:\n"
            f"```json\n{json.dumps(payload.get('memo_payload'), ensure_ascii=False)}\n```\n"
            "Write the memo."
        )

    def untrusted_content(self, payload: dict[str, Any]) -> list[str]:
        text = payload.get("listing_text")
        return [str(text)] if text else []

    def subject(self, payload: dict[str, Any]) -> tuple[str, UUID | None]:
        return ("opportunity", payload.get("opportunity_id"))

    def input_fingerprint(self, payload: dict[str, Any]) -> Any:
        return {
            "locale": payload["locale"],
            "memo_payload": payload["memo_payload"],
            "listing_text": payload.get("listing_text"),
            "attempt": payload.get("attempt", 0),
        }

    def validate_output(self, output: InvestmentMemo, payload: dict[str, Any]) -> InvestmentMemo:
        """Post-model validation. This is where the memo actually fails closed."""
        facts: dict[str, float] = payload["memo_payload"]["facts"]
        text_facts: dict[str, str] = payload["memo_payload"]["text_facts"]

        if output.locale != payload["locale"]:
            raise UnresolvableFigureError(
                f"memo locale {output.locale!r} does not match the requested "
                f"locale {payload['locale']!r}"
            )

        keys = tuple(section.key for section in output.sections)
        if keys != SECTION_KEYS:
            raise UnresolvableFigureError(
                f"memo sections {list(keys)} do not match the required sections "
                f"{list(SECTION_KEYS)}"
            )

        expected_decision = text_facts["policy.decision"]
        if output.decision != expected_decision:
            raise UnresolvableFigureError(
                f"memo decision {output.decision!r} contradicts the "
                f"deterministic recommendation {expected_decision!r}"
            )

        allowed = _allowed_numerals(facts)
        for section in output.sections:
            for figure in section.figures:
                if figure.field_ref not in facts:
                    raise UnresolvableFigureError(
                        f"section {section.key!r} figure {figure.label!r} cites "
                        f"{figure.field_ref!r}, which is not a computed field"
                    )
                expected = facts[figure.field_ref]
                tolerance = max(FIGURE_TOLERANCE, abs(expected) * 1e-6)
                if abs(figure.value - expected) > tolerance:
                    raise UnresolvableFigureError(
                        f"section {section.key!r} figure {figure.label!r} reports "
                        f"{figure.value} but {figure.field_ref} is {expected}"
                    )
            for chunk in (section.heading, *section.body, *(f.label for f in section.figures)):
                for token in numerals(chunk):
                    if token not in allowed:
                        raise UnresolvableFigureError(
                            f"section {section.key!r} states {token!r}, which does "
                            "not resolve to any computed field"
                        )

        cited = {
            (section.key, figure.field_ref)
            for section in output.sections
            for figure in section.figures
        }
        for key, refs in REQUIRED_CITATIONS.items():
            for ref in refs:
                if ref in facts and (key, ref) not in cited:
                    raise UnresolvableFigureError(f"section {key!r} must cite {ref!r} and does not")
        return output


# --------------------------------------------------------------------------
# The offline responder. Template text over the fact table -- not reasoning.

_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "opportunity": "Opportunity",
        "why_now": "Why now",
        "pricing": "Pricing",
        "comparable_evidence": "Comparable evidence",
        "expected_returns": "Expected returns",
        "risks": "Risks",
        "maximum_purchase_price": "Maximum recommended purchase price",
        "questions_before_purchase": "Questions before purchase",
        "decision": "Decision",
        "PROCEED_TO_DILIGENCE": "Proceed to diligence",
        "INVESTIGATE": "Investigate further",
        "PASS": "Pass",
        "APARTMENT": "Apartment",
        "VILLA": "Villa",
        "RESIDENTIAL_PLOT": "Residential plot",
        "AUCTION": "an auction lot",
        "ASSIGNMENT": "an assignment",
        "RESALE": "a resale",
        "OFF_PLAN_RESALE": "an off-plan resale",
        "DEVELOPER_INVENTORY": "developer inventory",
        "EXCEPTIONAL": "Exceptional",
        "STRONG": "Strong",
        "WORTH_REVIEWING": "Worth reviewing",
        "WATCHLIST": "Watchlist",
        "WEAK": "Weak",
        "INSUFFICIENT_DATA": "Insufficient data",
    },
    "ar": {
        "opportunity": "الفرصة",
        "why_now": "لماذا الآن",
        "pricing": "التسعير",
        "comparable_evidence": "أدلة المقارنات",
        "expected_returns": "العوائد المتوقعة",
        "risks": "المخاطر",
        "maximum_purchase_price": "أقصى سعر شراء موصى به",
        "questions_before_purchase": "أسئلة قبل الشراء",
        "decision": "القرار",
        "PROCEED_TO_DILIGENCE": "المضي إلى الفحص النافي للجهالة",
        "INVESTIGATE": "مزيد من الفحص",
        "PASS": "الامتناع",
        "APARTMENT": "شقة",
        "VILLA": "فيلا",
        "RESIDENTIAL_PLOT": "أرض سكنية",
        "AUCTION": "مزاد",
        "ASSIGNMENT": "تنازل",
        "RESALE": "إعادة بيع",
        "OFF_PLAN_RESALE": "إعادة بيع على الخارطة",
        "DEVELOPER_INVENTORY": "مخزون مطور",
        "EXCEPTIONAL": "استثنائية",
        "STRONG": "قوية",
        "WORTH_REVIEWING": "تستحق المراجعة",
        "WATCHLIST": "قائمة المتابعة",
        "WEAK": "ضعيفة",
        "INSUFFICIENT_DATA": "بيانات غير كافية",
    },
}

_QUESTIONS: dict[str, tuple[str, ...]] = {
    "en": (
        "Is the advertisement licensed by REGA, and does the licence match this unit? "
        "No official register is integrated, so the platform cannot answer this.",
        "What is the remaining balance owed to the developer, confirmed against the "
        "developer's own statement rather than the seller's description?",
        "Are there liens, unpaid service charges, or occupancy claims attached to the title?",
        "Does the title deed match the unit inspected, including area, floor and unit number?",
        "What rent is actually achieved in this building today, and on what evidence? "
        "Any rent quoted above is an estimate from comparable leases, not a "
        "contract on this unit.",
        "If this is an auction lot: what are the deposit, commission and settlement terms, "
        "and are all of them already inside the cost breakdown above?",
    ),
    "ar": (
        "هل الإعلان مرخّص من الهيئة العامة للعقار، وهل يطابق الترخيص هذه الوحدة؟ "
        "لا يوجد سجل رسمي مدمج، ولذلك لا تستطيع المنصة الإجابة.",
        "ما المبلغ المتبقي للمطور، مؤكداً من كشف المطور نفسه لا من وصف البائع؟",
        "هل توجد رهون أو رسوم خدمات غير مسددة أو مطالبات إشغال على الصك؟",
        "هل يطابق الصك الوحدة المعروضة، من حيث المساحة والدور ورقم الوحدة؟",
        "ما الإيجار المتحقق فعلياً في هذا المبنى اليوم، وبأي دليل؟ "
        "وأي إيجار مذكور أعلاه تقدير من عقود مقارنة لا عقد على هذه الوحدة.",
        "إذا كان الأصل معروضاً في مزاد: ما شروط العربون والعمولة والسداد، "
        "وهل جميعها مدرجة في تفصيل التكلفة أعلاه؟",
    ),
}


def _word(words: dict[str, str], key: str) -> str:
    """Vocabulary lookup that degrades to the raw key rather than raising."""
    return words.get(key, key.replace("_", " ").lower())


def _payload_from_prompt(user: str) -> dict[str, Any]:
    match = re.search(r"```json\n(.*?)\n```", user, re.S)
    if match is None:  # pragma: no cover - defensive
        raise MemoError("memo prompt did not carry a fact table")
    parsed: dict[str, Any] = json.loads(match.group(1))
    locale_match = re.search(r"^Locale:\s*(\w+)", user, re.M)
    parsed["_locale"] = locale_match.group(1) if locale_match else "en"
    return parsed


def deterministic_memo_responder(request: Any) -> str:
    """Offline stand-in for the memo model.

    Assembles the memo from the fact table with locale-specific templates. It
    is deliberately incapable of inventing a figure, which is the point: the
    validator above is what would catch a model that is not.
    """
    payload = _payload_from_prompt(request.user)
    locale = str(payload["_locale"])
    if locale not in LOCALES:
        locale = "en"
    facts: dict[str, float] = payload["facts"]
    text_facts: dict[str, str] = payload["text_facts"]
    w = _WORDS[locale]

    def money(ref: str) -> str:
        return f"{facts[ref]:,.0f}"

    def pct(ref: str) -> str:
        return f"{facts[ref]:.1f}"

    def two(ref: str) -> str:
        return f"{facts[ref]:.2f}"

    def count(ref: str) -> str:
        return str(int(facts[ref]))

    def fig(label: str, ref: str) -> MemoFigure:
        return MemoFigure(label=label, field_ref=ref, value=facts[ref])

    district = text_facts["property.district" if locale == "en" else "property.district_ar"]
    pclass = _word(w, text_facts["property.class"])
    otype = _word(w, text_facts["opportunity.type"])
    classification = _word(w, text_facts["score.classification"])
    sections: list[MemoSection] = []

    # 1. Opportunity ------------------------------------------------------
    if locale == "en":
        body = [
            f"{pclass} in {district}, {money('property.area_sqm')} square metres of "
            f"built area, offered as {otype}.",
            f"Opportunity score {pct('score.total')} at data confidence "
            f"{two('score.data_confidence')}, classified {classification}. Both clear the "
            f"memo gate of {count('policy.score_gate')} and "
            f"{two('policy.confidence_gate')}.",
            "Every figure below resolves to a field this platform computed. No number in "
            "this memo was originated by the agent that wrote it.",
        ]
    else:
        body = [
            f"{pclass} في {district}، بمساحة مبنية {money('property.area_sqm')} متر "
            f"مربع، معروضة عبر {otype}.",
            f"درجة الفرصة {pct('score.total')} وثقة البيانات "
            f"{two('score.data_confidence')}، والتصنيف {classification}. وكلاهما يتجاوز "
            f"عتبة المذكرة عند {count('policy.score_gate')} و"
            f"{two('policy.confidence_gate')}.",
            "كل رقم في هذه المذكرة يعود إلى حقل حسبته المنصة. لم ينشئ الوكيل أي رقم من عنده.",
        ]
    sections.append(
        MemoSection(
            key="opportunity",
            heading=w["opportunity"],
            body=body,
            figures=[
                fig("Opportunity score", "score.total"),
                fig("Data confidence", "score.data_confidence"),
                fig("Built area, square metres", "property.area_sqm"),
            ],
        )
    )

    # 2. Why now ----------------------------------------------------------
    why_figures: list[MemoFigure] = []
    if "listing.total_reduction_pct" in facts:
        why_figures = [
            fig("First observed asking price", "listing.first_asking_price"),
            fig("Latest observed asking price", "listing.latest_asking_price"),
            fig("Recorded reductions", "listing.reduction_count"),
            fig("Total reduction (%)", "listing.total_reduction_pct"),
        ]
        if locale == "en":
            why_body = [
                f"The observed asking price moved from {money('listing.first_asking_price')} "
                f"to {money('listing.latest_asking_price')} SAR across "
                f"{count('listing.reduction_count')} recorded reductions over "
                f"{count('listing.days_observed')} days of observation, a total reduction "
                f"of {pct('listing.total_reduction_pct')}%.",
                "That sequence is the timing signal. It is observed history from the "
                "listing record, not an inference about the seller's intent.",
            ]
        else:
            why_body = [
                f"انتقل السعر المطلوب المرصود من {money('listing.first_asking_price')} "
                f"إلى {money('listing.latest_asking_price')} ريال عبر "
                f"{count('listing.reduction_count')} تخفيضات مسجلة خلال "
                f"{count('listing.days_observed')} يوماً من المراقبة، بانخفاض إجمالي "
                f"{pct('listing.total_reduction_pct')}٪.",
                "هذا التسلسل هو إشارة التوقيت، وهو تاريخ مرصود من سجل الإعلان لا استنتاج "
                "عن نية البائع.",
            ]
    elif locale == "en":
        why_body = [
            "No listing price history is recorded for this property, so there is no "
            "observed timing signal from the advertisement itself.",
            "The case for acting rests on the pricing gap below, not on seller urgency. "
            "Any claim of urgency here would be unevidenced.",
        ]
    else:
        why_body = [
            "لا يوجد تاريخ أسعار مسجل لهذا العقار، ولذلك لا توجد إشارة توقيت مرصودة من "
            "الإعلان نفسه.",
            "المبرر يستند إلى فجوة التسعير أدناه لا إلى استعجال البائع. وأي ادعاء "
            "بالاستعجال هنا سيكون بلا دليل.",
        ]
    sections.append(
        MemoSection(key="why_now", heading=w["why_now"], body=why_body, figures=why_figures)
    )

    # 3. Pricing ----------------------------------------------------------
    pricing_figures = [
        fig("True acquisition cost", "cost.total"),
        fig("Fair value (base)", "valuation.fair_value_base"),
        fig("Fair value (low)", "valuation.fair_value_low"),
        fig("Fair value (high)", "valuation.fair_value_high"),
        fig("Price per square metre", "valuation.base_price_per_sqm"),
    ]
    if locale == "en":
        pricing_body = [
            f"True acquisition cost {money('cost.total')} SAR, with every material line "
            f"item known ({pct('cost.completeness_pct')}% complete).",
            f"Estimated market value {money('valuation.fair_value_base')} SAR, within a "
            f"range of {money('valuation.fair_value_low')} to "
            f"{money('valuation.fair_value_high')} SAR, at "
            f"{money('valuation.base_price_per_sqm')} SAR per square metre.",
        ]
    else:
        pricing_body = [
            f"التكلفة الفعلية للاستحواذ {money('cost.total')} ريال، وجميع البنود الجوهرية "
            f"معلومة (اكتمال {pct('cost.completeness_pct')}٪).",
            f"القيمة السوقية التقديرية {money('valuation.fair_value_base')} ريال، داخل "
            f"نطاق من {money('valuation.fair_value_low')} إلى "
            f"{money('valuation.fair_value_high')} ريال، وبسعر "
            f"{money('valuation.base_price_per_sqm')} ريال للمتر المربع.",
        ]
    if "score.discount_pct" in facts:
        pricing_figures.append(fig("Discount against fair value (%)", "score.discount_pct"))
        if locale == "en":
            pricing_body.append(
                f"That is a discount of {pct('score.discount_pct')}% against the base "
                "estimate. The discount is measured against the true acquisition cost, "
                "never against an advertised price."
            )
        else:
            pricing_body.append(
                f"أي خصم {pct('score.discount_pct')}٪ مقابل التقدير الأساسي. ويُقاس الخصم "
                "على التكلفة الفعلية للاستحواذ لا على السعر المعلن."
            )
    sections.append(
        MemoSection(key="pricing", heading=w["pricing"], body=pricing_body, figures=pricing_figures)
    )

    # 4. Comparable evidence ---------------------------------------------
    ev_figures = [
        fig("Comparables used", "valuation.comparable_count"),
        fig("Effective sample size", "valuation.effective_n"),
        fig("Comparable quality", "valuation.comparable_quality"),
        fig("Valuation confidence", "valuation.confidence"),
    ]
    if locale == "en":
        ev_body = [
            f"The valuation rests on {count('valuation.comparable_count')} weighted "
            f"comparable transactions, effective sample size "
            f"{two('valuation.effective_n')}, comparable quality "
            f"{two('valuation.comparable_quality')}, valuation confidence "
            f"{two('valuation.confidence')}."
        ]
    else:
        ev_body = [
            f"يستند التقييم إلى {count('valuation.comparable_count')} صفقة مقارنة مرجّحة، "
            f"وحجم عينة فعّال {two('valuation.effective_n')}، وجودة مقارنات "
            f"{two('valuation.comparable_quality')}، وثقة تقييم "
            f"{two('valuation.confidence')}."
        ]
    if "comparables.min_adjusted_ppsqm" in facts:
        ev_figures += [
            fig("Lowest adjusted price per square metre", "comparables.min_adjusted_ppsqm"),
            fig("Highest adjusted price per square metre", "comparables.max_adjusted_ppsqm"),
            fig("Nearest comparable (m)", "comparables.nearest_distance_m"),
        ]
        if locale == "en":
            ev_body.append(
                f"Adjusted prices per square metre across those comparables run from "
                f"{money('comparables.min_adjusted_ppsqm')} to "
                f"{money('comparables.max_adjusted_ppsqm')} SAR. The nearest sits "
                f"{money('comparables.nearest_distance_m')} m away, the furthest "
                f"{money('comparables.furthest_distance_m')} m."
            )
        else:
            ev_body.append(
                f"تتراوح الأسعار المعدّلة للمتر بين "
                f"{money('comparables.min_adjusted_ppsqm')} و"
                f"{money('comparables.max_adjusted_ppsqm')} ريال. وأقرب مقارنة تبعد "
                f"{money('comparables.nearest_distance_m')} متر وأبعدها "
                f"{money('comparables.furthest_distance_m')} متر."
            )
    if text_facts["evidence.is_synthetic"] == "yes":
        ev_body.append(
            "The comparable transactions come from a synthetic fixture corpus, not from "
            "registered sales. The valuation machinery is real; the evidence behind it is "
            "generated, and no purchase decision should rest on it."
            if locale == "en"
            else "الصفقات المقارنة مأخوذة من مجموعة اصطناعية لا من صفقات مسجلة. آلية "
            "التقييم حقيقية، أما الأدلة فمولّدة، ولا يصح أن يُبنى قرار شراء عليها."
        )
    sections.append(
        MemoSection(
            key="comparable_evidence",
            heading=w["comparable_evidence"],
            body=ev_body,
            figures=ev_figures,
        )
    )

    # 5. Expected returns -------------------------------------------------
    ret_figures = [
        fig("Value uplift to base estimate", "derived.value_uplift_to_base"),
        fig("Rental dimension score", "score.components.RENTAL.normalized_score"),
        fig("Rental dimension weight", "score.components.RENTAL.weight"),
        fig("Rental dimension contribution", "score.components.RENTAL.contribution"),
    ]
    cost_side = "below" if facts["cost.total"] <= facts["valuation.fair_value_base"] else "above"
    cost_side_ar = "دون" if cost_side == "below" else "فوق"
    if locale == "en":
        ret_body = [
            f"Buying at {money('cost.total')} SAR against a base estimate of "
            f"{money('valuation.fair_value_base')} SAR is a gap of "
            f"{money('derived.value_uplift_to_base')} SAR, with the cost "
            f"{cost_side} the estimate. That gap is a valuation difference, not a "
            "realised gain: it is only recoverable on a sale at the estimate."
        ]
    else:
        ret_body = [
            f"الشراء بـ {money('cost.total')} ريال مقابل تقدير أساسي "
            f"{money('valuation.fair_value_base')} ريال يعني فرقاً قدره "
            f"{money('derived.value_uplift_to_base')} ريال، فالتكلفة "
            f"{cost_side_ar} التقدير. وهذا الفرق فرق تقييم لا ربح محقق، ولا يتحقق إلا "
            "ببيع عند التقدير."
        ]

    # The income half of the return. Three genuinely different states, and the
    # memo must not describe one as another.
    if "rental.gross_yield_pct" in facts:
        ret_figures += [
            fig("Estimated annual rent", "rental.annual_rent"),
            fig("Gross yield (%)", "rental.gross_yield_pct"),
            fig("Rental estimate confidence", "rental.confidence"),
            fig("Lease comparables used", "rental.comparable_count"),
        ]
        net_en = ""
        net_ar = ""
        if "rental.net_yield_pct" in facts:
            ret_figures.append(fig("Net yield (%)", "rental.net_yield_pct"))
            net_en = f" and a net yield of {pct('rental.net_yield_pct')}%"
            net_ar = f" وعائد صافي {pct('rental.net_yield_pct')}٪"
        if locale == "en":
            ret_body.append(
                f"Estimated annual rent {money('rental.annual_rent')} SAR, within a range "
                f"of {money('rental.annual_rent_low')} to "
                f"{money('rental.annual_rent_high')} SAR, giving a gross yield of "
                f"{pct('rental.gross_yield_pct')}%{net_en} on the true acquisition cost."
            )
            ret_body.append(
                f"That rent is an estimate from {count('rental.comparable_count')} lease "
                f"comparables at confidence {two('rental.confidence')}. It is not a "
                "contracted rent on this unit, and it is not a guarantee of occupancy."
            )
        else:
            ret_body.append(
                f"الإيجار السنوي المقدّر {money('rental.annual_rent')} ريال، داخل نطاق من "
                f"{money('rental.annual_rent_low')} إلى "
                f"{money('rental.annual_rent_high')} ريال، بعائد إجمالي "
                f"{pct('rental.gross_yield_pct')}٪{net_ar} على التكلفة الفعلية للاستحواذ."
            )
            ret_body.append(
                f"وهذا الإيجار تقدير من {count('rental.comparable_count')} عقود إيجار "
                f"مقارنة بثقة {two('rental.confidence')}. وهو ليس إيجاراً متعاقداً على "
                "هذه الوحدة ولا ضماناً للإشغال."
            )
    elif text_facts["rental.available"] == "yes":
        reason = _quotable(text_facts.get("rental.yield_refused_reason", ""))
        if locale == "en":
            sentence = (
                "A rental estimate exists for this property but the yield was refused, so "
                "no income return is stated here."
            )
            ret_body.append(f"{sentence} Reason: {reason}." if reason else sentence)
        else:
            sentence = (
                "يوجد تقدير إيجاري لهذا العقار لكن العائد مرفوض، ولذلك لا يُذكر أي عائد دخل هنا."
            )
            ret_body.append(f"{sentence} السبب: {reason}." if reason else sentence)
    elif locale == "en":
        ret_body.append(
            "No rental estimate has been computed for this property, so the rental "
            f"dimension contributes {two('score.components.RENTAL.contribution')} of its "
            f"{two('score.components.RENTAL.weight')} weight. Any income return quoted "
            "for this property, by anyone, would be unevidenced."
        )
    else:
        ret_body.append(
            f"لم يُحسب أي تقدير إيجاري لهذا العقار، ولذلك يسهم بُعد الإيجار بـ "
            f"{two('score.components.RENTAL.contribution')} من وزنه "
            f"{two('score.components.RENTAL.weight')}. وأي عائد إيجاري يُنسب إلى هذا "
            "العقار من أي جهة هو بلا دليل."
        )

    ret_body.append(
        "There is no modelled exit price, holding period or IRR here, because the "
        "platform computes none of those."
        if locale == "en"
        else "لا يوجد سعر خروج مقدّر ولا مدة احتفاظ ولا معدل عائد داخلي، لأن المنصة لا "
        "تحسب أياً منها."
    )
    sections.append(
        MemoSection(
            key="expected_returns",
            heading=w["expected_returns"],
            body=ret_body,
            figures=ret_figures,
        )
    )

    # 6. Risks -------------------------------------------------------------
    risk_figures = [
        fig("Risk dimension score", "score.components.RISK.normalized_score"),
        fig("Verification score (%)", "verification.score_pct"),
        fig("Internal coherence (%)", "verification.internal_pct"),
        fig("Official confirmation (%)", "verification.official_pct"),
    ]
    if locale == "en":
        risk_body = [
            f"The risk dimension scores {pct('score.components.RISK.normalized_score')}, "
            f"contributing {two('score.components.RISK.contribution')} at a weight of "
            f"{two('score.components.RISK.weight')}.",
            f"Verification stands at {pct('verification.score_pct')}% "
            f"(internal coherence {pct('verification.internal_pct')}%, official "
            f"confirmation {pct('verification.official_pct')}%), with "
            f"{count('verification.checks_verified')} of "
            f"{count('verification.checks_applicable')} applicable checks passing.",
        ]
        if text_facts["verification.official_available"] == "no":
            risk_body.append(
                "No official register is integrated, so verification cannot exceed "
                f"{pct('policy.internal_verification_cap_pct')}% of its full weight "
                "however many internal checks pass. Licensing, title and developer "
                "standing are therefore unconfirmed."
            )
        if text_facts["score.capped"] == "yes":
            risk_body.append(
                "The classification is capped by the confidence gate: the raw score would "
                "place it higher, and the evidence does not support that."
            )
    else:
        risk_body = [
            f"يسجل بُعد المخاطر {pct('score.components.RISK.normalized_score')}، ويسهم "
            f"بـ {two('score.components.RISK.contribution')} بوزن "
            f"{two('score.components.RISK.weight')}.",
            f"تبلغ درجة التحقق {pct('verification.score_pct')}٪ (اتساق داخلي "
            f"{pct('verification.internal_pct')}٪، وتأكيد رسمي "
            f"{pct('verification.official_pct')}٪)، بنجاح "
            f"{count('verification.checks_verified')} من "
            f"{count('verification.checks_applicable')} فحوص قابلة للتطبيق.",
        ]
        if text_facts["verification.official_available"] == "no":
            risk_body.append(
                "لا يوجد سجل رسمي مدمج، ولذلك لا تتجاوز درجة التحقق "
                f"{pct('policy.internal_verification_cap_pct')}٪ من وزنها الكامل مهما "
                "نجح من الفحوص الداخلية. والترخيص والصك وسجل المطور غير مؤكدة."
            )
        if text_facts["score.capped"] == "yes":
            risk_body.append(
                "التصنيف محدود بعتبة الثقة: الدرجة الخام تضعه أعلى، والأدلة لا تدعم ذلك."
            )
    risk_figures.append(fig("Risk dimension contribution", "score.components.RISK.contribution"))
    risk_figures.append(fig("Risk dimension weight", "score.components.RISK.weight"))
    sections.append(
        MemoSection(key="risks", heading=w["risks"], body=risk_body, figures=risk_figures)
    )

    # 7. Maximum recommended purchase price -------------------------------
    ceiling_figures = [
        fig("Maximum recommended purchase price", "derived.max_recommended_purchase_price"),
        fig("Fair value (low)", "valuation.fair_value_low"),
        fig("Target margin (%)", "policy.target_margin_pct"),
        fig("Headroom against cost", "derived.headroom_to_max_price"),
    ]
    side = text_facts["derived.cost_vs_max_price"]
    if locale == "en":
        ceiling_body = [
            f"Maximum recommended purchase price: "
            f"{money('derived.max_recommended_purchase_price')} SAR.",
            f"That is the cautious end of the valuation range, "
            f"{money('valuation.fair_value_low')} SAR, less a target margin of "
            f"{pct('policy.target_margin_pct')}%. It is arithmetic on computed fields; "
            "the agent narrates it and cannot move it.",
            f"The true acquisition cost of {money('cost.total')} SAR sits "
            f"{money('derived.headroom_to_max_price')} SAR {side} that ceiling.",
        ]
    else:
        side_ar = "دون" if side == "below" else "فوق"
        ceiling_body = [
            f"أقصى سعر شراء موصى به: {money('derived.max_recommended_purchase_price')} ريال.",
            f"وهو الطرف المتحفظ من نطاق التقييم، {money('valuation.fair_value_low')} "
            f"ريال، مخفضاً بهامش مستهدف {pct('policy.target_margin_pct')}٪. وهذه عملية "
            "حسابية على حقول محسوبة، يسردها الوكيل ولا يستطيع تغييرها.",
            f"وتقع التكلفة الفعلية للاستحواذ {money('cost.total')} ريال بمقدار "
            f"{money('derived.headroom_to_max_price')} ريال {side_ar} هذا الحد.",
        ]
    sections.append(
        MemoSection(
            key="maximum_purchase_price",
            heading=w["maximum_purchase_price"],
            body=ceiling_body,
            figures=ceiling_figures,
        )
    )

    # 8. Questions ---------------------------------------------------------
    sections.append(
        MemoSection(
            key="questions_before_purchase",
            heading=w["questions_before_purchase"],
            body=list(_QUESTIONS[locale]),
            figures=[],
        )
    )

    # 9. Decision ----------------------------------------------------------
    decision = text_facts["policy.decision"]
    basis_key = text_facts["policy.decision_basis"]
    if basis_key == "COST_ABOVE_CEILING":
        basis = (
            "the true acquisition cost is above the maximum recommended purchase price"
            if locale == "en"
            else "التكلفة الفعلية للاستحواذ تتجاوز أقصى سعر شراء موصى به"
        )
    else:
        basis = (
            f"the classification is {classification}"
            if locale == "en"
            else f"التصنيف {classification}"
        )
    if locale == "en":
        decision_body = [
            f"Recommendation: {_word(w, decision)}. Basis: {basis}.",
            "The recommendation is derived by rule from the classification and the "
            "purchase ceiling, not by the agent. The agent cannot upgrade it.",
            "This memo is decision support over synthetic comparable evidence with no "
            "official verification. It is not investment advice and not a valuation "
            "certificate.",
        ]
    else:
        decision_body = [
            f"التوصية: {_word(w, decision)}. الأساس: {basis}.",
            "التوصية مشتقة بقاعدة من التصنيف وحد الشراء، لا من الوكيل، ولا يستطيع الوكيل ترقيتها.",
            "هذه المذكرة دعم قرار مبني على أدلة مقارنة اصطناعية وبدون تحقق رسمي. وهي "
            "ليست نصيحة استثمارية ولا شهادة تقييم.",
        ]
    sections.append(
        MemoSection(key="decision", heading=w["decision"], body=decision_body, figures=[])
    )

    # Validated from a mapping rather than constructed positionally: the
    # decision comes out of the fact table as a string, and it should be
    # checked against the schema here exactly as a model response would be.
    return InvestmentMemo.model_validate(
        {"locale": locale, "decision": decision, "sections": sections}
    ).model_dump_json()


# --------------------------------------------------------------------------
# Orchestration


def _persist(
    session: Session,
    *,
    inputs: MemoInputs,
    locale: str,
    status: str,
    reason: str | None,
    gate: MemoGate,
    facts: MemoFacts | None,
    memo: InvestmentMemo | None,
    ceiling: Decimal | None,
    target_margin: float,
    agent_run_id: UUID | None,
    provider: str | None,
    attempts: int,
    injection_flagged: bool,
) -> InvestmentMemoRow:
    score = inputs.score
    row = InvestmentMemoRow(
        opportunity_id=inputs.opportunity.id,
        locale=locale,
        status=status,
        reason=reason,
        score_id=score.id if score else None,
        valuation_id=inputs.valuation.id if inputs.valuation else None,
        cost_id=inputs.cost.id if inputs.cost else None,
        score_total=gate.score_total,
        data_confidence=gate.data_confidence,
        scoring_method_version=score.method_version if score else None,
        valuation_method_version=(inputs.valuation.method_version if inputs.valuation else None),
        cost_method_version=inputs.cost.method_version if inputs.cost else None,
        memo_method_version=MEMO_METHOD_VERSION,
        prompt_version=PROMPT_VERSION,
        weight_profile_version=score.weight_profile_version if score else None,
        target_margin=target_margin if status == "GENERATED" else None,
        max_recommended_purchase_price=ceiling if status == "GENERATED" else None,
        decision=memo.decision if memo else None,
        sections=([json.loads(s.model_dump_json()) for s in memo.sections] if memo else None),
        figures=(
            [
                {**json.loads(f.model_dump_json()), "section": s.key}
                for s in memo.sections
                for f in s.figures
            ]
            if memo
            else None
        ),
        facts=({**facts.payload(), "injection_flagged": injection_flagged} if facts else None),
        agent_run_id=agent_run_id,
        provider=provider,
        attempts=attempts,
        generated_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def _record_run_error(
    session: Session, agent: InvestmentMemoAgent, payload: dict[str, Any], message: str
) -> None:
    """Attach the rejection reason to the run the runtime already opened.

    The runtime records `error` for provider and schema failures but not for a
    `validate_output` rejection, because that exception propagates before it
    reaches the error field. Filling it in here keeps `agent_runs` honest.
    """
    input_hash = stable_hash(agent.input_fingerprint(payload))
    run = session.scalar(
        select(AgentRun)
        .where(
            AgentRun.agent == agent.name,
            AgentRun.input_hash == input_hash,
            AgentRun.status == "FAILED",
            AgentRun.error.is_(None),
        )
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    if run is not None:
        run.error = message[:2000]
        session.flush()


def generate_memo(
    session: Session,
    *,
    opportunity: Opportunity,
    context: AgentContext,
    locale: str = "en",
    target_margin: float = DEFAULT_TARGET_MARGIN,
    max_attempts: int = 2,
) -> MemoRecord:
    """Produce a memo, or record precisely why there is not one.

    Returns a record for a gate refusal (a normal, expected outcome). Raises
    `MemoRejectedError` when the agent could not produce a memo whose every
    figure resolves -- an abnormal outcome that must not be silently swallowed.
    """
    if locale not in LOCALES:
        raise ValueError(f"unsupported memo locale {locale!r}")

    inputs = load_memo_inputs(session, opportunity)
    gate = memo_gate(inputs)
    if not gate.allowed:
        row = _persist(
            session,
            inputs=inputs,
            locale=locale,
            status="NOT_GENERATED",
            reason=gate.reason,
            gate=gate,
            facts=None,
            memo=None,
            ceiling=None,
            target_margin=target_margin,
            agent_run_id=None,
            provider=None,
            attempts=0,
            injection_flagged=False,
        )
        return MemoRecord(
            row_id=row.id,
            opportunity_id=opportunity.id,
            locale=locale,
            status="NOT_GENERATED",
            reason=gate.reason,
            memo=None,
            decision=None,
            max_recommended_purchase_price=None,
            facts=None,
            attempts=0,
        )

    facts = build_memo_facts(inputs, target_margin=target_margin)
    assert inputs.valuation is not None  # guaranteed by the gate
    ceiling = max_recommended_purchase_price(
        Decimal(inputs.valuation.fair_value_low), target_margin
    )

    agent = InvestmentMemoAgent()
    runtime = AgentRuntime(context)
    last_error = "memo generation did not run"
    attempts = 0
    injection_flagged = False

    for attempt in range(max(1, max_attempts)):
        attempts = attempt + 1
        payload: dict[str, Any] = {
            "opportunity_id": opportunity.id,
            "locale": locale,
            "memo_payload": facts.payload(),
            "listing_text": inputs.listing_text,
            "attempt": attempt,
        }
        try:
            result = runtime.run(agent, payload)
        except (AgentError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _record_run_error(session, agent, payload, last_error)
            continue

        if result.injection_flagged:
            # Failure behaviour for the memo stage is discard, not repair: the
            # scan is deterministic on the same text, so retrying is theatre.
            injection_flagged = True
            last_error = (
                "listing text carries a prompt-injection payload "
                f"({result.injection.summary}); memo discarded"
            )
            _record_run_error(session, agent, payload, last_error)
            break

        row = _persist(
            session,
            inputs=inputs,
            locale=locale,
            status="GENERATED",
            reason=None,
            gate=gate,
            facts=facts,
            memo=result.output,
            ceiling=ceiling,
            target_margin=target_margin,
            agent_run_id=result.run_id,
            provider=result.provider,
            attempts=attempts,
            injection_flagged=False,
        )
        return MemoRecord(
            row_id=row.id,
            opportunity_id=opportunity.id,
            locale=locale,
            status="GENERATED",
            reason=None,
            memo=result.output,
            decision=result.output.decision,
            max_recommended_purchase_price=ceiling,
            facts=facts,
            attempts=attempts,
        )

    _persist(
        session,
        inputs=inputs,
        locale=locale,
        status="REJECTED",
        reason=last_error,
        gate=gate,
        facts=facts,
        memo=None,
        ceiling=None,
        target_margin=target_margin,
        agent_run_id=None,
        provider=context.provider.name,
        attempts=attempts,
        injection_flagged=injection_flagged,
    )
    raise MemoRejectedError(
        f"memo abandoned after {attempts} attempt(s) for opportunity {opportunity.id}: {last_error}"
    )


def latest_memo(
    session: Session, opportunity_id: UUID, locale: str = "en"
) -> InvestmentMemoRow | None:
    """The most recent memo row for this opportunity and locale, of any status."""
    return session.scalar(
        select(InvestmentMemoRow)
        .where(
            InvestmentMemoRow.opportunity_id == opportunity_id,
            InvestmentMemoRow.locale == locale,
        )
        .order_by(InvestmentMemoRow.generated_at.desc())
        .limit(1)
    )
