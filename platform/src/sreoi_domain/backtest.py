"""Back-testing the valuation engine against realised sale prices.

Specified by docs/architecture/valuation-and-scoring.md section 7 and backlog
E4.7. Pure and deterministic: this module holds the arithmetic, the pipeline
module of the same name supplies the evidence.

**What a back-test on this corpus does and does not measure.** The transaction
corpus is generated fixture data (`sreoi_pipeline/seed.py`, registered under a
source flagged `is_synthetic=True`). Re-valuing a generated sale against other
generated sales measures the engine's *internal consistency* -- whether the
estimator recovers a price the generator drew from the same distribution --
and nothing whatever about accuracy in the Saudi market. Publishing an error
figure from it without that caveat attached would be the single most
misleading thing this product could do, so `BacktestReport` carries the flag
and the sentence as data, not as prose someone can forget to copy.

Two design points worth stating because they are easy to get wrong:

* **Coverage outranks point error.** The product publishes a band, so the
  question that matters is how often the realised price lands inside it. A
  band that misses 40% of the time is a broken product even at 5% median
  error; the spec is explicit about this ordering.
* **Segments, not just an average.** An overall figure hides the districts and
  area bands where the engine is weak. Every metric is therefore reported per
  district, per area band and per comparable count.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

METHOD_VERSION = "backtest-v1"

# Targets from the specification (section 7). Coverage is the binding one.
TARGET_MEDIAN_ABS_PCT_ERROR = 0.12
TARGET_INTERVAL_COVERAGE = 0.70

# Coverage alone can be gamed by widening the band, so it is paired with a
# width ceiling. This threshold is not in the specification -- it is derived
# from what the product is for. The discount thresholds in section 5.1 award
# 50/100 at a 10% discount and 80/100 at 20%, so a band spanning more than
# +/-15% of value cannot distinguish "20% below market" from "fairly priced",
# and a coverage figure earned by such a band tells the user nothing.
MAX_USEFUL_INTERVAL_WIDTH_PCT = 0.30

# Segment definitions. Reported so weak segments are visible rather than
# averaged away by strong ones.
AREA_BANDS: tuple[tuple[str, float, float], ...] = (
    ("area <110 sqm", 0.0, 110.0),
    ("area 110-140 sqm", 110.0, 140.0),
    ("area 140-175 sqm", 140.0, 175.0),
    ("area >=175 sqm", 175.0, math.inf),
)
COMPARABLE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("comps 0-7", 0, 8),
    ("comps 8-15", 8, 16),
    ("comps 16-31", 16, 32),
    ("comps >=32", 32, 1 << 30),
)

SYNTHETIC_CAVEAT = (
    "SYNTHETIC EVIDENCE. The comparable corpus is generated fixture data, not "
    "registered Saudi sales. These figures measure the engine's internal "
    "consistency only and say nothing about accuracy in the real market."
)
REAL_EVIDENCE_NOTE = (
    "Evidence is drawn from non-synthetic sources; figures measure accuracy "
    "against realised prices as recorded by those sources."
)


class Verdict(StrEnum):
    """Whether a metric meets its specified target."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_ASSESSED = "NOT_ASSESSED"


class LeakageError(Exception):
    """Evidence dated at or after the as-of date reached the valuation.

    A back-test that can see the future is worthless, so this is an exception
    rather than a warning: the harness refuses to produce a number at all.
    """


@dataclass(frozen=True, slots=True)
class HeldOutCase:
    """A realised sale used as a pseudo-subject.

    `as_of` is the date the valuation is performed for, and is the sale date
    itself: we ask what the engine would have said about this property on the
    day it changed hands, knowing only what was already registered.
    """

    transaction_id: UUID
    as_of: date
    realised_price: float
    area_sqm: float
    district: str | None = None

    def __post_init__(self) -> None:
        if self.realised_price <= 0:
            raise ValueError(f"case {self.transaction_id} has non-positive realised price")
        if self.area_sqm <= 0:
            raise ValueError(f"case {self.transaction_id} has non-positive area")


@dataclass(frozen=True, slots=True)
class CasePrediction:
    """The engine produced a value for a held-out sale."""

    case: HeldOutCase
    predicted_base: float
    predicted_low: float
    predicted_high: float
    confidence: float
    comparable_count: int
    effective_n: float

    @property
    def signed_pct_error(self) -> float:
        """Positive means the engine over-valued. Sign is reported: a
        systematic bias is a different defect from noise and needs a different
        fix."""
        return (self.predicted_base - self.case.realised_price) / self.case.realised_price

    @property
    def abs_pct_error(self) -> float:
        return abs(self.signed_pct_error)

    @property
    def abs_error(self) -> float:
        return abs(self.predicted_base - self.case.realised_price)

    @property
    def inside_interval(self) -> bool:
        return self.predicted_low <= self.case.realised_price <= self.predicted_high

    @property
    def interval_width_pct(self) -> float:
        """Coverage without width is meaningless -- an infinitely wide band
        covers everything -- so the two are always reported together."""
        return (self.predicted_high - self.predicted_low) / self.case.realised_price


@dataclass(frozen=True, slots=True)
class CaseRefusal:
    """The engine refused to value a held-out sale.

    A refusal is a correct outcome, not a failure, and it is counted and
    reported rather than dropped: silently removing refusals from the
    denominator would flatter every other metric on this page.
    """

    case: HeldOutCase
    reason: str
    comparable_count: int
    effective_n: float


CaseOutcome = CasePrediction | CaseRefusal


def check_no_leakage(
    as_of: date,
    evidence: Sequence[tuple[UUID, date]],
    *,
    subject_id: UUID | None = None,
) -> None:
    """Refuse evidence the engine could not have had on `as_of`.

    Only transactions strictly before the as-of date are admissible, and the
    held-out sale itself is never admissible at any date. Enforced here rather
    than left to the query so that a future change to the selection SQL cannot
    reintroduce leakage unnoticed.
    """
    if subject_id is not None:
        for eid, _ in evidence:
            if eid == subject_id:
                raise LeakageError(
                    f"the held-out sale {subject_id} appears in its own evidence set"
                )
    future = sorted((eid, when) for eid, when in evidence if when >= as_of)
    if future:
        shown = ", ".join(f"{eid} on {when.isoformat()}" for eid, when in future[:5])
        raise LeakageError(
            f"{len(future)} comparable(s) dated on or after the as-of date "
            f"{as_of.isoformat()}: {shown}"
        )


def area_band(area_sqm: float) -> str:
    for label, lower, upper in AREA_BANDS:
        if lower <= area_sqm < upper:
            return label
    return AREA_BANDS[-1][0]


def comparable_band(count: int) -> str:
    for label, lower, upper in COMPARABLE_BANDS:
        if lower <= count < upper:
            return label
    return COMPARABLE_BANDS[-1][0]


def district_key(outcome: CaseOutcome) -> str:
    return outcome.case.district or "district unknown"


def area_key(outcome: CaseOutcome) -> str:
    return area_band(outcome.case.area_sqm)


def comparable_key(outcome: CaseOutcome) -> str:
    return comparable_band(outcome.comparable_count)


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile on a sorted copy. Small n is the norm
    here, so the exact convention matters less than being stated."""
    if not values:
        raise ValueError("cannot take a quantile of no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (pos - lo) * (ordered[hi] - ordered[lo])


@dataclass(frozen=True, slots=True)
class ErrorMetrics:
    """One segment's results. `None` where the segment has no valued case."""

    label: str
    cases: int
    valued: int
    refused: int
    median_abs_pct_error: float | None
    mean_abs_pct_error: float | None
    p90_abs_pct_error: float | None
    mean_abs_error_sar: float | None
    median_signed_pct_error: float | None
    within_10_pct: float | None
    within_20_pct: float | None
    interval_coverage: float | None
    median_interval_width_pct: float | None
    mean_confidence: float | None

    @property
    def refusal_rate(self) -> float:
        return self.refused / self.cases if self.cases else 0.0

    @property
    def point_error_verdict(self) -> Verdict:
        if self.median_abs_pct_error is None:
            return Verdict.NOT_ASSESSED
        return (
            Verdict.PASS
            if self.median_abs_pct_error <= TARGET_MEDIAN_ABS_PCT_ERROR
            else Verdict.FAIL
        )

    @property
    def coverage_verdict(self) -> Verdict:
        if self.interval_coverage is None:
            return Verdict.NOT_ASSESSED
        return Verdict.PASS if self.interval_coverage >= TARGET_INTERVAL_COVERAGE else Verdict.FAIL

    @property
    def width_verdict(self) -> Verdict:
        """Whether the band is narrow enough for its coverage to mean anything.

        Reported beside coverage rather than buried, because coverage is
        trivially bought with width and a band this product cannot act on is a
        failure however often it is technically correct.
        """
        if self.median_interval_width_pct is None:
            return Verdict.NOT_ASSESSED
        return (
            Verdict.PASS
            if self.median_interval_width_pct <= MAX_USEFUL_INTERVAL_WIDTH_PCT
            else Verdict.FAIL
        )

    @property
    def interval_is_uninformative(self) -> bool:
        """Coverage met, but only by a band too wide to support a decision."""
        return self.coverage_verdict is Verdict.PASS and self.width_verdict is Verdict.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cases": self.cases,
            "valued": self.valued,
            "refused": self.refused,
            "refusal_rate": round(self.refusal_rate, 4),
            "median_abs_pct_error": _round(self.median_abs_pct_error),
            "mean_abs_pct_error": _round(self.mean_abs_pct_error),
            "p90_abs_pct_error": _round(self.p90_abs_pct_error),
            "mean_abs_error_sar": None
            if self.mean_abs_error_sar is None
            else round(self.mean_abs_error_sar, 2),
            "median_signed_pct_error": _round(self.median_signed_pct_error),
            "within_10_pct": _round(self.within_10_pct),
            "within_20_pct": _round(self.within_20_pct),
            "interval_coverage": _round(self.interval_coverage),
            "median_interval_width_pct": _round(self.median_interval_width_pct),
            "mean_confidence": _round(self.mean_confidence),
            "point_error_verdict": self.point_error_verdict.value,
            "coverage_verdict": self.coverage_verdict.value,
            "width_verdict": self.width_verdict.value,
            "interval_is_uninformative": self.interval_is_uninformative,
            "targets": {
                "median_abs_pct_error": TARGET_MEDIAN_ABS_PCT_ERROR,
                "interval_coverage": TARGET_INTERVAL_COVERAGE,
                "max_interval_width_pct": MAX_USEFUL_INTERVAL_WIDTH_PCT,
            },
        }


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def summarise(label: str, outcomes: Iterable[CaseOutcome]) -> ErrorMetrics:
    """Reduce one segment's outcomes to its metrics."""
    items = list(outcomes)
    valued = [o for o in items if isinstance(o, CasePrediction)]
    refused = [o for o in items if isinstance(o, CaseRefusal)]

    if not valued:
        return ErrorMetrics(
            label=label,
            cases=len(items),
            valued=0,
            refused=len(refused),
            median_abs_pct_error=None,
            mean_abs_pct_error=None,
            p90_abs_pct_error=None,
            mean_abs_error_sar=None,
            median_signed_pct_error=None,
            within_10_pct=None,
            within_20_pct=None,
            interval_coverage=None,
            median_interval_width_pct=None,
            mean_confidence=None,
        )

    abs_pct = [o.abs_pct_error for o in valued]
    signed = [o.signed_pct_error for o in valued]
    return ErrorMetrics(
        label=label,
        cases=len(items),
        valued=len(valued),
        refused=len(refused),
        median_abs_pct_error=statistics.median(abs_pct),
        mean_abs_pct_error=statistics.fmean(abs_pct),
        p90_abs_pct_error=_quantile(abs_pct, 0.90),
        mean_abs_error_sar=statistics.fmean([o.abs_error for o in valued]),
        median_signed_pct_error=statistics.median(signed),
        within_10_pct=sum(1 for e in abs_pct if e <= 0.10) / len(abs_pct),
        within_20_pct=sum(1 for e in abs_pct if e <= 0.20) / len(abs_pct),
        interval_coverage=sum(1 for o in valued if o.inside_interval) / len(valued),
        median_interval_width_pct=statistics.median([o.interval_width_pct for o in valued]),
        mean_confidence=statistics.fmean([o.confidence for o in valued]),
    )


def segment(
    outcomes: Iterable[CaseOutcome],
    key: Callable[[CaseOutcome], str],
    *,
    min_cases: int = 1,
) -> tuple[ErrorMetrics, ...]:
    """Group outcomes and summarise each group, ordered by label."""
    groups: dict[str, list[CaseOutcome]] = {}
    for outcome in outcomes:
        groups.setdefault(key(outcome), []).append(outcome)
    return tuple(
        summarise(label, members)
        for label, members in sorted(groups.items())
        if len(members) >= min_cases
    )


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """The complete result. `evidence_is_synthetic` travels with the numbers."""

    overall: ErrorMetrics
    by_district: tuple[ErrorMetrics, ...]
    by_area_band: tuple[ErrorMetrics, ...]
    by_comparable_band: tuple[ErrorMetrics, ...]
    evidence_is_synthetic: bool
    refusal_reasons: tuple[tuple[str, int], ...] = ()
    method_version: str = METHOD_VERSION

    @property
    def caveat(self) -> str:
        return SYNTHETIC_CAVEAT if self.evidence_is_synthetic else REAL_EVIDENCE_NOTE

    @property
    def segments(self) -> tuple[ErrorMetrics, ...]:
        return (*self.by_district, *self.by_area_band, *self.by_comparable_band)

    @property
    def weakest_segments(self) -> tuple[ErrorMetrics, ...]:
        """Segments failing coverage, worst first. The point of the exercise."""
        failing = [s for s in self.segments if s.coverage_verdict is Verdict.FAIL]
        return tuple(sorted(failing, key=lambda s: s.interval_coverage or 0.0))

    @property
    def uninformative_segments(self) -> tuple[ErrorMetrics, ...]:
        """Segments that pass coverage only because their band is too wide."""
        offenders = [s for s in self.segments if s.interval_is_uninformative]
        return tuple(sorted(offenders, key=lambda s: -(s.median_interval_width_pct or 0.0)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "method_version": self.method_version,
            # Deliberately first, and a machine-readable field rather than only
            # a sentence: a consumer must not be able to read the metrics
            # without also receiving this.
            "evidence_is_synthetic": self.evidence_is_synthetic,
            "measures": "internal_consistency" if self.evidence_is_synthetic else "accuracy",
            "caveat": self.caveat,
            "overall": self.overall.to_dict(),
            "by_district": [s.to_dict() for s in self.by_district],
            "by_area_band": [s.to_dict() for s in self.by_area_band],
            "by_comparable_band": [s.to_dict() for s in self.by_comparable_band],
            "refusal_reasons": [{"reason": r, "count": c} for r, c in self.refusal_reasons],
            "weakest_segments": [s.label for s in self.weakest_segments],
            "uninformative_segments": [s.label for s in self.uninformative_segments],
        }


def _refusal_reasons(outcomes: Sequence[CaseOutcome]) -> tuple[tuple[str, int], ...]:
    """Group refusals by their cause, not by their exact message.

    The messages embed counts, so grouping raw text would produce one bucket
    per case and hide the pattern.
    """
    counts: dict[str, int] = {}
    for outcome in outcomes:
        if isinstance(outcome, CaseRefusal):
            bucket = (
                "no comparables at all"
                if outcome.comparable_count == 0
                else f"effective n below minimum ({comparable_band(outcome.comparable_count)})"
            )
            counts[bucket] = counts.get(bucket, 0) + 1
    return tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def build_report(
    outcomes: Sequence[CaseOutcome],
    *,
    evidence_is_synthetic: bool,
    min_segment_cases: int = 3,
) -> BacktestReport:
    """Assemble the overall and per-segment view of a back-test."""
    return BacktestReport(
        overall=summarise("overall", outcomes),
        by_district=segment(outcomes, district_key, min_cases=min_segment_cases),
        by_area_band=segment(outcomes, area_key, min_cases=min_segment_cases),
        by_comparable_band=segment(outcomes, comparable_key, min_cases=min_segment_cases),
        evidence_is_synthetic=evidence_is_synthetic,
        refusal_reasons=_refusal_reasons(outcomes),
    )
