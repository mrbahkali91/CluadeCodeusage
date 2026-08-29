"""Robust weighted statistics.

Saudi transaction data contains genuine extremes -- nominal family transfers,
land-assembly premiums -- so the estimator is a weighted median, never a mean.
"""

from __future__ import annotations

from collections.abc import Sequence


def weighted_quantile(values: Sequence[float], weights: Sequence[float], q: float) -> float:
    """Weighted quantile via the cumulative-weight midpoint convention.

    With equal weights this agrees with the ordinary definition of a median.
    """
    if len(values) != len(weights):
        raise ValueError("values and weights must be the same length")
    if not values:
        raise ValueError("cannot take a quantile of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0,1], got {q}")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")

    pairs = sorted(zip(values, weights, strict=True), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        raise ValueError("weights must sum to a positive number")

    cumulative = 0.0
    # Midpoint convention: each point spans [cum, cum+w]; we interpolate on its centre.
    centres: list[tuple[float, float]] = []
    for value, weight in pairs:
        centres.append((value, (cumulative + weight / 2.0) / total))
        cumulative += weight

    if q <= centres[0][1]:
        return centres[0][0]
    if q >= centres[-1][1]:
        return centres[-1][0]

    for i in range(1, len(centres)):
        v0, p0 = centres[i - 1]
        v1, p1 = centres[i]
        if q <= p1:
            if p1 == p0:
                return v1
            frac = (q - p0) / (p1 - p0)
            return v0 + frac * (v1 - v0)
    return centres[-1][0]


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    return weighted_quantile(values, weights, 0.5)


def effective_sample_size(weights: Sequence[float]) -> float:
    """Kish effective sample size.

    Twenty comparables of which nineteen are marginal is not twenty comparables.
    This is what keeps the confidence figure honest.
    """
    total = sum(weights)
    sum_sq = sum(w * w for w in weights)
    if sum_sq <= 0:
        return 0.0
    return (total * total) / sum_sq


def iqr_bounds(values: Sequence[float], k: float = 1.5) -> tuple[float, float]:
    """Tukey fences on the unweighted distribution, used to reject outliers."""
    if not values:
        raise ValueError("cannot compute bounds of no values")
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        if n == 1:
            return ordered[0]
        pos = p * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        return ordered[lo] + (pos - lo) * (ordered[hi] - ordered[lo])

    q1, q3 = pct(0.25), pct(0.75)
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr
