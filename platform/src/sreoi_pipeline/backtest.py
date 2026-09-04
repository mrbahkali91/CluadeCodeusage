"""Run the back-testing harness against the database (backlog E4.7).

    python -m sreoi_pipeline.backtest --sample 150 --json

Hold out registered sales as pseudo-subjects: for a sale at date `T` with a
known price, re-run the valuation using only evidence dated strictly before
`T`, then compare the estimate and the published band against what the
property actually fetched.

**Leakage is the failure that makes a back-test worthless**, so it is blocked
in two places rather than one. The evidence query filters
`transacted_on < as_of` and excludes the held-out row by id; the pure
`check_no_leakage` guard then re-checks the rows that came back before any of
them reaches the estimator. The query alone would be enough today, but a
future change to the selection SQL would silently invalidate every number this
module prints, and the second check is what makes that impossible.

Note the difference from `ComparableRepository.find`, which the production
evaluation path uses: that query is inclusive (`transacted_on <= as_of`),
which is correct for valuing a live opportunity today and wrong for a
back-test, because it would admit the held-out sale itself. The selection
constants are imported from the repository so the harness stays in step with
production when they change.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography, Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from sreoi_domain.backtest import (
    BacktestReport,
    CaseOutcome,
    CasePrediction,
    CaseRefusal,
    HeldOutCase,
    build_report,
    check_no_leakage,
)
from sreoi_domain.calibration import (
    CalibrationReport,
    HitCriterion,
    Prediction,
)
from sreoi_domain.calibration import build_report as build_calibration
from sreoi_domain.valuation import (
    METHOD_VERSION as VALUATION_METHOD_VERSION,
)
from sreoi_domain.valuation import (
    MIN_COMPARABLES,
    Comparable,
    IndexTier,
    InsufficientComparablesError,
    SubjectProperty,
    value_property,
)
from sreoi_persistence.db import session_scope
from sreoi_persistence.models import District, PriceIndexPoint, Source, Transaction
from sreoi_persistence.models_quality import BacktestResult, BacktestRun
from sreoi_persistence.repositories import (
    AREA_LOWER_FACTOR,
    AREA_UPPER_FACTOR,
    LOOKBACK_MONTHS,
    RADIUS_STEPS_M,
)
from sreoi_pipeline.evaluate import FALLBACK_SECTOR, SECTOR_BY_CLASS

DEFAULT_SAMPLE = 150
DEFAULT_SEED = 20260904
# A sale in the first months of the corpus has almost no prior evidence, so
# refusing it says more about the corpus window than about the engine. Cases
# below this much history are excluded from the sample and the exclusion is
# reported, rather than being allowed to inflate the refusal rate.
DEFAULT_MIN_HISTORY_DAYS = 180


@dataclass(frozen=True, slots=True)
class BacktestOutcome:
    """Everything one harness run produced."""

    report: BacktestReport
    interval_calibration: CalibrationReport
    tolerance_calibration: CalibrationReport
    outcomes: tuple[CaseOutcome, ...]
    corpus_count: int
    eligible_count: int
    requested_sample: int
    seed: int
    min_history_days: int
    duration_ms: float
    run_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id) if self.run_id else None,
            # First field after identity, deliberately: no consumer of this
            # payload can read a metric without also receiving the provenance
            # of the evidence it was measured on.
            "evidence_is_synthetic": self.report.evidence_is_synthetic,
            "caveat": self.report.caveat,
            "sampling": {
                "corpus_count": self.corpus_count,
                "eligible_count": self.eligible_count,
                "requested_sample": self.requested_sample,
                "held_out": self.report.overall.cases,
                "excluded_insufficient_history": self.corpus_count - self.eligible_count,
                "seed": self.seed,
                "min_history_days": self.min_history_days,
            },
            "valuation_method_version": VALUATION_METHOD_VERSION,
            "duration_ms": round(self.duration_ms, 1),
            "backtest": self.report.to_dict(),
            "calibration_interval": self.interval_calibration.to_dict(),
            "calibration_point_error": self.tolerance_calibration.to_dict(),
        }


def _coordinates(session: Session, transaction_id: UUID) -> tuple[float, float]:
    geom = cast(Transaction.location, Geometry)
    row = session.execute(
        select(func.ST_X(geom), func.ST_Y(geom)).where(Transaction.id == transaction_id)
    ).one()
    return float(row[0]), float(row[1])


def find_evidence_before(
    session: Session,
    *,
    subject_id: UUID,
    longitude: float,
    latitude: float,
    property_class: str,
    area_sqm: float,
    as_of: date,
    min_comparables: int = MIN_COMPARABLES,
) -> tuple[list[Comparable], float]:
    """Production comparable selection, with the back-test's leakage guard.

    Mirrors `ComparableRepository.find` -- same expanding radius steps, same
    lookback, same area band -- with two deliberate differences: the date
    filter is strict, and the held-out sale is excluded by id.
    """
    subject = cast(func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326), Geography)
    cutoff = as_of - timedelta(days=int(LOOKBACK_MONTHS * 30.44))

    found: list[Comparable] = []
    used_radius = RADIUS_STEPS_M[-1]

    for radius in RADIUS_STEPS_M:
        stmt = (
            select(
                Transaction,
                func.ST_Distance(Transaction.location, subject).label("distance_m"),
            )
            .where(
                Transaction.id != subject_id,
                Transaction.property_class == property_class,
                Transaction.transacted_on >= cutoff,
                # Strict: a sale on the as-of date was not yet knowable, and
                # the held-out sale is one of them.
                Transaction.transacted_on < as_of,
                Transaction.area_sqm >= area_sqm * AREA_LOWER_FACTOR,
                Transaction.area_sqm <= area_sqm * AREA_UPPER_FACTOR,
                func.ST_DWithin(Transaction.location, subject, radius),
            )
            .order_by("distance_m")
            .limit(200)
        )
        rows = session.execute(stmt).all()
        if len(rows) >= min_comparables or radius == RADIUS_STEPS_M[-1]:
            found = [
                Comparable(
                    id=txn.id,
                    price=Decimal(txn.price),
                    area_sqm=float(txn.area_sqm),
                    transacted_on=txn.transacted_on,
                    distance_m=float(distance),
                    property_class=txn.property_class,
                    district_id=txn.district_id,
                    project_id=txn.project_id,
                    build_year=txn.build_year,
                    floor=txn.floor,
                )
                for txn, distance in rows
            ]
            used_radius = radius
            break

    return found, used_radius


def _index_series(session: Session, property_class: str) -> dict[str, float]:
    sector = SECTOR_BY_CLASS.get(property_class, FALLBACK_SECTOR)
    rows = list(
        session.scalars(
            select(PriceIndexPoint).where(
                PriceIndexPoint.sector == sector, PriceIndexPoint.tier == "NATIONAL"
            )
        )
    )
    if not rows:
        rows = list(
            session.scalars(
                select(PriceIndexPoint).where(
                    PriceIndexPoint.sector == FALLBACK_SECTOR,
                    PriceIndexPoint.tier == "NATIONAL",
                )
            )
        )
    return {row.period: float(row.value) for row in rows}


def _index_at(series: dict[str, float], when: date) -> float | None:
    """Index value for a month, falling back to the nearest prior period.

    The published series is quarterly, so most months are absent.
    """
    if not series:
        return None
    period = f"{when.year:04d}-{when.month:02d}"
    if period in series:
        return series[period]
    prior = sorted(p for p in series if p <= period)
    if prior:
        return series[prior[-1]]
    return series[min(series)]


def _subject_completeness(txn: Transaction) -> float:
    """Completeness over the fields a transaction row actually carries.

    Production computes this over five property fields including bedrooms,
    which transactions do not record, so this figure is not directly
    comparable with a live valuation's. It is stated rather than quietly
    assumed to be 1.0, which would inflate every confidence in the run.
    """
    fields = [txn.district_id, txn.project_id, txn.build_year, txn.floor]
    return sum(1 for f in fields if f is not None) / len(fields)


def _evidence_is_synthetic(session: Session, transaction_ids: list[UUID]) -> bool:
    """True when any evidence used came from a source flagged synthetic."""
    if not transaction_ids:
        return bool(session.scalar(select(Source.id).where(Source.is_synthetic.is_(True))))
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Transaction)
            .join(Source, Source.id == Transaction.source_id)
            .where(Transaction.id.in_(transaction_ids), Source.is_synthetic.is_(True))
        )
    )


def select_holdout(
    session: Session,
    *,
    sample_size: int = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
) -> tuple[list[Transaction], int, int]:
    """Deterministically choose held-out sales.

    Returns the sample, the corpus size and the eligible-window size, so the
    report can state what was excluded and why instead of presenting a
    filtered sample as if it were the whole corpus.
    """
    corpus_count = int(session.scalar(select(func.count()).select_from(Transaction)) or 0)
    earliest = session.scalar(select(func.min(Transaction.transacted_on)))
    if earliest is None:
        return [], 0, 0

    window_start = earliest + timedelta(days=min_history_days)
    eligible = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.transacted_on >= window_start)
            .order_by(Transaction.transacted_on, Transaction.id)
        )
    )
    if not eligible:
        return [], corpus_count, 0

    rng = random.Random(seed)
    chosen = eligible if sample_size >= len(eligible) else rng.sample(eligible, sample_size)
    chosen.sort(key=lambda t: (t.transacted_on, str(t.id)))
    return chosen, corpus_count, len(eligible)


def evaluate_case(
    session: Session, txn: Transaction, district_names: dict[UUID, str]
) -> CaseOutcome:
    """Value one held-out sale using only evidence that predates it."""
    as_of = txn.transacted_on
    case = HeldOutCase(
        transaction_id=txn.id,
        as_of=as_of,
        realised_price=float(txn.price),
        area_sqm=float(txn.area_sqm),
        district=district_names.get(txn.district_id) if txn.district_id else None,
    )

    longitude, latitude = _coordinates(session, txn.id)
    comps, _radius = find_evidence_before(
        session,
        subject_id=txn.id,
        longitude=longitude,
        latitude=latitude,
        property_class=txn.property_class,
        area_sqm=float(txn.area_sqm),
        as_of=as_of,
    )

    # Second, independent leakage check. See the module docstring.
    check_no_leakage(as_of, [(c.id, c.transacted_on) for c in comps], subject_id=txn.id)

    series = _index_series(session, txn.property_class)
    # Valuing as of T means indexing forward to T, not to today. Indexing to
    # today would compare a present-day estimate against a historic price and
    # blame the difference on the estimator.
    index_now = _index_at(series, as_of)
    index_tier = IndexTier.NATIONAL if series else IndexTier.NONE
    index_at = {c.id: v for c in comps if (v := _index_at(series, c.transacted_on)) is not None}

    subject = SubjectProperty(
        property_class=txn.property_class,
        area_sqm=float(txn.area_sqm),
        district_id=txn.district_id,
        project_id=txn.project_id,
        build_year=txn.build_year,
        floor=txn.floor,
    )

    try:
        fair_value = value_property(
            subject,
            comps,
            as_of=as_of,
            index_now=index_now,
            index_at=index_at,
            index_tier=index_tier,
            subject_completeness=_subject_completeness(txn),
        )
    except InsufficientComparablesError as exc:
        # A refusal is a correct outcome. It is recorded, counted and reported.
        return CaseRefusal(
            case=case,
            reason=str(exc),
            comparable_count=exc.count,
            effective_n=exc.effective_n,
        )

    return CasePrediction(
        case=case,
        predicted_base=float(fair_value.base),
        predicted_low=float(fair_value.low),
        predicted_high=float(fair_value.high),
        confidence=fair_value.confidence,
        comparable_count=fair_value.comparable_count,
        effective_n=fair_value.effective_n,
    )


def run_backtest(
    session: Session,
    *,
    sample_size: int = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    persist: bool = True,
) -> BacktestOutcome:
    """Execute the harness and, by default, store the run."""
    started = time.perf_counter()
    holdout, corpus_count, eligible_count = select_holdout(
        session, sample_size=sample_size, seed=seed, min_history_days=min_history_days
    )
    district_names = {d.id: d.name_en for d in session.scalars(select(District)).all()}

    outcomes = [evaluate_case(session, txn, district_names) for txn in holdout]
    synthetic = _evidence_is_synthetic(session, [t.id for t in holdout])

    report = build_report(outcomes, evidence_is_synthetic=synthetic)
    predictions = [
        Prediction(
            claimed_confidence=o.confidence,
            inside_interval=o.inside_interval,
            abs_pct_error=o.abs_pct_error,
        )
        for o in outcomes
        if isinstance(o, CasePrediction)
    ]
    interval = build_calibration(
        predictions, criterion=HitCriterion.INTERVAL, evidence_is_synthetic=synthetic
    )
    tolerance = build_calibration(
        predictions, criterion=HitCriterion.TOLERANCE, evidence_is_synthetic=synthetic
    )
    duration_ms = (time.perf_counter() - started) * 1000

    outcome = BacktestOutcome(
        report=report,
        interval_calibration=interval,
        tolerance_calibration=tolerance,
        outcomes=tuple(outcomes),
        corpus_count=corpus_count,
        eligible_count=eligible_count,
        requested_sample=sample_size,
        seed=seed,
        min_history_days=min_history_days,
        duration_ms=duration_ms,
    )
    if not persist:
        return outcome

    return replace(outcome, run_id=persist_run(session, outcome, holdout))


def persist_run(session: Session, outcome: BacktestOutcome, holdout: list[Transaction]) -> UUID:
    """Store the run and every case. Append-only; runs are never updated."""
    overall = outcome.report.overall
    as_ofs = [o.case.as_of for o in outcome.outcomes]
    run = BacktestRun(
        method_version=outcome.report.method_version,
        valuation_method_version=VALUATION_METHOD_VERSION,
        sample_seed=outcome.seed,
        requested_sample=outcome.requested_sample,
        min_history_days=outcome.min_history_days,
        eligible_count=outcome.eligible_count,
        corpus_count=outcome.corpus_count,
        held_out_count=overall.cases,
        refused_count=overall.refused,
        earliest_as_of=min(as_ofs) if as_ofs else None,
        latest_as_of=max(as_ofs) if as_ofs else None,
        evidence_is_synthetic=outcome.report.evidence_is_synthetic,
        caveat=outcome.report.caveat,
        median_abs_pct_error=overall.median_abs_pct_error,
        mean_abs_pct_error=overall.mean_abs_pct_error,
        interval_coverage=overall.interval_coverage,
        median_interval_width_pct=overall.median_interval_width_pct,
        brier_score=outcome.interval_calibration.brier,
        brier_skill=outcome.interval_calibration.brier_skill,
        expected_calibration_error=outcome.interval_calibration.ece,
        point_error_verdict=overall.point_error_verdict.value,
        coverage_verdict=overall.coverage_verdict.value,
        calibration_verdict=outcome.interval_calibration.verdict.value,
        report=outcome.to_dict(),
        duration_ms=outcome.duration_ms,
    )
    session.add(run)
    session.flush()

    districts = {t.id: t.district_id for t in holdout}
    for case_outcome in outcome.outcomes:
        case = case_outcome.case
        row = BacktestResult(
            run_id=run.id,
            transaction_id=case.transaction_id,
            district_id=districts.get(case.transaction_id),
            district_name=case.district,
            as_of=case.as_of,
            realised_price=Decimal(str(round(case.realised_price, 2))),
            area_sqm=case.area_sqm,
            comparable_count=case_outcome.comparable_count,
            effective_n=round(case_outcome.effective_n, 3),
        )
        if isinstance(case_outcome, CaseRefusal):
            row.refused = True
            row.refusal_reason = case_outcome.reason
        else:
            row.refused = False
            row.predicted_base = Decimal(str(round(case_outcome.predicted_base, 2)))
            row.predicted_low = Decimal(str(round(case_outcome.predicted_low, 2)))
            row.predicted_high = Decimal(str(round(case_outcome.predicted_high, 2)))
            row.confidence = round(case_outcome.confidence, 5)
            row.signed_pct_error = round(case_outcome.signed_pct_error, 5)
            row.inside_interval = case_outcome.inside_interval
        session.add(row)
    session.flush()
    return run.id


def latest_run(session: Session) -> BacktestRun | None:
    return session.scalar(select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(1))


def _print_summary(outcome: BacktestOutcome) -> None:
    report = outcome.report
    overall = report.overall
    # The caveat is printed before any metric, never after, so that a
    # truncated copy-paste still carries it.
    print(f"!! {report.caveat}")
    print(
        f"   corpus {outcome.corpus_count}, eligible {outcome.eligible_count}, "
        f"held out {overall.cases} (seed {outcome.seed})"
    )
    print(
        f"   valued {overall.valued}, refused {overall.refused} "
        f"({overall.refusal_rate:.1%}) -- a refusal is a correct outcome"
    )
    if overall.median_abs_pct_error is not None:
        print(
            f"   median |error| {overall.median_abs_pct_error:.2%} "
            f"[{overall.point_error_verdict.value}, target <=12%]"
        )
        print(
            f"   mean   |error| {overall.mean_abs_pct_error:.2%}   "
            f"p90 {overall.p90_abs_pct_error:.2%}"
        )
        print(f"   bias (median signed) {overall.median_signed_pct_error:+.2%}")
    if overall.interval_coverage is not None:
        print(
            f"   interval coverage {overall.interval_coverage:.1%} "
            f"[{overall.coverage_verdict.value}, target >=70%]"
        )
        print(
            f"   median band width {overall.median_interval_width_pct:.1%} of value "
            f"[{overall.width_verdict.value}, ceiling 30%]"
        )
        if overall.interval_is_uninformative:
            print(
                "   !! coverage is met only because the band is too wide to act "
                "on: it cannot separate a 20% discount from fair value"
            )
    print(f"   calibration: {outcome.interval_calibration.finding}")
    for bucket in outcome.interval_calibration.buckets:
        if not bucket.count:
            continue
        print(
            f"     {bucket.label:>9}  n={bucket.count:<4} claimed "
            f"{bucket.mean_claimed:.1%} realised {bucket.realised_rate:.1%} "
            f"({bucket.verdict.value})"
        )
    wide = report.uninformative_segments
    if wide:
        print("   segments whose band is too wide to be informative:")
        for seg in wide[:6]:
            print(
                f"     {seg.label:<22} width {seg.median_interval_width_pct:.1%} "
                f"coverage {seg.interval_coverage:.1%} (n={seg.valued})"
            )
    weak = report.weakest_segments
    if weak:
        print("   segments failing coverage:")
        for seg in weak:
            err = "n/a" if seg.median_abs_pct_error is None else f"{seg.median_abs_pct_error:.1%}"
            print(
                f"     {seg.label:<22} coverage {seg.interval_coverage:.1%} "
                f"median |error| {err} (n={seg.valued})"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sreoi_pipeline.backtest",
        description=(
            "Back-test the valuation engine against realised sale prices. "
            "On a synthetic corpus this measures internal consistency only."
        ),
    )
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--min-history-days", type=int, default=DEFAULT_MIN_HISTORY_DAYS)
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument("--no-persist", action="store_true", help="compute without storing the run")
    args = parser.parse_args(argv)

    with session_scope() as session:
        outcome = run_backtest(
            session,
            sample_size=args.sample,
            seed=args.seed,
            min_history_days=args.min_history_days,
            persist=not args.no_persist,
        )
        payload = outcome.to_dict()

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_summary(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
