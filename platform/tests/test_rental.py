"""Rental estimate, yield, and the synthetic rental corpus.

The pure tests pin the arithmetic of spec section 4 exactly. The integration
tests assert that the estimate reaches the database and that the yield reaches
the opportunity score, because a formula nothing calls is not a feature.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_domain.rental import (
    DEFAULT_MAINTENANCE_RESERVE_FRACTION,
    DEFAULT_MANAGEMENT_FRACTION,
    DEFAULT_OCCUPANCY,
    InsufficientRentalEvidenceError,
    RentalAssumptions,
    RentalComparable,
    RentalYield,
    YieldRefused,
    compute_yield,
    estimate_rent,
)
from sreoi_domain.scoring import Dimension
from sreoi_domain.valuation import Comparable, SubjectProperty, compute_weight
from sreoi_persistence.models import Source
from sreoi_persistence.models_rental import (
    RentalComparable as RentalComparableRow,
)
from sreoi_persistence.models_rental import (
    RentalEstimateComparable,
    RentalEstimateRow,
)
from sreoi_pipeline.ingest import ingest_manual_submission
from sreoi_pipeline.rental import (
    SYNTHETIC_RENTAL_SOURCE_KEY,
    RentalComparableRepository,
    seed_rental_comparables,
)
from tests.conftest import requires_db

AS_OF = date(2026, 8, 1)
DISTRICT = uuid.uuid4()


# --------------------------------------------------------------- helpers


def _rental_comp(
    rent_per_sqm_year: float,
    *,
    area: float = 140.0,
    distance: float = 300.0,
    months_ago: int = 3,
    district: uuid.UUID | None = DISTRICT,
) -> RentalComparable:
    year = AS_OF.year - (months_ago // 12)
    month = AS_OF.month - (months_ago % 12)
    if month <= 0:
        month += 12
        year -= 1
    return RentalComparable(
        id=uuid.uuid4(),
        annual_rent=Decimal(str(round(rent_per_sqm_year * area, 2))),
        area_sqm=area,
        contract_date=date(year, month, 1),
        distance_m=distance,
        property_class="APARTMENT",
        district_id=district,
        build_year=2020,
    )


def _subject(area: float = 140.0) -> SubjectProperty:
    return SubjectProperty("APARTMENT", area, district_id=DISTRICT, build_year=2020)


# --------------------------------------------------------------- pure: rent


def test_annual_rent_is_the_weighted_median_rent_per_sqm_times_area() -> None:
    """Spec section 4: annual_rent = WeightedMedian(comps) * area."""
    comps = [_rental_comp(500.0) for _ in range(8)]
    estimate = estimate_rent(_subject(140.0), comps, as_of=AS_OF)
    assert estimate.rent_per_sqm_year == pytest.approx(500.0)
    assert estimate.annual_rent == Decimal("70000.00")
    assert estimate.monthly_rent == Decimal("5833.33")
    assert estimate.comparable_count == 8


def test_band_is_ordered_and_widens_on_thin_evidence() -> None:
    tight = [_rental_comp(480.0 + i * 4) for i in range(14)]
    thin = [_rental_comp(480.0 + i * 4) for i in range(5)]
    wide = estimate_rent(_subject(), thin, as_of=AS_OF)
    narrow = estimate_rent(_subject(), tight, as_of=AS_OF)

    for estimate in (wide, narrow):
        assert estimate.annual_rent_low <= estimate.annual_rent <= estimate.annual_rent_high
    assert wide.effective_n < narrow.effective_n
    assert wide.confidence < narrow.confidence


def test_two_leases_are_refused_rather_than_extrapolated() -> None:
    with pytest.raises(InsufficientRentalEvidenceError) as excinfo:
        estimate_rent(_subject(), [_rental_comp(500.0), _rental_comp(520.0)], as_of=AS_OF)
    assert excinfo.value.effective_n < 3.0


def test_no_leases_at_all_is_refused() -> None:
    with pytest.raises(InsufficientRentalEvidenceError):
        estimate_rent(_subject(), [], as_of=AS_OF)


def test_a_furnished_outlier_is_excluded_and_says_why() -> None:
    comps = [_rental_comp(480.0 + i * 5) for i in range(10)]
    comps.append(_rental_comp(2400.0))  # a furnished short-let, not market rent
    estimate = estimate_rent(_subject(), comps, as_of=AS_OF)

    excluded = [c for c in estimate.comparables if not c.included]
    assert len(excluded) == 1
    assert excluded[0].comparable.annual_rent == Decimal(str(round(2400.0 * 140.0, 2)))
    assert "outlier" in (excluded[0].excluded_reason or "")
    # The outlier must not have moved the estimate.
    assert estimate.rent_per_sqm_year < 600.0


def test_rental_weights_are_the_valuation_kernels_not_a_copy() -> None:
    """Spec section 4 says "same kernels". Assert it rather than trust it."""
    subject = _subject(140.0)
    comp = _rental_comp(500.0, area=155.0, distance=800.0, months_ago=11)
    sale_equivalent = Comparable(
        id=comp.id,
        price=comp.annual_rent,
        area_sqm=comp.area_sqm,
        transacted_on=comp.contract_date,
        distance_m=comp.distance_m,
        property_class=comp.property_class,
        district_id=comp.district_id,
        build_year=comp.build_year,
    )
    rental_weight, rental_parts = compute_weight(subject, comp.as_similarity_subject(), AS_OF)
    sale_weight, sale_parts = compute_weight(subject, sale_equivalent, AS_OF)
    assert rental_weight == sale_weight
    assert rental_parts == sale_parts


def test_estimate_is_bit_identical_on_repeat() -> None:
    comps = [_rental_comp(470.0 + i * 7) for i in range(11)]
    first = estimate_rent(_subject(), comps, as_of=AS_OF)
    second = estimate_rent(_subject(), comps, as_of=AS_OF)
    assert first.annual_rent == second.annual_rent
    assert first.annual_rent_low == second.annual_rent_low
    assert first.annual_rent_high == second.annual_rent_high
    assert first.confidence == second.confidence
    assert first.method_version == "rental-v1"


# --------------------------------------------------------------- pure: yield


def test_gross_yield_is_rent_over_true_acquisition_cost() -> None:
    result = compute_yield(
        annual_rent=Decimal("100000"),
        true_acquisition_cost=Decimal("1000000"),
        cost_is_complete=True,
    )
    assert isinstance(result, RentalYield)
    assert result.gross == pytest.approx(0.10)
    assert result.true_acquisition_cost == Decimal("1000000.00")


def test_net_yield_applies_occupancy_and_the_three_opex_lines() -> None:
    """(rent * occupancy - opex) / cost, computed by hand."""
    result = compute_yield(
        annual_rent=Decimal("100000"),
        true_acquisition_cost=Decimal("1000000"),
        cost_is_complete=True,
    )
    assert isinstance(result, RentalYield)
    # effective gross income = 100000 * 0.92 = 92000
    # management            =  92000 * 0.08 =  7360   (on collected rent)
    # maintenance reserve   = 100000 * 0.05 =  5000   (on gross rent)
    # service charges       =                     0   (not supplied)
    assert result.effective_gross_income == Decimal("92000.00")
    assert result.opex_breakdown == {
        "service_charges": 0.0,
        "management": 7360.0,
        "maintenance_reserve": 5000.0,
    }
    assert result.opex_total == Decimal("12360.00")
    assert result.net == pytest.approx((92000 - 12360) / 1_000_000)
    assert result.net < result.gross


def test_every_assumption_is_returned_so_the_user_can_disagree() -> None:
    described = RentalAssumptions().describe()
    assert described["occupancy"] == DEFAULT_OCCUPANCY == 0.92
    assert described["management_fraction"] == DEFAULT_MANAGEMENT_FRACTION == 0.08
    assert described["maintenance_reserve_fraction"] == DEFAULT_MAINTENANCE_RESERVE_FRACTION
    # The base of each percentage is stated: "8% management" is ambiguous alone.
    assert described["management_basis"] == "effective_gross_income"
    assert described["maintenance_reserve_basis"] == "gross_annual_rent"
    # An unsupplied service charge is treated as zero AND says so.
    assert described["annual_service_charges"] == 0.0
    assert described["annual_service_charges_assumed_zero"] is True
    assert described["method_version"] == "rental-v1"


def test_user_overrides_change_the_net_yield() -> None:
    overridden = RentalAssumptions(
        occupancy=1.0,
        management_fraction=0.0,
        maintenance_reserve_fraction=0.0,
        annual_service_charges=Decimal("12000"),
    )
    result = compute_yield(
        annual_rent=Decimal("100000"),
        true_acquisition_cost=Decimal("1000000"),
        cost_is_complete=True,
        assumptions=overridden,
    )
    assert isinstance(result, RentalYield)
    assert result.net == pytest.approx(0.088)
    assert result.assumptions.describe()["annual_service_charges_assumed_zero"] is False


def test_yield_is_refused_when_the_cost_is_incomplete() -> None:
    """The section 3 invariant, applied to the yield.

    A seller asking 120k for a unit carrying 600k of installments would show a
    five-fold yield. Refusing is the only honest answer.
    """
    result = compute_yield(
        annual_rent=Decimal("68000"),
        true_acquisition_cost=Decimal("120000"),
        cost_is_complete=False,
    )
    assert isinstance(result, YieldRefused)
    assert "incomplete" in result.reason


def test_yield_is_refused_on_a_zero_cost() -> None:
    result = compute_yield(
        annual_rent=Decimal("68000"),
        true_acquisition_cost=Decimal("0"),
        cost_is_complete=True,
    )
    assert isinstance(result, YieldRefused)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"occupancy": 0.0},
        {"occupancy": 1.2},
        {"management_fraction": -0.1},
        {"maintenance_reserve_fraction": 1.0},
        {"annual_service_charges": Decimal("-1")},
    ],
)
def test_impossible_assumptions_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RentalAssumptions(**kwargs)


def test_negative_rent_is_a_programming_error_not_a_refusal() -> None:
    with pytest.raises(ValueError):
        compute_yield(
            annual_rent=Decimal("-1"),
            true_acquisition_cost=Decimal("1000000"),
            cost_is_complete=True,
        )


# --------------------------------------------------------------- integration

pytestmark_db = requires_db

SIDRAH_ASSIGNMENT: dict[str, Any] = {
    "opportunity_type": "ASSIGNMENT",
    "property_class": "APARTMENT",
    "district": "Sidrah",
    "area_sqm": 140,
    "bedrooms": 3,
    "floor": 4,
    "build_year": 2023,
    "seller_payment": 120000,
    "remaining_installments": 600000,
    "longitude": 46.8500,
    "latitude": 24.8700,
}


@pytest.fixture
def rental_corpus(session: Session) -> Iterator[int]:
    """Seed the synthetic rental corpus inside the test's own transaction.

    Nothing is committed, so the corpus disappears with the rollback and does
    not perturb the score fixtures other modules rely on.
    """
    yield seed_rental_comparables(session)


@requires_db
def test_synthetic_rental_corpus_is_labelled_synthetic(
    rental_corpus: int, session: Session
) -> None:
    """Generated leases must never be able to pass as market data."""
    assert rental_corpus >= 160
    source = session.scalar(select(Source).where(Source.key == SYNTHETIC_RENTAL_SOURCE_KEY))
    assert source is not None
    assert source.is_synthetic is True
    assert "SYNTHETIC" in source.data_license
    assert "NOT real" in source.data_license
    # Every generated lease hangs off that source, not off a real one.
    other = session.scalar(
        select(func.count())
        .select_from(RentalComparableRow)
        .where(RentalComparableRow.source_id != source.id)
    )
    assert int(other or 0) == 0


@requires_db
def test_seeding_the_corpus_twice_is_a_no_op(rental_corpus: int, session: Session) -> None:
    assert seed_rental_comparables(session) == 0


@requires_db
def test_generated_rents_sit_in_the_plausible_band(rental_corpus: int, session: Session) -> None:
    lo, hi = session.execute(
        select(
            func.min(RentalComparableRow.annual_rent / RentalComparableRow.area_sqm),
            func.max(RentalComparableRow.annual_rent / RentalComparableRow.area_sqm),
        )
    ).one()
    assert float(lo) >= 350.0
    assert float(hi) <= 650.0


@requires_db
def test_expanding_radius_reports_the_radius_it_needed(
    rental_corpus: int, session: Session
) -> None:
    comps, radius = RentalComparableRepository(session).find(
        longitude=46.8500,
        latitude=24.8700,
        property_class="APARTMENT",
        area_sqm=140.0,
        as_of=date.today(),
    )
    assert comps
    assert radius in (750.0, 1500.0, 3000.0, 6000.0)
    assert all(c.distance_m <= radius for c in comps)


@requires_db
def test_pipeline_stores_the_estimate_and_feeds_the_yield_into_the_score(
    isolated: None, rental_corpus: int, session: Session
) -> None:
    opportunity, result = ingest_manual_submission(
        session,
        {**SIDRAH_ASSIGNMENT, "external_id": "rental-wired", "title": "Assignment Sidrah rent"},
    )
    session.flush()

    row = session.scalar(
        select(RentalEstimateRow).where(RentalEstimateRow.opportunity_id == opportunity.id)
    )
    assert row is not None, "the rental estimate must be persisted"
    assert row.annual_rent > 0
    assert row.gross_yield is not None and row.net_yield is not None
    assert row.yield_refused_reason is None
    assert float(row.net_yield) < float(row.gross_yield)
    # Plausibility, not a golden value: the corpus is synthetic.
    assert 0.04 < float(row.gross_yield) < 0.15

    # The assumptions travel with the numbers.
    assert row.assumptions["occupancy"] == 0.92
    assert row.assumptions["management_basis"] == "effective_gross_income"
    assert "search_radius_m" in row.assumptions

    # The leases cited are retained with their weights.
    cited = session.scalars(
        select(RentalEstimateComparable).where(
            RentalEstimateComparable.rental_estimate_id == row.id
        )
    ).all()
    assert cited
    assert all(0 < float(c.weight) <= 1.0 for c in cited)

    # And the yield actually reaches the score, rather than scoring zero.
    assert result.score is not None
    rental_component = next(c for c in result.score.components if c.dimension is Dimension.RENTAL)
    # abs tolerance: the stored column is Numeric(8,6), the in-memory float is not.
    assert rental_component.raw_value == pytest.approx(float(row.gross_yield), abs=1e-6)
    assert rental_component.normalized_score > 0


@requires_db
def test_unknown_installments_refuse_the_yield_as_well_as_the_discount(
    isolated: None, rental_corpus: int, session: Session
) -> None:
    payload = {**SIDRAH_ASSIGNMENT, "external_id": "rental-refused", "title": "Assignment X"}
    del payload["remaining_installments"]
    opportunity, result = ingest_manual_submission(session, payload)
    session.flush()

    row = session.scalar(
        select(RentalEstimateRow).where(RentalEstimateRow.opportunity_id == opportunity.id)
    )
    assert row is not None
    # A rent is still stated -- it does not depend on the cost -- but the yield
    # is refused, because 68k of rent against a 120k "cost" is nonsense.
    assert row.annual_rent > 0
    assert row.gross_yield is None
    assert row.net_yield is None
    assert row.yield_refused_reason is not None

    assert result.score is not None
    rental_component = next(c for c in result.score.components if c.dimension is Dimension.RENTAL)
    assert rental_component.raw_value is None
    assert rental_component.normalized_score == 0.0


@requires_db
def test_no_lease_evidence_leaves_the_rental_dimension_unestablished(
    isolated: None, session: Session
) -> None:
    """Without a rental corpus nothing is invented and nothing is stored."""
    opportunity, result = ingest_manual_submission(
        session, {**SIDRAH_ASSIGNMENT, "external_id": "rental-none", "title": "Assignment none"}
    )
    session.flush()

    row = session.scalar(
        select(RentalEstimateRow).where(RentalEstimateRow.opportunity_id == opportunity.id)
    )
    assert row is None
    assert result.score is not None
    rental_component = next(c for c in result.score.components if c.dimension is Dimension.RENTAL)
    assert rental_component.raw_value is None
