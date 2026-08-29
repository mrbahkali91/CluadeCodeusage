"""Opportunity scoring: deterministic, versioned, reproducible (ADR-010).

Same inputs + same method version + same weight profile => bit-identical score.
An LLM never participates. A score that cannot be reproduced and decomposed is
not usable for a decision involving this much money.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

METHOD_VERSION = "scoring-v1"
DEFAULT_WEIGHT_PROFILE_VERSION = "default-v1"

CONFIDENCE_FLOOR = 0.60  # below this: INSUFFICIENT DATA, no recommendation
CONFIDENCE_CAP_LEVEL = 0.75  # below this: classification capped at WORTH_REVIEWING


class Dimension(StrEnum):
    DISCOUNT = "DISCOUNT"
    LIQUIDITY = "LIQUIDITY"
    RENTAL = "RENTAL"
    LOCATION = "LOCATION"
    DEVELOPER = "DEVELOPER"
    RISK = "RISK"
    CONFIDENCE = "CONFIDENCE"


class Classification(StrEnum):
    EXCEPTIONAL = "EXCEPTIONAL"
    STRONG = "STRONG"
    WORTH_REVIEWING = "WORTH_REVIEWING"
    WATCHLIST = "WATCHLIST"
    WEAK = "WEAK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    @property
    def label(self) -> str:
        return {
            "EXCEPTIONAL": "Exceptional",
            "STRONG": "Strong",
            "WORTH_REVIEWING": "Worth reviewing",
            "WATCHLIST": "Watchlist",
            "WEAK": "Weak opportunity",
            "INSUFFICIENT_DATA": "Insufficient data",
        }[self.value]


@dataclass(frozen=True, slots=True)
class WeightProfile:
    """Weights are an investment thesis, so they are per-organisation and versioned."""

    version: str
    weights: dict[Dimension, float]

    def __post_init__(self) -> None:
        missing = set(Dimension) - set(self.weights)
        if missing:
            names = sorted(d.value for d in missing)
            raise ValueError(f"weight profile missing dimensions: {names}")
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")


DEFAULT_WEIGHTS = WeightProfile(
    version=DEFAULT_WEIGHT_PROFILE_VERSION,
    weights={
        Dimension.DISCOUNT: 0.30,
        Dimension.LIQUIDITY: 0.15,
        Dimension.RENTAL: 0.15,
        Dimension.LOCATION: 0.10,
        Dimension.DEVELOPER: 0.10,
        Dimension.RISK: 0.10,
        Dimension.CONFIDENCE: 0.10,
    },
)


def _piecewise(x: float, knots: Sequence[tuple[float, float]]) -> float:
    """Monotonic piecewise-linear map. Explicit knots, reviewable by a human."""
    if x <= knots[0][0]:
        return knots[0][1]
    if x >= knots[-1][0]:
        return knots[-1][1]
    for i in range(1, len(knots)):
        x0, y0 = knots[i - 1]
        x1, y1 = knots[i]
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return knots[-1][1]


DISCOUNT_KNOTS = (
    (0.0, 0.0),
    (0.05, 25.0),
    (0.10, 50.0),
    (0.15, 68.0),
    (0.20, 80.0),
    (0.25, 88.0),
    (0.35, 100.0),
)
YIELD_KNOTS = ((0.0, 0.0), (0.04, 30.0), (0.06, 55.0), (0.07, 70.0), (0.08, 82.0), (0.10, 100.0))


def discount_score(discount_fraction: float) -> float:
    return _piecewise(discount_fraction, DISCOUNT_KNOTS)


def rental_score(gross_yield: float) -> float:
    return _piecewise(gross_yield, YIELD_KNOTS)


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    dimension: Dimension
    raw_value: float | None
    normalized_score: float
    weight: float
    inputs: dict[str, float | str | None]

    @property
    def contribution(self) -> float:
        return self.weight * self.normalized_score


@dataclass(frozen=True, slots=True)
class OpportunityScore:
    total: float
    classification: Classification
    data_confidence: float
    components: tuple[ScoreComponent, ...]
    weight_profile_version: str
    method_version: str = METHOD_VERSION
    capped: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.classification is not Classification.INSUFFICIENT_DATA


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    valuation_confidence: float
    cost_completeness: float
    verification_score: float
    source_confidence: float
    field_completeness: float

    @property
    def data_confidence(self) -> float:
        return max(
            0.0,
            min(
                1.0,
                0.30 * self.valuation_confidence
                + 0.20 * self.cost_completeness
                + 0.20 * self.verification_score
                + 0.15 * self.source_confidence
                + 0.15 * self.field_completeness,
            ),
        )


def classify(total: float, data_confidence: float) -> tuple[Classification, bool]:
    """Classification honours the confidence gate.

    This is the specific mechanism that prevents a thinly-evidenced property
    scoring 91 on an optimistic discount from being presented as "Exceptional".
    """
    if data_confidence < CONFIDENCE_FLOOR:
        return Classification.INSUFFICIENT_DATA, False

    if total >= 90:
        raw = Classification.EXCEPTIONAL
    elif total >= 80:
        raw = Classification.STRONG
    elif total >= 70:
        raw = Classification.WORTH_REVIEWING
    elif total >= 60:
        raw = Classification.WATCHLIST
    else:
        raw = Classification.WEAK

    if data_confidence < CONFIDENCE_CAP_LEVEL and raw in {
        Classification.EXCEPTIONAL,
        Classification.STRONG,
    }:
        return Classification.WORTH_REVIEWING, True
    return raw, False


def score_opportunity(
    *,
    discount_fraction: float | None,
    gross_yield: float | None,
    liquidity: float,
    location: float,
    developer: float,
    risk_score: float,
    confidence: ConfidenceInputs,
    profile: WeightProfile = DEFAULT_WEIGHTS,
) -> OpportunityScore:
    """Compute the opportunity score. Pure function -- no I/O, no randomness."""
    data_confidence = confidence.data_confidence

    # A refused discount scores zero on that dimension rather than being dropped:
    # dropping it would silently re-weight every other dimension upward.
    d_score = 0.0 if discount_fraction is None else discount_score(discount_fraction)
    r_score = 0.0 if gross_yield is None else rental_score(gross_yield)

    raw: dict[Dimension, tuple[float | None, float, dict[str, float | str | None]]] = {
        Dimension.DISCOUNT: (
            discount_fraction,
            d_score,
            {"discount_fraction": discount_fraction, "knots": "DISCOUNT_KNOTS"},
        ),
        Dimension.LIQUIDITY: (liquidity, liquidity, {"liquidity_score": liquidity}),
        Dimension.RENTAL: (gross_yield, r_score, {"gross_yield": gross_yield}),
        Dimension.LOCATION: (location, location, {"location_score": location}),
        Dimension.DEVELOPER: (developer, developer, {"developer_score": developer}),
        Dimension.RISK: (risk_score, risk_score, {"risk_score": risk_score}),
        Dimension.CONFIDENCE: (
            data_confidence,
            data_confidence * 100.0,
            {
                "valuation_confidence": confidence.valuation_confidence,
                "cost_completeness": confidence.cost_completeness,
                "verification_score": confidence.verification_score,
                "source_confidence": confidence.source_confidence,
                "field_completeness": confidence.field_completeness,
            },
        ),
    }

    components = tuple(
        ScoreComponent(
            dimension=dim,
            raw_value=raw[dim][0],
            normalized_score=max(0.0, min(100.0, raw[dim][1])),
            weight=profile.weights[dim],
            inputs=raw[dim][2],
        )
        for dim in Dimension
    )

    total = round(sum(c.contribution for c in components), 4)
    classification, capped = classify(total, data_confidence)

    return OpportunityScore(
        total=total,
        classification=classification,
        data_confidence=data_confidence,
        components=components,
        weight_profile_version=profile.version,
        capped=capped,
    )
