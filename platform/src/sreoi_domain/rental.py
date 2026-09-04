"""Rental estimate, gross yield and net yield.

Deterministic and pure. No LLM participates in any calculation in this module
(ADR-010). See docs/architecture/valuation-and-scoring.md section 4:

    annual_rent = WeightedMedian(rental comps, same kernels) * area
    gross_yield = annual_rent / true_acquisition_cost
    net_yield   = (annual_rent * occupancy - opex) / true_acquisition_cost

Two properties of this module are deliberate rather than incidental.

**Every assumption is returned, not buried.** `RentalAssumptions.describe()`
is carried out with the estimate so the UI can display occupancy, management,
maintenance and service charges, and the user can disagree with any of them.
An assumption the user cannot see is one they cannot disagree with.

**The denominator is the true acquisition cost, never the asking price**, and a
yield is refused outright when that cost is incomplete. A yield computed
against a partial cost has a too-small denominator and therefore flatters the
return -- the same failure mode that section 3 refuses for the discount, and
in the same dangerous direction.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sreoi_domain.stats import (
    effective_sample_size,
    iqr_bounds,
    weighted_median,
    weighted_quantile,
)
from sreoi_domain.valuation import Comparable, SubjectProperty, compute_weight

METHOD_VERSION = "rental-v1"

# Documented defaults, all displayed and all overridable (spec section 4).
DEFAULT_OCCUPANCY = 0.92
DEFAULT_MANAGEMENT_FRACTION = 0.08
DEFAULT_MAINTENANCE_RESERVE_FRACTION = 0.05

# Rental evidence is thinner and more dispersed than sale evidence (furnished
# lets, related-party leases, shorter terms), so the expanding-radius target is
# HIGHER than the valuation engine's eight rather than lower: at eight leases
# the Kish effective sample size lands around 2.5-3.5 and the estimate is
# refused about a third of the time. Twelve pushes the search one radius step
# out and lifts effective n to 3.6-11.4 across the four Riyadh districts. The
# radius that was needed is recorded and penalises confidence.
#
# The refusal floor is NOT relaxed to compensate: three effective comparables
# remains the point below which we decline to state a number, exactly as for
# fair value.
MIN_RENTAL_COMPARABLES = 12
MIN_EFFECTIVE_N = 3.0

_CENTS = Decimal("0.01")


def _money(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


class InsufficientRentalEvidenceError(Exception):
    """Raised when rental evidence is too thin to state a rent.

    Mirrors `valuation.InsufficientComparablesError`: we refuse rather than
    extrapolate. A rent extrapolated from two contracts is not a weaker
    answer, it is a wrong one, and it would propagate into the yield component
    of the opportunity score.
    """

    def __init__(self, effective_n: float, count: int) -> None:
        super().__init__(
            f"insufficient rental evidence: {count} comparables, "
            f"effective n={effective_n:.2f} (minimum {MIN_EFFECTIVE_N})"
        )
        self.effective_n = effective_n
        self.count = count


@dataclass(frozen=True, slots=True)
class RentalComparable:
    """A signed lease used as rental evidence.

    `annual_rent` is the contract rent for a full year. Monthly contracts are
    annualised by the loader, not here, so this type never has to guess a term.
    """

    id: UUID
    annual_rent: Decimal
    area_sqm: float
    contract_date: date
    distance_m: float
    property_class: str
    district_id: UUID | None = None
    project_id: UUID | None = None
    build_year: int | None = None
    floor: int | None = None

    @property
    def rent_per_sqm_year(self) -> float:
        if self.area_sqm <= 0:
            raise ValueError(f"rental comparable {self.id} has non-positive area")
        return float(self.annual_rent) / self.area_sqm

    def as_similarity_subject(self) -> Comparable:
        """Adapter that lets the valuation kernels apply unchanged.

        Spec section 4 says "same kernels". Rather than copy the seven kernels
        and let the two definitions drift apart, the rental comparable is
        presented to `valuation.compute_weight` as a sale comparable whose
        `price` happens to be the annual rent. The kernels read only distance,
        date, area, age and floor, so the weights are identical by
        construction and any future change to the kernels applies to both.
        """
        return Comparable(
            id=self.id,
            price=self.annual_rent,
            area_sqm=self.area_sqm,
            transacted_on=self.contract_date,
            distance_m=self.distance_m,
            property_class=self.property_class,
            district_id=self.district_id,
            project_id=self.project_id,
            build_year=self.build_year,
            floor=self.floor,
        )


@dataclass(frozen=True, slots=True)
class WeightedRentalComparable:
    """A rental comparable with its similarity weight and rent per m2/year."""

    comparable: RentalComparable
    weight: float
    rent_per_sqm_year: float
    weight_breakdown: dict[str, float]
    excluded_reason: str | None = None

    @property
    def included(self) -> bool:
        return self.excluded_reason is None


@dataclass(frozen=True, slots=True)
class RentalAssumptions:
    """The operating assumptions behind a net yield.

    Held as a value object so that a user override is a different object rather
    than a mutated one, which keeps a stored yield reproducible: the exact
    assumption set that produced it is recoverable from the record.
    """

    occupancy: float = DEFAULT_OCCUPANCY
    management_fraction: float = DEFAULT_MANAGEMENT_FRACTION
    maintenance_reserve_fraction: float = DEFAULT_MAINTENANCE_RESERVE_FRACTION
    # None means "not supplied". It is then treated as zero AND said so, which
    # is different from a user asserting the charges are genuinely zero.
    annual_service_charges: Decimal | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.occupancy <= 1.0:
            raise ValueError(f"occupancy must be in (0,1], got {self.occupancy}")
        for name, value in (
            ("management_fraction", self.management_fraction),
            ("maintenance_reserve_fraction", self.maintenance_reserve_fraction),
        ):
            if not 0.0 <= value < 1.0:
                raise ValueError(f"{name} must be in [0,1), got {value}")
        if self.annual_service_charges is not None and self.annual_service_charges < 0:
            raise ValueError("annual service charges cannot be negative")

    @property
    def service_charges(self) -> Decimal:
        return Decimal("0") if self.annual_service_charges is None else self.annual_service_charges

    @property
    def service_charges_assumed_zero(self) -> bool:
        return self.annual_service_charges is None

    def describe(self) -> dict[str, Any]:
        """Everything the UI must show, including the bases of the percentages.

        "8% management" is ambiguous until you say 8% of what. Management is
        charged on rent actually collected (effective gross income); the
        maintenance reserve is set aside against gross contract rent.
        """
        return {
            "occupancy": self.occupancy,
            "management_fraction": self.management_fraction,
            "management_basis": "effective_gross_income",
            "maintenance_reserve_fraction": self.maintenance_reserve_fraction,
            "maintenance_reserve_basis": "gross_annual_rent",
            "annual_service_charges": float(self.service_charges),
            "annual_service_charges_assumed_zero": self.service_charges_assumed_zero,
            "method_version": METHOD_VERSION,
        }


DEFAULT_ASSUMPTIONS = RentalAssumptions()


@dataclass(frozen=True, slots=True)
class RentalEstimate:
    """The output of the rental engine."""

    annual_rent: Decimal
    annual_rent_low: Decimal
    annual_rent_high: Decimal
    rent_per_sqm_year: float
    comparable_count: int
    effective_n: float
    comparable_quality: float
    confidence: float
    method_version: str
    comparables: tuple[WeightedRentalComparable, ...]

    @property
    def monthly_rent(self) -> Decimal:
        return _money(self.annual_rent / 12)


@dataclass(frozen=True, slots=True)
class YieldRefused:
    """A refusal that names what blocks the calculation."""

    reason: str


@dataclass(frozen=True, slots=True)
class RentalYield:
    gross: float
    net: float
    annual_rent: Decimal
    effective_gross_income: Decimal
    opex_total: Decimal
    opex_breakdown: dict[str, float]
    true_acquisition_cost: Decimal
    assumptions: RentalAssumptions
    method_version: str = METHOD_VERSION

    @property
    def gross_percent(self) -> float:
        return self.gross * 100.0

    @property
    def net_percent(self) -> float:
        return self.net * 100.0


def estimate_rent(
    subject: SubjectProperty,
    comparables: Sequence[RentalComparable],
    *,
    as_of: date,
    subject_completeness: float = 1.0,
) -> RentalEstimate:
    """Estimate annual market rent from comparable leases.

    Structurally identical to `valuation.value_property`, and deliberately so:
    weights from the same kernels, outliers rejected before the estimator, a
    weighted median rather than a mean, and a band widened for thin evidence.
    """
    if subject.area_sqm <= 0:
        raise ValueError("subject area must be positive")

    scored: list[WeightedRentalComparable] = []
    for comp in comparables:
        weight, breakdown = compute_weight(subject, comp.as_similarity_subject(), as_of)
        scored.append(WeightedRentalComparable(comp, weight, comp.rent_per_sqm_year, breakdown))

    if not scored:
        raise InsufficientRentalEvidenceError(0.0, 0)

    # Renormalise so weights read as "how comparable, relative to the best".
    max_weight = max(s.weight for s in scored)
    if max_weight <= 0:
        raise InsufficientRentalEvidenceError(0.0, len(scored))
    scored = [
        WeightedRentalComparable(
            s.comparable, s.weight / max_weight, s.rent_per_sqm_year, s.weight_breakdown
        )
        for s in scored
    ]

    # Reject outliers before estimating. Rental data carries its own extremes:
    # related-party leases, and units let furnished at a large premium.
    lower, upper = iqr_bounds([s.rent_per_sqm_year for s in scored])
    evaluated = [
        s
        if lower <= s.rent_per_sqm_year <= upper
        else WeightedRentalComparable(
            s.comparable,
            s.weight,
            s.rent_per_sqm_year,
            s.weight_breakdown,
            excluded_reason=(
                f"outlier: {s.rent_per_sqm_year:,.0f} SAR/sqm/year outside "
                f"[{lower:,.0f}, {upper:,.0f}]"
            ),
        )
        for s in scored
    ]

    included = [s for s in evaluated if s.included]
    if not included:
        raise InsufficientRentalEvidenceError(0.0, len(evaluated))

    values = [s.rent_per_sqm_year for s in included]
    weights = [s.weight for s in included]
    eff_n = effective_sample_size(weights)

    if eff_n < MIN_EFFECTIVE_N:
        raise InsufficientRentalEvidenceError(eff_n, len(included))

    base = weighted_median(values, weights)
    q1 = weighted_quantile(values, weights, 0.25)
    q3 = weighted_quantile(values, weights, 0.75)

    spread = 1.0 + 0.6 / math.sqrt(eff_n)
    low = q1 / spread
    high = q3 * spread

    agreement = 1.0 - min(1.0, (q3 - q1) / base) if base > 0 else 0.0
    quality = sum(weights) / len(weights)

    # Adapted from spec section 2.3. The index-quality term is dropped because
    # no rental price index is available in any CONFIRMED source (see
    # docs/data-sources/matrix.md); its 0.15 is redistributed to evidence
    # quantity and quality rather than silently scored as 1.0, which would
    # award confidence we have not earned.
    confidence = (
        0.45 * min(1.0, eff_n / 12.0)
        + 0.30 * quality
        + 0.15 * agreement
        + 0.10 * subject_completeness
    )
    confidence = max(0.0, min(1.0, confidence))

    return RentalEstimate(
        annual_rent=_money(base * subject.area_sqm),
        annual_rent_low=_money(low * subject.area_sqm),
        annual_rent_high=_money(high * subject.area_sqm),
        rent_per_sqm_year=base,
        comparable_count=len(included),
        effective_n=eff_n,
        comparable_quality=quality,
        confidence=confidence,
        method_version=METHOD_VERSION,
        comparables=tuple(sorted(evaluated, key=lambda s: -s.weight)),
    )


def compute_yield(
    *,
    annual_rent: Decimal,
    true_acquisition_cost: Decimal,
    cost_is_complete: bool,
    assumptions: RentalAssumptions = DEFAULT_ASSUMPTIONS,
) -> RentalYield | YieldRefused:
    """Gross and net yield against the true acquisition cost.

    Never against the asking price, and never against an incomplete cost: a
    seller asking SAR 120,000 for a unit carrying SAR 600,000 of remaining
    installments would show a five-fold yield, which is nonsense in exactly
    the flattering direction.
    """
    if annual_rent < 0:
        raise ValueError("annual rent cannot be negative")
    if not cost_is_complete:
        return YieldRefused(
            "true acquisition cost is incomplete: a yield against a partial "
            "cost overstates the return, so it is refused rather than estimated"
        )
    if true_acquisition_cost <= 0:
        return YieldRefused("true acquisition cost must be positive to compute a yield")

    occupancy = Decimal(str(assumptions.occupancy))
    effective_gross = annual_rent * occupancy

    management = effective_gross * Decimal(str(assumptions.management_fraction))
    maintenance = annual_rent * Decimal(str(assumptions.maintenance_reserve_fraction))
    service = assumptions.service_charges
    opex = management + maintenance + service

    gross = float(annual_rent / true_acquisition_cost)
    net = float((effective_gross - opex) / true_acquisition_cost)

    return RentalYield(
        gross=gross,
        net=net,
        annual_rent=_money(annual_rent),
        effective_gross_income=_money(effective_gross),
        opex_total=_money(opex),
        opex_breakdown={
            "service_charges": float(_money(service)),
            "management": float(_money(management)),
            "maintenance_reserve": float(_money(maintenance)),
        },
        true_acquisition_cost=_money(true_acquisition_cost),
        assumptions=assumptions,
    )
