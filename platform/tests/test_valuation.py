"""Comparable selection, time adjustment and fair value."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from sreoi_domain.valuation import (
    Comparable,
    IndexTier,
    InsufficientComparablesError,
    SubjectProperty,
    compute_weight,
    time_adjust,
    value_property,
)

AS_OF = date(2026, 8, 1)
DISTRICT = uuid.uuid4()


def _comp(
    ppsqm: float,
    *,
    area: float = 140.0,
    distance: float = 300.0,
    months_ago: int = 3,
    district: uuid.UUID | None = DISTRICT,
) -> Comparable:
    year = AS_OF.year - (months_ago // 12)
    month = AS_OF.month - (months_ago % 12)
    if month <= 0:
        month += 12
        year -= 1
    return Comparable(
        id=uuid.uuid4(),
        price=Decimal(str(round(ppsqm * area, 2))),
        area_sqm=area,
        transacted_on=date(year, month, 1),
        distance_m=distance,
        property_class="APARTMENT",
        district_id=district,
        build_year=2020,
    )


def _subject(area: float = 140.0) -> SubjectProperty:
    return SubjectProperty("APARTMENT", area, district_id=DISTRICT, build_year=2020)


def test_fair_value_bands_are_ordered() -> None:
    comps = [_comp(6000 + i * 60) for i in range(12)]
    fv = value_property(_subject(), comps, as_of=AS_OF)
    assert fv.low <= fv.base <= fv.high
    assert fv.comparable_count == 12


def test_refuses_when_evidence_is_too_thin() -> None:
    """We refuse rather than extrapolate from two sales."""
    with pytest.raises(InsufficientComparablesError):
        value_property(_subject(), [_comp(6000), _comp(6100)], as_of=AS_OF)


def test_refuses_when_there_are_no_comparables() -> None:
    with pytest.raises(InsufficientComparablesError):
        value_property(_subject(), [], as_of=AS_OF)


def test_nearer_and_more_recent_comparables_weigh_more() -> None:
    subject = _subject()
    near_recent, _ = compute_weight(subject, _comp(6000, distance=100, months_ago=1), AS_OF)
    far_old, _ = compute_weight(subject, _comp(6000, distance=4000, months_ago=20), AS_OF)
    assert near_recent > far_old


def test_area_mismatch_reduces_weight() -> None:
    subject = _subject(140)
    close, _ = compute_weight(subject, _comp(6000, area=145), AS_OF)
    distant, _ = compute_weight(subject, _comp(6000, area=210), AS_OF)
    assert close > distant


def test_same_district_is_favoured() -> None:
    subject = _subject()
    same, _ = compute_weight(subject, _comp(6000), AS_OF)
    other, _ = compute_weight(subject, _comp(6000, district=uuid.uuid4()), AS_OF)
    assert same > other


def test_time_adjustment_indexes_forward() -> None:
    # A sale at index 100 valued when the index is 110 is worth 10% more today.
    assert time_adjust(6000.0, 100.0, 110.0) == pytest.approx(6600.0)
    # Missing index data must not silently corrupt the number.
    assert time_adjust(6000.0, None, 110.0) == 6000.0
    assert time_adjust(6000.0, 0.0, 110.0) == 6000.0


def test_time_adjustment_changes_the_valuation() -> None:
    comps = [_comp(6000, months_ago=12) for _ in range(12)]
    index_at = dict.fromkeys((c.id for c in comps), 100.0)
    flat = value_property(_subject(), comps, as_of=AS_OF)
    rising = value_property(
        _subject(),
        comps,
        as_of=AS_OF,
        index_now=115.0,
        index_at=index_at,
        index_tier=IndexTier.DISTRICT,
    )
    assert rising.base > flat.base


def test_outliers_are_excluded_but_retained_for_display() -> None:
    comps = [_comp(6000 + i * 20) for i in range(12)]
    comps.append(_comp(60000))  # an order of magnitude out
    fv = value_property(_subject(), comps, as_of=AS_OF)
    excluded = [c for c in fv.comparables if not c.included]
    assert len(excluded) == 1
    assert excluded[0].excluded_reason is not None
    assert "outlier" in excluded[0].excluded_reason
    # The outlier must not have moved the estimate.
    assert float(fv.base_price_per_sqm) < 7000


def test_thin_evidence_lowers_confidence_rather_than_widening_the_band() -> None:
    """The `valuation-v2` contract, and a deliberate reversal of v1.

    v1 widened the band by `1 + 0.6/sqrt(n_eff)`, so thin evidence bought
    coverage by inflating the interval -- at the effective sample sizes the
    engine actually achieves that produced a band 58.8% of value wide, which
    cannot separate a 20% discount from fair value. v2 states the band as an
    empirical quantile pair, so thin evidence is reported where it belongs: in
    the confidence figure, not by making the value claim vaguer.
    """
    tight = value_property(_subject(), [_comp(6000 + i * 10) for i in range(25)], as_of=AS_OF)
    thin = value_property(_subject(), [_comp(6000 + i * 10) for i in range(8)], as_of=AS_OF)
    assert thin.effective_n < tight.effective_n
    assert thin.confidence < tight.confidence


def test_disagreeing_comparables_lower_confidence_more_than_thin_ones() -> None:
    """Agreement carries 0.40 of the formula and evidence quantity 0.25.

    v1 had this backwards -- `mean(w)`, how *similar* the comparables are to the
    subject, carried 0.25 while their agreement with *each other* carried only
    0.15. Similarity predicts nothing about whether the evidence agrees on a
    price, and Track D measured the consequence as AUC 0.329: the score ranked
    misses above hits.
    """
    agreeing = value_property(_subject(), [_comp(6000 + i * 10) for i in range(14)], as_of=AS_OF)
    disagreeing = value_property(
        _subject(), [_comp(4800 + i * 190) for i in range(14)], as_of=AS_OF
    )
    assert disagreeing.confidence < agreeing.confidence
    disagreeing_width = float(disagreeing.high - disagreeing.low) / float(disagreeing.base)
    agreeing_width = float(agreeing.high - agreeing.low) / float(agreeing.base)
    # And the band widens with genuine disagreement, because it is the evidence
    # spread rather than a sampling-distribution guess.
    assert disagreeing_width > agreeing_width


def test_the_band_is_the_evidence_spread_with_no_inflation_term() -> None:
    """Guards the specific defect: any factor applied to the quantile pair
    reappears as an unusable band. Ten comparables spanning a known range must
    produce a band inside that range, never wider than it."""
    prices = [5500 + i * 100 for i in range(11)]  # 5500..6500
    fv = value_property(_subject(), [_comp(p) for p in prices], as_of=AS_OF)
    low = float(fv.low) / _subject().area_sqm
    high = float(fv.high) / _subject().area_sqm
    assert min(prices) <= low <= high <= max(prices), (low, high)


def test_index_tier_affects_confidence() -> None:
    comps = [_comp(6000 + i * 30) for i in range(14)]
    district = value_property(_subject(), comps, as_of=AS_OF, index_tier=IndexTier.DISTRICT)
    national = value_property(_subject(), comps, as_of=AS_OF, index_tier=IndexTier.NATIONAL)
    assert district.confidence > national.confidence


def test_zero_area_subject_is_rejected() -> None:
    with pytest.raises(ValueError, match="area must be positive"):
        value_property(SubjectProperty("APARTMENT", 0.0), [_comp(6000)], as_of=AS_OF)
