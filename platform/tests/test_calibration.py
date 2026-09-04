"""Confidence calibration arithmetic (spec section 7)."""

from __future__ import annotations

import pytest

from sreoi_domain.calibration import (
    CALIBRATION_TOLERANCE,
    CalibrationVerdict,
    HitCriterion,
    Prediction,
    base_rate,
    brier_score,
    brier_skill_score,
    build_report,
    discrimination_auc,
    error_rank_correlation,
    expected_calibration_error,
    maximum_calibration_error,
    reliability_curve,
)


def _p(conf: float, *, inside: bool = True, error: float = 0.05) -> Prediction:
    return Prediction(claimed_confidence=conf, inside_interval=inside, abs_pct_error=error)


class TestPrediction:
    def test_rejects_confidence_outside_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="confidence must be in"):
            _p(1.4)

    def test_rejects_negative_error(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            Prediction(claimed_confidence=0.5, inside_interval=True, abs_pct_error=-0.1)

    def test_interval_and_tolerance_criteria_are_independent(self) -> None:
        """A point estimate can be accurate while the band misses, and the
        reverse; conflating them would hide whichever failure is real."""
        p = _p(0.8, inside=False, error=0.02)
        assert p.hit(HitCriterion.INTERVAL) is False
        assert p.hit(HitCriterion.TOLERANCE, 0.12) is True


class TestReliabilityCurve:
    def test_buckets_by_stated_confidence(self) -> None:
        curve = reliability_curve([_p(0.45), _p(0.55), _p(0.55)], edges=(0.0, 0.5, 1.0))
        assert [b.count for b in curve] == [1, 2]

    def test_upper_edge_lands_in_the_last_bucket(self) -> None:
        curve = reliability_curve([_p(1.0)], edges=(0.0, 0.5, 1.0))
        assert curve[-1].count == 1

    def test_empty_buckets_are_retained(self) -> None:
        """ "We never issued a valuation above 80% confidence" is a finding, and
        dropping the row would hide it."""
        curve = reliability_curve([_p(0.1)], edges=(0.0, 0.5, 1.0))
        assert curve[1].count == 0
        assert curve[1].verdict is CalibrationVerdict.EMPTY
        assert curve[1].gap is None

    def test_overconfident_bucket_is_named(self) -> None:
        preds = [_p(0.9, inside=i < 3) for i in range(10)]
        curve = reliability_curve(preds, edges=(0.0, 1.0))
        assert curve[0].realised_rate == pytest.approx(0.3)
        assert curve[0].gap == pytest.approx(0.3 - 0.9)
        assert curve[0].verdict is CalibrationVerdict.OVERCONFIDENT

    def test_underconfident_bucket_is_named(self) -> None:
        curve = reliability_curve([_p(0.4) for _ in range(10)], edges=(0.0, 1.0))
        assert curve[0].verdict is CalibrationVerdict.UNDERCONFIDENT

    def test_within_tolerance_counts_as_calibrated(self) -> None:
        claim = 0.9
        hits = round((claim - CALIBRATION_TOLERANCE / 2) * 10)
        curve = reliability_curve([_p(claim, inside=i < hits) for i in range(10)], edges=(0.0, 1.0))
        assert curve[0].verdict is CalibrationVerdict.CALIBRATED

    def test_requires_at_least_two_edges(self) -> None:
        with pytest.raises(ValueError, match="two bucket edges"):
            reliability_curve([_p(0.5)], edges=(0.5,))


class TestScores:
    def test_brier_of_a_perfect_forecaster_is_zero(self) -> None:
        assert brier_score([_p(1.0, inside=True), _p(0.0, inside=False)]) == 0.0

    def test_brier_of_a_confident_wrong_forecaster_is_one(self) -> None:
        assert brier_score([_p(1.0, inside=False)]) == 1.0

    def test_brier_of_a_coin_flip_is_a_quarter(self) -> None:
        assert brier_score([_p(0.5, inside=True), _p(0.5, inside=False)]) == 0.25

    def test_base_rate_counts_hits(self) -> None:
        assert base_rate([_p(0.5, inside=True), _p(0.5, inside=False)]) == 0.5

    def test_a_constant_score_has_no_skill(self) -> None:
        """The blunt question the spec is really asking: does the confidence
        figure carry information a single number would not?"""
        preds = [_p(0.5, inside=i % 2 == 0) for i in range(10)]
        assert brier_skill_score(preds) == pytest.approx(0.0)

    def test_a_discriminating_score_has_positive_skill(self) -> None:
        preds = [_p(0.9, inside=True) for _ in range(5)] + [_p(0.1, inside=False) for _ in range(5)]
        skill = brier_skill_score(preds)
        assert skill is not None and skill > 0.5

    def test_skill_is_undefined_when_every_case_has_the_same_outcome(self) -> None:
        assert brier_skill_score([_p(0.7), _p(0.8)]) is None

    def test_brier_rejects_an_empty_set(self) -> None:
        with pytest.raises(ValueError, match="no predictions"):
            brier_score([])


class TestCalibrationError:
    def test_ece_is_count_weighted(self) -> None:
        curve = reliability_curve(
            [_p(0.9, inside=False)] + [_p(0.1, inside=False) for _ in range(9)],
            edges=(0.0, 0.5, 1.0),
        )
        # gaps: -0.1 (n=9) and -0.9 (n=1)
        assert expected_calibration_error(curve) == pytest.approx((9 * 0.1 + 0.9) / 10)

    def test_mce_reports_the_worst_bucket(self) -> None:
        curve = reliability_curve(
            [_p(0.9, inside=False)] + [_p(0.1, inside=False) for _ in range(9)],
            edges=(0.0, 0.5, 1.0),
        )
        assert maximum_calibration_error(curve) == pytest.approx(0.9)

    def test_no_data_means_no_figure_rather_than_zero(self) -> None:
        curve = reliability_curve([], edges=(0.0, 1.0))
        assert expected_calibration_error(curve) is None
        assert maximum_calibration_error(curve) is None


class TestDiscrimination:
    def test_perfect_ordering_scores_one(self) -> None:
        preds = [_p(0.9, inside=True), _p(0.2, inside=False)]
        assert discrimination_auc(preds) == 1.0

    def test_reversed_ordering_scores_zero(self) -> None:
        preds = [_p(0.2, inside=True), _p(0.9, inside=False)]
        assert discrimination_auc(preds) == 0.0

    def test_ties_score_a_half(self) -> None:
        preds = [_p(0.5, inside=True), _p(0.5, inside=False)]
        assert discrimination_auc(preds) == 0.5

    def test_auc_is_undefined_without_both_outcomes(self) -> None:
        assert discrimination_auc([_p(0.5, inside=True)]) is None

    def test_confidence_that_predicts_error_correlates_negatively(self) -> None:
        preds = [_p(0.9 - 0.1 * i, error=0.02 * (i + 1)) for i in range(8)]
        corr = error_rank_correlation(preds)
        assert corr is not None and corr == pytest.approx(-1.0)

    def test_correlation_needs_variation(self) -> None:
        assert error_rank_correlation([_p(0.5), _p(0.5), _p(0.5)]) is None


class TestReport:
    def test_overconfidence_is_stated_not_softened(self) -> None:
        preds = [_p(0.85, inside=i < 4) for i in range(10)]
        report = build_report(preds, evidence_is_synthetic=True)
        assert report.verdict is CalibrationVerdict.OVERCONFIDENT
        assert report.overall_gap is not None and report.overall_gap < -0.4
        assert "over-states" in report.finding
        assert report.miscalibrated_buckets

    def test_synthetic_flag_is_in_the_payload_not_only_the_prose(self) -> None:
        report = build_report([_p(0.6)], evidence_is_synthetic=True)
        assert report.to_dict()["evidence_is_synthetic"] is True

    def test_empty_input_reports_no_assessment_rather_than_a_pass(self) -> None:
        report = build_report([], evidence_is_synthetic=False)
        assert report.count == 0
        assert report.verdict is CalibrationVerdict.EMPTY
        assert report.brier is None
        assert "cannot be assessed" in report.finding

    def test_tolerance_criterion_is_reported_separately(self) -> None:
        preds = [_p(0.6, inside=True, error=0.30)]
        interval = build_report(preds, evidence_is_synthetic=False)
        point = build_report(preds, criterion=HitCriterion.TOLERANCE, evidence_is_synthetic=False)
        assert interval.realised_rate == 1.0
        assert point.realised_rate == 0.0
