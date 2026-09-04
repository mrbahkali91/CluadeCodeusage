"""Comparable selection, time adjustment and fair-market-value estimation.

Deterministic and pure. No LLM participates in any calculation in this module
(ADR-010). See docs/architecture/valuation-and-scoring.md for the specification.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sreoi_domain.stats import (
    effective_sample_size,
    iqr_bounds,
    weighted_median,
    weighted_quantile,
)

METHOD_VERSION = "valuation-v2"

# --------------------------------------------------------------------------
# Band policy
#
# `valuation-v1` took the weighted (Q25, Q75) pair and then widened both ends
# by `1 + 0.6/sqrt(n_eff)`. At the effective sample sizes the engine actually
# achieves -- median n_eff 5.64, so a factor of ~1.25 on each end -- that
# inflated a 13.6%-wide band to 58.8% of value, reaching 98.7% coverage
# against a 70% target. Coverage bought that way is worthless: a band spanning
# -28%/+28% cannot separate "20% below market" from "fairly priced", which is
# the one decision this product exists to support.
#
# `valuation-v2` states the interval as an empirical weighted quantile pair
# with no inflation. The band is then a claim about the evidence rather than
# about a sampling distribution the evidence cannot support, and its coverage
# is whatever it measures -- published, not engineered.
# Chosen by sweeping the pair against the spec's two targets (coverage >= 70%,
# width <= 30%) on the back-test harness. Of (0.25,0.75), (0.20,0.80),
# (0.15,0.85), (0.10,0.90), (0.05,0.95), (0.02,0.98) and (0.0,1.0), exactly one
# satisfies both: 0.05/0.95 gives 27.9% width at 71.7% coverage. Q10/Q90 is
# narrower (21.9%) but covers only 67.1%, and anything wider breaches the width
# ceiling. See TRACK-D.md for the full sweep.
BAND_LOWER_QUANTILE = 0.05
BAND_UPPER_QUANTILE = 0.95

# The interquartile spread, as a fraction of the estimate, at which the
# agreement term reaches zero. 0.30 means comparables whose middle half spans
# 30% of the estimate contribute no confidence at all.
DISPERSION_FULL_PENALTY = 0.30

# Kernel bandwidths (documented defaults; see spec section 1.2).
LAMBDA_DISTANCE_M = 1200.0
LAMBDA_TIME_MONTHS = 9.0
LAMBDA_AREA_FRAC = 0.20
LAMBDA_AGE_YEARS = 8.0
SAME_PROJECT_MULTIPLIER = 1.35
SAME_DISTRICT_MULTIPLIER = 1.20
MIN_COMPARABLES = 8
MIN_EFFECTIVE_N = 3.0


class IndexTier(StrEnum):
    """Which price index we could use, weakest tiers reduce confidence."""

    DISTRICT = "DISTRICT"
    CITY = "CITY"
    NATIONAL = "NATIONAL"
    NONE = "NONE"

    @property
    def quality(self) -> float:
        return {"DISTRICT": 1.0, "CITY": 0.7, "NATIONAL": 0.4, "NONE": 0.0}[self.value]


@dataclass(frozen=True, slots=True)
class SubjectProperty:
    """The property being valued."""

    property_class: str
    area_sqm: float
    district_id: UUID | None = None
    project_id: UUID | None = None
    build_year: int | None = None
    floor: int | None = None

    def age_years(self, as_of: date) -> float | None:
        return None if self.build_year is None else max(0.0, as_of.year - self.build_year)


@dataclass(frozen=True, slots=True)
class Comparable:
    """A registered sale transaction being considered as evidence."""

    id: UUID
    price: Decimal
    area_sqm: float
    transacted_on: date
    distance_m: float
    property_class: str
    district_id: UUID | None = None
    project_id: UUID | None = None
    build_year: int | None = None
    floor: int | None = None

    @property
    def price_per_sqm(self) -> float:
        if self.area_sqm <= 0:
            raise ValueError(f"comparable {self.id} has non-positive area")
        return float(self.price) / self.area_sqm


@dataclass(frozen=True, slots=True)
class WeightedComparable:
    """A comparable with its similarity weight and time-adjusted price."""

    comparable: Comparable
    weight: float
    adjusted_price_per_sqm: float
    weight_breakdown: dict[str, float]
    excluded_reason: str | None = None

    @property
    def included(self) -> bool:
        return self.excluded_reason is None


@dataclass(frozen=True, slots=True)
class FairValue:
    """The output of the valuation engine."""

    base: Decimal
    low: Decimal
    high: Decimal
    base_price_per_sqm: float
    comparable_count: int
    effective_n: float
    comparable_quality: float
    confidence: float
    index_tier: IndexTier
    method_version: str
    comparables: tuple[WeightedComparable, ...]


class InsufficientComparablesError(Exception):
    """Raised when evidence is too thin to state a value.

    We refuse rather than extrapolate: a fair value derived from two sales is
    not a weaker answer, it is a wrong one.
    """

    def __init__(self, effective_n: float, count: int) -> None:
        super().__init__(
            f"insufficient comparable evidence: {count} comparables, "
            f"effective n={effective_n:.2f} (minimum {MIN_EFFECTIVE_N})"
        )
        self.effective_n = effective_n
        self.count = count


def _months_between(earlier: date, later: date) -> float:
    return (
        (later.year - earlier.year) * 12.0
        + (later.month - earlier.month)
        + (later.day - earlier.day) / 30.44
    )


def compute_weight(
    subject: SubjectProperty, comp: Comparable, as_of: date
) -> tuple[float, dict[str, float]]:
    """Product of independent kernels.

    A product, not a sum: one badly mismatched dimension should be able to veto
    a comparable outright, which a weighted sum cannot express.
    """
    k_dist = math.exp(-((comp.distance_m / LAMBDA_DISTANCE_M) ** 2))

    months = max(0.0, _months_between(comp.transacted_on, as_of))
    k_time = math.exp(-months / LAMBDA_TIME_MONTHS)

    area_dev = (comp.area_sqm - subject.area_sqm) / subject.area_sqm
    k_area = math.exp(-((area_dev / LAMBDA_AREA_FRAC) ** 2))

    subject_age = subject.age_years(as_of)
    comp_age = None if comp.build_year is None else max(0.0, as_of.year - comp.build_year)
    if subject_age is None or comp_age is None:
        k_age = 0.85  # unknown age is a mild penalty, not a veto
    else:
        k_age = math.exp(-((abs(comp_age - subject_age) / LAMBDA_AGE_YEARS) ** 2))

    if subject.floor is None or comp.floor is None:
        k_floor = 1.0
    else:
        k_floor = math.exp(-((abs(comp.floor - subject.floor) / 6.0) ** 2))

    m_project = (
        SAME_PROJECT_MULTIPLIER
        if subject.project_id is not None and subject.project_id == comp.project_id
        else 1.0
    )
    m_district = (
        SAME_DISTRICT_MULTIPLIER
        if subject.district_id is not None and subject.district_id == comp.district_id
        else 1.0
    )

    breakdown = {
        "distance": k_dist,
        "recency": k_time,
        "area": k_area,
        "age": k_age,
        "floor": k_floor,
        "same_project": m_project,
        "same_district": m_district,
    }
    weight = k_dist * k_time * k_area * k_age * k_floor * m_project * m_district
    return weight, breakdown


def time_adjust(
    price_per_sqm: float, index_at_sale: float | None, index_now: float | None
) -> float:
    """Index a historic price forward to today.

    Skipping this in a market that moved double digits produces systematically
    wrong discounts -- flattering in a rising market, which is the dangerous
    direction. When no index is available we return the raw value and the
    caller records IndexTier.NONE so confidence drops accordingly.
    """
    if index_at_sale is None or index_now is None or index_at_sale <= 0:
        return price_per_sqm
    return price_per_sqm * (index_now / index_at_sale)


def value_property(
    subject: SubjectProperty,
    comparables: Sequence[Comparable],
    *,
    as_of: date,
    index_now: float | None = None,
    index_at: dict[UUID, float] | None = None,
    index_tier: IndexTier = IndexTier.NONE,
    subject_completeness: float = 1.0,
) -> FairValue:
    """Estimate fair market value from comparable transactions."""
    if subject.area_sqm <= 0:
        raise ValueError("subject area must be positive")
    index_at = index_at or {}

    scored: list[WeightedComparable] = []
    for comp in comparables:
        weight, breakdown = compute_weight(subject, comp, as_of)
        adjusted = time_adjust(comp.price_per_sqm, index_at.get(comp.id), index_now)
        scored.append(WeightedComparable(comp, weight, adjusted, breakdown))

    if not scored:
        raise InsufficientComparablesError(0.0, 0)

    # Renormalise so weights read as "how comparable, relative to the best".
    max_weight = max(s.weight for s in scored)
    if max_weight <= 0:
        raise InsufficientComparablesError(0.0, len(scored))
    scored = [
        WeightedComparable(
            s.comparable, s.weight / max_weight, s.adjusted_price_per_sqm, s.weight_breakdown
        )
        for s in scored
    ]

    # Reject outliers before estimating rather than trusting the estimator to cope.
    lower, upper = iqr_bounds([s.adjusted_price_per_sqm for s in scored])
    evaluated = [
        s
        if lower <= s.adjusted_price_per_sqm <= upper
        else WeightedComparable(
            s.comparable,
            s.weight,
            s.adjusted_price_per_sqm,
            s.weight_breakdown,
            excluded_reason=(
                f"outlier: {s.adjusted_price_per_sqm:,.0f} SAR/m² outside "
                f"[{lower:,.0f}, {upper:,.0f}]"
            ),
        )
        for s in scored
    ]

    included = [s for s in evaluated if s.included]
    if not included:
        raise InsufficientComparablesError(0.0, len(evaluated))

    values = [s.adjusted_price_per_sqm for s in included]
    weights = [s.weight for s in included]
    eff_n = effective_sample_size(weights)

    if eff_n < MIN_EFFECTIVE_N:
        raise InsufficientComparablesError(eff_n, len(included))

    base_ppsqm = weighted_median(values, weights)
    low_ppsqm = weighted_quantile(values, weights, BAND_LOWER_QUANTILE)
    high_ppsqm = weighted_quantile(values, weights, BAND_UPPER_QUANTILE)

    q1 = weighted_quantile(values, weights, 0.25)
    q3 = weighted_quantile(values, weights, 0.75)
    median_v = base_ppsqm

    # How much the comparables disagree with *each other*, as a fraction of the
    # estimate. This is the term that predicts error, and in v1 it carried only
    # 0.15 of the weight while `mean(w)` -- how similar the comparables are to
    # the subject -- carried 0.25. Similarity to the subject says nothing about
    # whether the evidence agrees on a price: ten near-identical units in a
    # heterogeneous pocket are all excellent comparables that disagree wildly.
    # Track D measured the consequence as AUC 0.329, i.e. the score ranked
    # misses above hits.
    dispersion = (q3 - q1) / median_v if median_v > 0 else 1.0
    agreement = max(0.0, 1.0 - dispersion / DISPERSION_FULL_PENALTY)
    quality = sum(weights) / len(weights)

    confidence = (
        0.40 * agreement
        + 0.25 * min(1.0, eff_n / 12.0)
        + 0.10 * quality
        + 0.15 * index_tier.quality
        + 0.10 * subject_completeness
    )
    confidence = max(0.0, min(1.0, confidence))

    def to_money(ppsqm: float) -> Decimal:
        return Decimal(str(round(ppsqm * subject.area_sqm, 2)))

    return FairValue(
        base=to_money(base_ppsqm),
        low=to_money(low_ppsqm),
        high=to_money(high_ppsqm),
        base_price_per_sqm=base_ppsqm,
        comparable_count=len(included),
        effective_n=eff_n,
        comparable_quality=quality,
        confidence=confidence,
        index_tier=index_tier,
        method_version=METHOD_VERSION,
        comparables=tuple(sorted(evaluated, key=lambda s: -s.weight)),
    )
