"""Robust statistics."""

from __future__ import annotations

import pytest

from sreoi_domain.stats import (
    effective_sample_size,
    iqr_bounds,
    weighted_median,
    weighted_quantile,
)


def test_weighted_median_matches_plain_median_with_equal_weights() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert weighted_median(values, [1.0] * 5) == pytest.approx(30.0)


def test_weighted_median_follows_the_weight() -> None:
    # A heavily weighted low value must drag the median down.
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    heavy_low = weighted_median(values, [10.0, 1.0, 1.0, 1.0, 1.0])
    heavy_high = weighted_median(values, [1.0, 1.0, 1.0, 1.0, 10.0])
    assert heavy_low < heavy_high


def test_weighted_median_is_robust_to_an_extreme_outlier() -> None:
    # The reason we do not use a mean: one nominal family transfer must not move it.
    normal = [6000.0, 6100.0, 6200.0, 6300.0]
    with_outlier = [*normal, 1.0]
    weights = [1.0] * 5
    assert weighted_median(with_outlier, weights) == pytest.approx(
        weighted_median(normal, [1.0] * 4), rel=0.05
    )


def test_quantiles_are_ordered() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    w = [1.0] * 6
    assert weighted_quantile(values, w, 0.25) <= weighted_quantile(values, w, 0.5)
    assert weighted_quantile(values, w, 0.5) <= weighted_quantile(values, w, 0.75)


def test_effective_sample_size_penalises_uneven_weights() -> None:
    # Twenty comparables of which nineteen are marginal is not twenty comparables.
    assert effective_sample_size([1.0] * 10) == pytest.approx(10.0)
    lopsided = effective_sample_size([1.0] + [0.01] * 19)
    assert lopsided < 2.0


def test_weighted_quantile_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="same length"):
        weighted_quantile([1.0, 2.0], [1.0], 0.5)
    with pytest.raises(ValueError, match="no values"):
        weighted_quantile([], [], 0.5)
    with pytest.raises(ValueError, match="non-negative"):
        weighted_quantile([1.0], [-1.0], 0.5)


def test_iqr_bounds_bracket_the_bulk() -> None:
    low, high = iqr_bounds([10.0, 11.0, 12.0, 13.0, 14.0])
    assert low < 10.0 and high > 14.0
