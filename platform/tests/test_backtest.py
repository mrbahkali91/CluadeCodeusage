"""The back-testing harness: pure metrics, and the database-backed run.

The leakage tests are the load-bearing ones. Every other figure this module
produces is meaningless if the engine can see the sale it is being asked to
predict, so leakage is asserted from both directions: the guard rejects future
evidence, and the query never returns any.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_domain.backtest import (
    MAX_USEFUL_INTERVAL_WIDTH_PCT,
    TARGET_INTERVAL_COVERAGE,
    TARGET_MEDIAN_ABS_PCT_ERROR,
    CaseOutcome,
    CasePrediction,
    CaseRefusal,
    HeldOutCase,
    LeakageError,
    Verdict,
    area_band,
    build_report,
    check_no_leakage,
    comparable_band,
    segment,
    summarise,
)
from sreoi_persistence.models import Transaction
from sreoi_persistence.models_quality import BacktestResult, BacktestRun
from sreoi_pipeline.backtest import (
    evaluate_case,
    find_evidence_before,
    latest_run,
    run_backtest,
    select_holdout,
)
from tests.conftest import requires_db

AS_OF = date(2026, 6, 1)


def _case(
    *, price: float = 1_000_000.0, area: float = 140.0, district: str | None = "Sidrah"
) -> HeldOutCase:
    return HeldOutCase(
        transaction_id=uuid.uuid4(),
        as_of=AS_OF,
        realised_price=price,
        area_sqm=area,
        district=district,
    )


def _prediction(
    *,
    base: float,
    low: float | None = None,
    high: float | None = None,
    confidence: float = 0.7,
    comps: int = 12,
    realised: float = 1_000_000.0,
    district: str | None = "Sidrah",
    area: float = 140.0,
) -> CasePrediction:
    return CasePrediction(
        case=_case(price=realised, area=area, district=district),
        predicted_base=base,
        predicted_low=low if low is not None else base * 0.9,
        predicted_high=high if high is not None else base * 1.1,
        confidence=confidence,
        comparable_count=comps,
        effective_n=float(comps) / 2,
    )


class TestHeldOutCase:
    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(ValueError, match="non-positive realised price"):
            HeldOutCase(uuid.uuid4(), AS_OF, 0.0, 140.0)

    def test_rejects_non_positive_area(self) -> None:
        with pytest.raises(ValueError, match="non-positive area"):
            HeldOutCase(uuid.uuid4(), AS_OF, 1.0, 0.0)


class TestLeakageGuard:
    """A back-test that can see the future is worthless."""

    def test_accepts_evidence_strictly_before_the_as_of_date(self) -> None:
        check_no_leakage(AS_OF, [(uuid.uuid4(), AS_OF - timedelta(days=1))])

    def test_rejects_evidence_after_the_as_of_date(self) -> None:
        with pytest.raises(LeakageError, match="dated on or after"):
            check_no_leakage(AS_OF, [(uuid.uuid4(), AS_OF + timedelta(days=1))])

    def test_rejects_evidence_on_the_as_of_date(self) -> None:
        """Same-day sales were not knowable on the morning of the valuation,
        and one of them is the held-out sale itself."""
        with pytest.raises(LeakageError, match="dated on or after"):
            check_no_leakage(AS_OF, [(uuid.uuid4(), AS_OF)])

    def test_rejects_the_subject_appearing_in_its_own_evidence(self) -> None:
        subject = uuid.uuid4()
        with pytest.raises(LeakageError, match="its own evidence"):
            check_no_leakage(AS_OF, [(subject, AS_OF - timedelta(days=30))], subject_id=subject)

    def test_names_the_offending_rows(self) -> None:
        bad = uuid.uuid4()
        with pytest.raises(LeakageError) as excinfo:
            check_no_leakage(AS_OF, [(bad, AS_OF)])
        assert str(bad) in str(excinfo.value)


class TestSegmentation:
    def test_area_bands_partition_the_range(self) -> None:
        assert area_band(90.0) == "area <110 sqm"
        assert area_band(110.0) == "area 110-140 sqm"
        assert area_band(174.9) == "area 140-175 sqm"
        assert area_band(400.0) == "area >=175 sqm"

    def test_comparable_bands_partition_the_range(self) -> None:
        assert comparable_band(0) == "comps 0-7"
        assert comparable_band(8) == "comps 8-15"
        assert comparable_band(31) == "comps 16-31"
        assert comparable_band(500) == "comps >=32"

    def test_segments_are_reported_so_weak_ones_are_not_averaged_away(self) -> None:
        outcomes = [
            _prediction(base=1_000_000, district="Good"),
            _prediction(base=1_000_000, district="Good"),
            _prediction(base=1_500_000, district="Bad"),
            _prediction(base=1_500_000, district="Bad"),
        ]
        report = build_report(outcomes, evidence_is_synthetic=True, min_segment_cases=2)
        by_label = {s.label: s for s in report.by_district}
        assert by_label["Good"].median_abs_pct_error == pytest.approx(0.0)
        assert by_label["Bad"].median_abs_pct_error == pytest.approx(0.5)

    def test_small_segments_are_suppressed_rather_than_reported_as_fact(self) -> None:
        outcomes = [_prediction(base=1_000_000, district="Tiny")]
        report = build_report(outcomes, evidence_is_synthetic=True, min_segment_cases=3)
        assert report.by_district == ()

    def test_grouping_uses_the_supplied_key(self) -> None:
        outcomes = [
            _prediction(base=1_000_000, area=100.0),
            _prediction(base=1_000_000, area=200.0),
        ]
        groups = segment(outcomes, lambda o: area_band(o.case.area_sqm))
        assert {g.label for g in groups} == {"area <110 sqm", "area >=175 sqm"}


class TestMetrics:
    def test_signed_error_keeps_its_sign_so_bias_is_visible(self) -> None:
        over = _prediction(base=1_100_000)
        under = _prediction(base=900_000)
        assert over.signed_pct_error == pytest.approx(0.1)
        assert under.signed_pct_error == pytest.approx(-0.1)
        assert under.abs_pct_error == pytest.approx(0.1)

    def test_coverage_counts_the_realised_price_inside_the_band(self) -> None:
        inside = _prediction(base=1_100_000, low=900_000, high=1_300_000)
        outside = _prediction(base=1_400_000, low=1_300_000, high=1_500_000)
        metrics = summarise("s", [inside, outside])
        assert metrics.interval_coverage == pytest.approx(0.5)

    def test_band_endpoints_are_inclusive(self) -> None:
        edge = _prediction(base=1_000_000, low=1_000_000, high=1_200_000)
        assert edge.inside_interval is True

    def test_refusals_are_counted_and_excluded_from_error_metrics(self) -> None:
        """A refusal is a correct outcome; quietly dropping it from the
        denominator would flatter every other figure."""
        outcomes: list[CaseOutcome] = [
            _prediction(base=1_000_000),
            CaseRefusal(case=_case(), reason="insufficient", comparable_count=2, effective_n=1.5),
        ]
        metrics = summarise("s", outcomes)
        assert metrics.cases == 2
        assert metrics.valued == 1
        assert metrics.refused == 1
        assert metrics.refusal_rate == pytest.approx(0.5)
        assert metrics.median_abs_pct_error == pytest.approx(0.0)

    def test_a_segment_of_only_refusals_reports_no_error_rather_than_zero(self) -> None:
        metrics = summarise(
            "s",
            [CaseRefusal(case=_case(), reason="r", comparable_count=1, effective_n=0.9)],
        )
        assert metrics.median_abs_pct_error is None
        assert metrics.point_error_verdict is Verdict.NOT_ASSESSED
        assert metrics.coverage_verdict is Verdict.NOT_ASSESSED

    def test_refusal_reasons_are_grouped_not_listed_verbatim(self) -> None:
        outcomes = [
            CaseRefusal(case=_case(), reason="a", comparable_count=0, effective_n=0.0),
            CaseRefusal(case=_case(), reason="b", comparable_count=0, effective_n=0.0),
        ]
        report = build_report(outcomes, evidence_is_synthetic=True)
        assert report.refusal_reasons == (("no comparables at all", 2),)


class TestVerdicts:
    def test_point_error_target_is_the_specified_twelve_percent(self) -> None:
        assert TARGET_MEDIAN_ABS_PCT_ERROR == 0.12
        just_inside = summarise("s", [_prediction(base=1_120_000)])
        just_outside = summarise("s", [_prediction(base=1_130_000)])
        assert just_inside.point_error_verdict is Verdict.PASS
        assert just_outside.point_error_verdict is Verdict.FAIL

    def test_coverage_target_is_the_specified_seventy_percent(self) -> None:
        assert TARGET_INTERVAL_COVERAGE == 0.70
        outcomes = [_prediction(base=1_000_000, low=900_000, high=1_100_000) for _ in range(7)] + [
            _prediction(base=2_000_000, low=1_900_000, high=2_100_000) for _ in range(3)
        ]
        assert summarise("s", outcomes).coverage_verdict is Verdict.PASS

    def test_a_band_wide_enough_to_guarantee_coverage_fails_on_width(self) -> None:
        """Coverage is trivially bought with width. A band spanning more than
        +/-15% of value cannot separate a 20% discount from fair value, so
        coverage earned that way is reported as uninformative, not as a pass."""
        wide = _prediction(base=1_000_000, low=400_000, high=1_600_000)
        metrics = summarise("s", [wide])
        assert metrics.interval_coverage == 1.0
        assert metrics.coverage_verdict is Verdict.PASS
        assert metrics.median_interval_width_pct is not None
        assert metrics.median_interval_width_pct > MAX_USEFUL_INTERVAL_WIDTH_PCT
        assert metrics.width_verdict is Verdict.FAIL
        assert metrics.interval_is_uninformative is True

    def test_a_narrow_covering_band_is_informative(self) -> None:
        narrow = _prediction(base=1_000_000, low=950_000, high=1_050_000)
        metrics = summarise("s", [narrow])
        assert metrics.width_verdict is Verdict.PASS
        assert metrics.interval_is_uninformative is False

    def test_uninformative_segments_are_surfaced(self) -> None:
        outcomes = [
            _prediction(base=1_000_000, low=400_000, high=1_600_000, district="Wide")
            for _ in range(3)
        ]
        report = build_report(outcomes, evidence_is_synthetic=True)
        assert [s.label for s in report.uninformative_segments]


class TestReportPayload:
    def test_synthetic_caveat_is_a_field_not_only_prose(self) -> None:
        report = build_report([_prediction(base=1_000_000)], evidence_is_synthetic=True)
        payload = report.to_dict()
        assert payload["evidence_is_synthetic"] is True
        assert payload["measures"] == "internal_consistency"
        assert "SYNTHETIC EVIDENCE" in payload["caveat"]

    def test_non_synthetic_evidence_is_labelled_differently(self) -> None:
        report = build_report([_prediction(base=1_000_000)], evidence_is_synthetic=False)
        payload = report.to_dict()
        assert payload["evidence_is_synthetic"] is False
        assert payload["measures"] == "accuracy"
        assert "SYNTHETIC" not in payload["caveat"]


@requires_db
class TestAgainstTheCorpus:
    def test_evidence_query_never_returns_the_subject_or_the_future(self, session: Session) -> None:
        """The query-side half of the leakage guarantee.

        This is the test that fails if someone relaxes the date filter back to
        the inclusive form the production repository uses.
        """
        txn = session.scalars(
            select(Transaction).order_by(Transaction.transacted_on.desc()).limit(1)
        ).one()
        longitude, latitude = 46.85, 24.87
        comps, _radius = find_evidence_before(
            session,
            subject_id=txn.id,
            longitude=longitude,
            latitude=latitude,
            property_class=txn.property_class,
            area_sqm=float(txn.area_sqm),
            as_of=txn.transacted_on,
        )
        assert comps, "the corpus should supply some prior evidence"
        assert all(c.transacted_on < txn.transacted_on for c in comps)
        assert all(c.id != txn.id for c in comps)

    def test_evidence_respects_the_lookback_and_area_band(self, session: Session) -> None:
        txn = session.scalars(
            select(Transaction).order_by(Transaction.transacted_on.desc()).limit(1)
        ).one()
        area = float(txn.area_sqm)
        comps, _ = find_evidence_before(
            session,
            subject_id=txn.id,
            longitude=46.85,
            latitude=24.87,
            property_class=txn.property_class,
            area_sqm=area,
            as_of=txn.transacted_on,
        )
        assert all(0.65 * area <= c.area_sqm <= 1.50 * area for c in comps)
        assert all((txn.transacted_on - c.transacted_on).days <= int(24 * 30.44) for c in comps)

    def test_holdout_selection_is_deterministic(self, session: Session) -> None:
        first, corpus, eligible = select_holdout(session, sample_size=25, seed=11)
        second, _, _ = select_holdout(session, sample_size=25, seed=11)
        assert [t.id for t in first] == [t.id for t in second]
        assert corpus >= eligible >= len(first)

    def test_a_different_seed_selects_a_different_sample(self, session: Session) -> None:
        a, _, _ = select_holdout(session, sample_size=25, seed=11)
        b, _, _ = select_holdout(session, sample_size=25, seed=12)
        assert [t.id for t in a] != [t.id for t in b]

    def test_holdout_excludes_the_thin_start_of_the_corpus(self, session: Session) -> None:
        earliest = session.scalar(select(func.min(Transaction.transacted_on)))
        assert earliest is not None
        chosen, _, _ = select_holdout(session, sample_size=500, min_history_days=180)
        assert chosen
        assert all(t.transacted_on >= earliest + timedelta(days=180) for t in chosen)

    def test_evaluating_one_case_produces_a_prediction_or_a_refusal(self, session: Session) -> None:
        chosen, _, _ = select_holdout(session, sample_size=6, seed=3)
        for txn in chosen:
            outcome = evaluate_case(session, txn, {})
            assert isinstance(outcome, CasePrediction | CaseRefusal)
            assert outcome.case.as_of == txn.transacted_on
            if isinstance(outcome, CasePrediction):
                assert outcome.predicted_low <= outcome.predicted_base
                assert outcome.predicted_base <= outcome.predicted_high

    def test_a_run_reports_metrics_and_flags_the_synthetic_corpus(self, session: Session) -> None:
        outcome = run_backtest(session, sample_size=30, seed=7, persist=False)
        payload = outcome.to_dict()
        assert payload["evidence_is_synthetic"] is True
        assert payload["backtest"]["measures"] == "internal_consistency"
        assert outcome.report.overall.cases == 30
        assert outcome.report.overall.valued + outcome.report.overall.refused == 30
        assert payload["sampling"]["seed"] == 7

    def test_a_run_is_reproducible_from_its_seed(self, session: Session) -> None:
        a = run_backtest(session, sample_size=20, seed=99, persist=False)
        b = run_backtest(session, sample_size=20, seed=99, persist=False)
        assert a.report.overall.to_dict() == b.report.overall.to_dict()

    def test_a_persisted_run_stores_every_case(self, session: Session) -> None:
        outcome = run_backtest(session, sample_size=20, seed=21)
        assert outcome.run_id is not None
        run = session.get(BacktestRun, outcome.run_id)
        assert run is not None
        assert run.evidence_is_synthetic is True
        assert "SYNTHETIC" in run.caveat
        rows = list(session.scalars(select(BacktestResult).where(BacktestResult.run_id == run.id)))
        assert len(rows) == run.held_out_count == 20
        assert sum(1 for r in rows if r.refused) == run.refused_count
        # A refusal carries no prediction, and a valued case always does.
        assert all((r.predicted_base is None) == r.refused for r in rows)
        session.rollback()

    def test_latest_run_returns_the_most_recent(self, session: Session) -> None:
        run_backtest(session, sample_size=10, seed=41)
        second = run_backtest(session, sample_size=10, seed=42)
        found = latest_run(session)
        assert found is not None and found.id == second.run_id
        session.rollback()
