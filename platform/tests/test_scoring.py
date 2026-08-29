"""Scoring: reproducibility and the confidence gate (ADR-010)."""

from __future__ import annotations

from typing import Any

import pytest

from sreoi_domain.scoring import (
    CONFIDENCE_CAP_LEVEL,
    CONFIDENCE_FLOOR,
    DEFAULT_WEIGHTS,
    Classification,
    ConfidenceInputs,
    Dimension,
    OpportunityScore,
    WeightProfile,
    classify,
    discount_score,
    rental_score,
    score_opportunity,
)

HIGH_CONFIDENCE = ConfidenceInputs(0.95, 1.0, 0.95, 0.95, 1.0)


def _score(**overrides: Any) -> OpportunityScore:
    kwargs: dict[str, Any] = {
        "discount_fraction": 0.20,
        "gross_yield": 0.08,
        "liquidity": 85.0,
        "location": 90.0,
        "developer": 70.0,
        "risk_score": 80.0,
        "confidence": HIGH_CONFIDENCE,
    }
    kwargs.update(overrides)
    return score_opportunity(**kwargs)


def test_score_is_bit_identical_across_runs() -> None:
    """The reproducibility contract: same inputs => identical score."""
    a = _score()
    b = _score()
    assert a.total == b.total
    assert [c.contribution for c in a.components] == [c.contribution for c in b.components]
    assert a.weight_profile_version == b.weight_profile_version


def test_golden_score_value() -> None:
    """A golden fixture, hand-derived rather than snapshotted.

    discount 0.20            -> 80.00 x 0.30 = 24.000
    liquidity                -> 85.00 x 0.15 = 12.750
    yield 0.08               -> 82.00 x 0.15 = 12.300
    location                 -> 90.00 x 0.10 =  9.000
    developer                -> 70.00 x 0.10 =  7.000
    risk                     -> 80.00 x 0.10 =  8.000
    confidence 0.9675        -> 96.75 x 0.10 =  9.675
                                       total = 82.725

    Changing this number requires a new method version and a backfill decision.
    """
    result = _score()
    assert result.data_confidence == pytest.approx(0.9675, abs=1e-9)
    assert result.total == pytest.approx(82.725, abs=0.001)
    assert result.classification is Classification.STRONG


def test_components_sum_to_total_under_the_weight_profile() -> None:
    result = _score()
    assert sum(c.contribution for c in result.components) == pytest.approx(result.total, abs=1e-6)


def test_every_component_exposes_its_inputs() -> None:
    for component in _score().components:
        assert component.inputs, f"{component.dimension} exposes no inputs"


def test_confidence_floor_suppresses_the_recommendation() -> None:
    """A high raw score with weak evidence must not be presented as an opportunity."""
    weak = ConfidenceInputs(0.3, 0.4, 0.0, 0.4, 0.3)
    assert weak.data_confidence < CONFIDENCE_FLOOR
    result = _score(discount_fraction=0.40, confidence=weak)
    assert result.classification is Classification.INSUFFICIENT_DATA
    assert not result.is_actionable


def test_middle_confidence_caps_classification() -> None:
    """Between the floor and the cap, nothing may read Strong or Exceptional."""
    mid = ConfidenceInputs(0.70, 0.75, 0.55, 0.70, 0.70)
    assert CONFIDENCE_FLOOR <= mid.data_confidence < CONFIDENCE_CAP_LEVEL
    result = _score(discount_fraction=0.40, confidence=mid)
    assert result.classification is Classification.WORTH_REVIEWING
    assert result.capped is True


def test_refused_discount_scores_zero_rather_than_being_dropped() -> None:
    """Dropping the dimension would silently re-weight every other one upward."""
    result = _score(discount_fraction=None)
    discount = next(c for c in result.components if c.dimension is Dimension.DISCOUNT)
    assert discount.normalized_score == 0.0
    assert discount.weight == pytest.approx(0.30)


def test_discount_mapping_is_monotonic() -> None:
    xs = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
    scores = [discount_score(x) for x in xs]
    assert scores == sorted(scores)
    assert discount_score(-0.10) == 0.0
    assert discount_score(1.0) == 100.0


def test_yield_mapping_is_monotonic() -> None:
    scores = [rental_score(y) for y in [0.0, 0.03, 0.05, 0.07, 0.09, 0.12]]
    assert scores == sorted(scores)


def test_weight_profile_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        WeightProfile("bad", {d: 0.5 for d in Dimension})


def test_weight_profile_must_cover_every_dimension() -> None:
    partial = {d: 0.5 for d in list(Dimension)[:2]}
    with pytest.raises(ValueError, match="missing dimensions"):
        WeightProfile("bad", partial)


def test_alternative_profile_changes_the_ranking() -> None:
    """Weights are an investment thesis, so a yield-seeker ranks differently."""
    yield_focused = WeightProfile(
        "yield-v1",
        {
            Dimension.DISCOUNT: 0.10,
            Dimension.LIQUIDITY: 0.10,
            Dimension.RENTAL: 0.45,
            Dimension.LOCATION: 0.10,
            Dimension.DEVELOPER: 0.05,
            Dimension.RISK: 0.10,
            Dimension.CONFIDENCE: 0.10,
        },
    )
    default = _score(discount_fraction=0.30, gross_yield=0.04)
    alternative = _score(discount_fraction=0.30, gross_yield=0.04, profile=yield_focused)
    assert alternative.total < default.total
    assert alternative.weight_profile_version == "yield-v1"


def test_classify_boundaries() -> None:
    conf = 0.9
    assert classify(90.0, conf)[0] is Classification.EXCEPTIONAL
    assert classify(80.0, conf)[0] is Classification.STRONG
    assert classify(70.0, conf)[0] is Classification.WORTH_REVIEWING
    assert classify(60.0, conf)[0] is Classification.WATCHLIST
    assert classify(59.9, conf)[0] is Classification.WEAK
    assert classify(95.0, 0.5)[0] is Classification.INSUFFICIENT_DATA


def test_default_profile_matches_the_specification() -> None:
    assert DEFAULT_WEIGHTS.weights[Dimension.DISCOUNT] == 0.30
    assert sum(DEFAULT_WEIGHTS.weights.values()) == pytest.approx(1.0)
