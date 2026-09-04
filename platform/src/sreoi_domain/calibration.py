"""Confidence calibration: does the stated confidence earn the trust it invites?

Specified by docs/architecture/valuation-and-scoring.md section 7 and backlog
E4.4. Pure and deterministic.

The specification's wording is the point of this module: **an uncalibrated
confidence score is worse than none, because it invites trust it has not
earned.** A valuation labelled "86% confident" whose band contains the
realised price half the time is not a slightly optimistic product, it is a
product that has taught its users to believe a number that does not mean what
it says.

Calibration therefore needs an explicit claim to test. `confidence_valuation`
in section 2.3 is a weighted sum of evidence-quality terms, not a stated
probability, so this module makes the interpretation users will inevitably
place on it -- *"the published band contains the realised price about this
often"* -- and measures whether it holds. Where it does not, the honest
remedies are to recalibrate the mapping, widen the band, or stop displaying
the number as a percentage; all three are decisions the reliability curve
below is meant to inform.

Three complementary readings are reported because each catches a different
failure:

* **Reliability curve / ECE** -- is the *level* right? (Am I 80% confident and
  right 55% of the time?)
* **Brier skill** -- is the score better than a constant? A confidence figure
  with zero skill is decoration, however well-centred.
* **Discrimination (AUC, rank correlation)** -- does the score at least
  *order* cases correctly, even if its level is wrong? A score that ranks well
  but sits too high is repairable by recalibration; one that does not rank at
  all is not.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

METHOD_VERSION = "calibration-v1"

# Buckets. Narrow near the top of the range because that is where an
# over-claim does the most damage.
DEFAULT_BUCKET_EDGES: tuple[float, ...] = (0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

# How far a bucket's realised rate may sit from its claim before we call it
# miscalibrated. 10 points is generous; anything tighter is not measurable at
# the sample sizes available here.
CALIBRATION_TOLERANCE = 0.10

# Default point-accuracy tolerance, matching the back-test target.
DEFAULT_ERROR_TOLERANCE = 0.12


class HitCriterion(StrEnum):
    """What "being right" means for this calibration run."""

    # The published band contains the realised price. This is the claim the
    # product actually makes, so it is the primary criterion.
    INTERVAL = "INTERVAL"
    # The point estimate is within tolerance. Secondary, but it is what a user
    # reading a single headline number will assume.
    TOLERANCE = "TOLERANCE"


class CalibrationVerdict(StrEnum):
    OVERCONFIDENT = "OVERCONFIDENT"
    UNDERCONFIDENT = "UNDERCONFIDENT"
    CALIBRATED = "CALIBRATED"
    EMPTY = "EMPTY"


@dataclass(frozen=True, slots=True)
class Prediction:
    """One valuation with its stated confidence and what actually happened."""

    claimed_confidence: float
    inside_interval: bool
    abs_pct_error: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.claimed_confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.claimed_confidence}")
        if self.abs_pct_error < 0:
            raise ValueError("absolute percentage error cannot be negative")

    def hit(self, criterion: HitCriterion, tolerance: float = DEFAULT_ERROR_TOLERANCE) -> bool:
        if criterion is HitCriterion.INTERVAL:
            return self.inside_interval
        return self.abs_pct_error <= tolerance


@dataclass(frozen=True, slots=True)
class ReliabilityBucket:
    """One confidence bucket: what it claimed, what it delivered."""

    lower: float
    upper: float
    count: int
    mean_claimed: float | None
    realised_rate: float | None
    mean_abs_pct_error: float | None

    @property
    def label(self) -> str:
        return f"{self.lower:.0%}-{self.upper:.0%}"

    @property
    def gap(self) -> float | None:
        """Realised minus claimed. Negative means over-confident."""
        if self.mean_claimed is None or self.realised_rate is None:
            return None
        return self.realised_rate - self.mean_claimed

    @property
    def verdict(self) -> CalibrationVerdict:
        gap = self.gap
        if gap is None:
            return CalibrationVerdict.EMPTY
        if abs(gap) <= CALIBRATION_TOLERANCE:
            return CalibrationVerdict.CALIBRATED
        return CalibrationVerdict.UNDERCONFIDENT if gap > 0 else CalibrationVerdict.OVERCONFIDENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "lower": self.lower,
            "upper": self.upper,
            "count": self.count,
            "mean_claimed": _round(self.mean_claimed),
            "realised_rate": _round(self.realised_rate),
            "gap": _round(self.gap),
            "mean_abs_pct_error": _round(self.mean_abs_pct_error),
            "verdict": self.verdict.value,
        }


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def reliability_curve(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
    edges: Sequence[float] = DEFAULT_BUCKET_EDGES,
) -> tuple[ReliabilityBucket, ...]:
    """Bucket by stated confidence and compare claim against outcome.

    Empty buckets are retained: "we never issued a valuation above 80%
    confidence" is itself a finding, and dropping the row hides it.
    """
    if len(edges) < 2:
        raise ValueError("need at least two bucket edges")

    buckets: list[ReliabilityBucket] = []
    for i in range(len(edges) - 1):
        lower, upper = edges[i], edges[i + 1]
        last = i == len(edges) - 2
        members = [
            p
            for p in predictions
            if lower <= p.claimed_confidence < upper or (last and p.claimed_confidence == upper)
        ]
        if not members:
            buckets.append(ReliabilityBucket(lower, upper, 0, None, None, None))
            continue
        buckets.append(
            ReliabilityBucket(
                lower=lower,
                upper=upper,
                count=len(members),
                mean_claimed=statistics.fmean([p.claimed_confidence for p in members]),
                realised_rate=sum(1 for p in members if p.hit(criterion, tolerance)) / len(members),
                mean_abs_pct_error=statistics.fmean([p.abs_pct_error for p in members]),
            )
        )
    return tuple(buckets)


def brier_score(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
) -> float:
    """Mean squared error of the confidence read as a probability. Lower is
    better; 0.25 is what a constant 50% guess scores."""
    if not predictions:
        raise ValueError("cannot score no predictions")
    return statistics.fmean(
        [
            (p.claimed_confidence - (1.0 if p.hit(criterion, tolerance) else 0.0)) ** 2
            for p in predictions
        ]
    )


def base_rate(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
) -> float:
    if not predictions:
        raise ValueError("cannot compute a base rate of no predictions")
    return sum(1 for p in predictions if p.hit(criterion, tolerance)) / len(predictions)


def brier_skill_score(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
) -> float | None:
    """Skill against the climatological baseline "always predict the base rate".

    The blunt question: does this confidence score carry information a single
    constant would not? <= 0 means no, and a score with no skill should not be
    displayed as a percentage.
    """
    if not predictions:
        return None
    rate = base_rate(predictions, criterion=criterion, tolerance=tolerance)
    reference = statistics.fmean(
        [(rate - (1.0 if p.hit(criterion, tolerance) else 0.0)) ** 2 for p in predictions]
    )
    if reference <= 0:
        # Every case had the same outcome; skill is undefined rather than perfect.
        return None
    return 1.0 - brier_score(predictions, criterion=criterion, tolerance=tolerance) / reference


def expected_calibration_error(buckets: Sequence[ReliabilityBucket]) -> float | None:
    """Count-weighted mean absolute gap across non-empty buckets."""
    populated = [b for b in buckets if b.count and b.gap is not None]
    total = sum(b.count for b in populated)
    if not total:
        return None
    return sum(b.count * abs(b.gap or 0.0) for b in populated) / total


def maximum_calibration_error(buckets: Sequence[ReliabilityBucket]) -> float | None:
    """The worst bucket. An average can hide one badly broken band."""
    gaps = [abs(b.gap) for b in buckets if b.count and b.gap is not None]
    return max(gaps) if gaps else None


def discrimination_auc(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
) -> float | None:
    """Probability a hit is ranked above a miss (ties count half).

    0.5 is coin-flip ordering. This separates "wrong level" from "no signal",
    which is the difference between a recalibration and a redesign.
    """
    hits = [p.claimed_confidence for p in predictions if p.hit(criterion, tolerance)]
    misses = [p.claimed_confidence for p in predictions if not p.hit(criterion, tolerance)]
    if not hits or not misses:
        return None
    wins = 0.0
    for h in hits:
        for m in misses:
            if h > m:
                wins += 1.0
            elif h == m:
                wins += 0.5
    return wins / (len(hits) * len(misses))


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, ties shared."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def error_rank_correlation(predictions: Sequence[Prediction]) -> float | None:
    """Spearman correlation between stated confidence and absolute error.

    Should be *negative*: more confidence, less error. A value at or above
    zero means the confidence figure carries no usable information about how
    wrong the valuation is, which is the finding that matters most.
    """
    if len(predictions) < 3:
        return None
    conf = _ranks([p.claimed_confidence for p in predictions])
    err = _ranks([p.abs_pct_error for p in predictions])
    n = len(predictions)
    mean_c = statistics.fmean(conf)
    mean_e = statistics.fmean(err)
    cov = sum((conf[i] - mean_c) * (err[i] - mean_e) for i in range(n))
    var_c = sum((c - mean_c) ** 2 for c in conf)
    var_e = sum((e - mean_e) ** 2 for e in err)
    if var_c <= 0 or var_e <= 0:
        return None
    return float(cov / (var_c**0.5 * var_e**0.5))


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Everything needed to decide whether the confidence figure may be shown."""

    criterion: HitCriterion
    tolerance: float
    count: int
    buckets: tuple[ReliabilityBucket, ...]
    mean_claimed: float | None
    realised_rate: float | None
    brier: float | None
    brier_skill: float | None
    ece: float | None
    mce: float | None
    auc: float | None
    error_correlation: float | None
    evidence_is_synthetic: bool
    method_version: str = METHOD_VERSION

    @property
    def overall_gap(self) -> float | None:
        if self.mean_claimed is None or self.realised_rate is None:
            return None
        return self.realised_rate - self.mean_claimed

    @property
    def verdict(self) -> CalibrationVerdict:
        gap = self.overall_gap
        if gap is None:
            return CalibrationVerdict.EMPTY
        if abs(gap) <= CALIBRATION_TOLERANCE:
            return CalibrationVerdict.CALIBRATED
        return CalibrationVerdict.UNDERCONFIDENT if gap > 0 else CalibrationVerdict.OVERCONFIDENT

    @property
    def miscalibrated_buckets(self) -> tuple[ReliabilityBucket, ...]:
        return tuple(
            b
            for b in self.buckets
            if b.verdict in {CalibrationVerdict.OVERCONFIDENT, CalibrationVerdict.UNDERCONFIDENT}
        )

    @property
    def has_skill(self) -> bool | None:
        """Whether the score beats a constant. `None` when undecidable."""
        return None if self.brier_skill is None else self.brier_skill > 0.0

    @property
    def finding(self) -> str:
        """One sentence a product owner can act on."""
        if self.count == 0:
            return "No valued cases: calibration cannot be assessed."
        gap = self.overall_gap
        if gap is None:
            return "Calibration cannot be assessed from these cases."
        direction = "over-states" if gap < 0 else "under-states"
        skill = (
            "carries no skill over a constant"
            if self.has_skill is False
            else "carries some skill over a constant"
            if self.has_skill
            else "has undecidable skill"
        )
        event = (
            "band contains the realised price"
            if self.criterion is HitCriterion.INTERVAL
            else f"estimate lands within {self.tolerance:.0%}"
        )
        return (
            f"Stated confidence averages {self.mean_claimed:.0%} while the "
            f"{event} {self.realised_rate:.0%} of the time: the score "
            f"{direction} accuracy by {abs(gap):.0%} points and {skill}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_version": self.method_version,
            "evidence_is_synthetic": self.evidence_is_synthetic,
            "criterion": self.criterion.value,
            "tolerance": self.tolerance,
            "count": self.count,
            "mean_claimed": _round(self.mean_claimed),
            "realised_rate": _round(self.realised_rate),
            "gap": _round(self.overall_gap),
            "verdict": self.verdict.value,
            "brier": _round(self.brier),
            "brier_skill": _round(self.brier_skill),
            "expected_calibration_error": _round(self.ece),
            "maximum_calibration_error": _round(self.mce),
            "discrimination_auc": _round(self.auc),
            "error_rank_correlation": _round(self.error_correlation),
            "has_skill": self.has_skill,
            "finding": self.finding,
            "buckets": [b.to_dict() for b in self.buckets],
        }


def build_report(
    predictions: Sequence[Prediction],
    *,
    criterion: HitCriterion = HitCriterion.INTERVAL,
    tolerance: float = DEFAULT_ERROR_TOLERANCE,
    edges: Sequence[float] = DEFAULT_BUCKET_EDGES,
    evidence_is_synthetic: bool,
) -> CalibrationReport:
    """Assemble the calibration view of a set of predictions."""
    buckets = reliability_curve(predictions, criterion=criterion, tolerance=tolerance, edges=edges)
    if not predictions:
        return CalibrationReport(
            criterion=criterion,
            tolerance=tolerance,
            count=0,
            buckets=buckets,
            mean_claimed=None,
            realised_rate=None,
            brier=None,
            brier_skill=None,
            ece=None,
            mce=None,
            auc=None,
            error_correlation=None,
            evidence_is_synthetic=evidence_is_synthetic,
        )
    return CalibrationReport(
        criterion=criterion,
        tolerance=tolerance,
        count=len(predictions),
        buckets=buckets,
        mean_claimed=statistics.fmean([p.claimed_confidence for p in predictions]),
        realised_rate=base_rate(predictions, criterion=criterion, tolerance=tolerance),
        brier=brier_score(predictions, criterion=criterion, tolerance=tolerance),
        brier_skill=brier_skill_score(predictions, criterion=criterion, tolerance=tolerance),
        ece=expected_calibration_error(buckets),
        mce=maximum_calibration_error(buckets),
        auc=discrimination_auc(predictions, criterion=criterion, tolerance=tolerance),
        error_correlation=error_rank_correlation(predictions),
        evidence_is_synthetic=evidence_is_synthetic,
    )
