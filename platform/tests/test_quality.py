"""Data-quality monitoring and the admin endpoints (backlog E8.4)."""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sreoi_api.i18n import CATALOGUE, translator
from sreoi_api.routers.quality import router
from sreoi_persistence.models_quality import QualitySnapshot
from sreoi_pipeline.quality import (
    THRESHOLDS,
    THRESHOLDS_BY_KEY,
    Severity,
    Threshold,
    agent_disagreement,
    collect_quality,
    confidence_distribution,
    duplicate_resolution,
    field_completeness,
    latest_snapshot,
    persist_snapshot,
    provenance_basis,
    refusals,
    snapshot_history,
    source_freshness,
    verification_rates,
)
from tests.conftest import requires_db


@pytest.fixture(scope="module")
def populated(seeded_db: None) -> Iterator[None]:
    """A small opportunity graph with duplicates, so the quality metrics have
    something to measure.

    Built from the demonstration corpus helpers rather than hand-written
    submissions: a monitoring test that measures data the pipeline would never
    actually produce measures nothing.

    Module-scoped, and it clears the mutable tables on the way in *and* out, so
    it behaves like `isolated` for this module without paying to re-ingest the
    graph for every one of twenty assertions.
    """
    from sreoi_persistence.db import session_scope
    from sreoi_pipeline.demo import build_duplicates, build_submissions
    from sreoi_pipeline.ingest import ingest_manual_submission
    from tests.conftest import _MUTABLE_TABLES

    truncate = text(f"TRUNCATE {', '.join(_MUTABLE_TABLES)} RESTART IDENTITY CASCADE")
    submissions = build_submissions(12)
    with session_scope() as db:
        db.execute(truncate)
        for payload in [*submissions, *build_duplicates(submissions)]:
            ingest_manual_submission(db, dict(payload))
    yield
    with session_scope() as db:
        db.execute(truncate)


class TestThresholds:
    def test_higher_is_better_thresholds_fire_downwards(self) -> None:
        t = Threshold("k", "l", warn=0.7, fail=0.5, higher_is_better=True, note="n")
        assert t.severity(0.9) is Severity.OK
        assert t.severity(0.6) is Severity.WARN
        assert t.severity(0.4) is Severity.FAIL

    def test_lower_is_better_thresholds_fire_upwards(self) -> None:
        t = Threshold("k", "l", warn=0.2, fail=0.4, higher_is_better=False, note="n")
        assert t.severity(0.1) is Severity.OK
        assert t.severity(0.3) is Severity.WARN
        assert t.severity(0.5) is Severity.FAIL

    def test_a_missing_measurement_is_not_reported_as_a_failure(self) -> None:
        """No data is not bad data. Flagging it FAIL would train operators to
        ignore the dashboard."""
        t = Threshold("k", "l", warn=0.7, fail=0.5, higher_is_better=True, note="n")
        assert t.severity(None) is Severity.OK

    def test_every_threshold_carries_an_explanation(self) -> None:
        assert THRESHOLDS
        for threshold in THRESHOLDS:
            assert threshold.note.strip()
            assert threshold.label.strip()

    def test_severity_ordering_lets_the_worst_flag_win(self) -> None:
        assert Severity.FAIL.rank > Severity.WARN.rank > Severity.OK.rank


@requires_db
class TestMetricsAgainstTheCorpus:
    def test_field_completeness_is_per_field_and_bounded(
        self, populated: None, session: Session
    ) -> None:
        by_field, overall, count = field_completeness(session)
        assert count > 0
        assert by_field
        assert all(0.0 <= v <= 1.0 for v in by_field.values())
        assert overall is not None and 0.0 <= overall <= 1.0

    def test_confidence_distribution_buckets_sum_to_the_population(
        self, populated: None, session: Session
    ) -> None:
        dist = confidence_distribution(session)
        data = dist["data_confidence"]
        assert data["count"] > 0
        assert sum(b["count"] for b in data["buckets"]) == data["count"]
        assert data["below_gate"] is not None

    def test_duplicate_resolution_reads_the_decision_log(
        self, populated: None, session: Session
    ) -> None:
        """`properties.merged_into_id` is never set by the ingest path, so the
        rate must come from the decision log or it is structurally zero."""
        dup = duplicate_resolution(session)
        assert dup["decisions"] > 0
        assert dup["duplicate_resolution_rate"] is not None
        assert dup["min_auto_merge_score"] >= dup["auto_merge_threshold"]
        assert dup["borderline_merge_share"] is not None

    def test_source_freshness_reports_an_age_per_source(
        self, populated: None, session: Session
    ) -> None:
        rows, stalest = source_freshness(session)
        assert rows
        assert any(r["is_synthetic"] for r in rows), "the synthetic source must be visible"
        assert stalest is None or stalest >= 0.0

    def test_verification_pass_rate_excludes_checks_that_could_not_run(
        self, populated: None, session: Session
    ) -> None:
        rates = verification_rates(session)
        assert rates["applicable"] > 0
        assert rates["verified"] <= rates["applicable"]
        unavailable = sum(
            entry["statuses"].get("UNAVAILABLE", 0) for entry in rates["by_check_type"].values()
        )
        assert unavailable > 0, "official registers are not integrated yet"
        # A check that could not run is not a check that failed.
        assert rates["applicable"] + unavailable > rates["applicable"]

    def test_agent_disagreement_is_zero_because_the_guard_holds(
        self, populated: None, session: Session
    ) -> None:
        """The agent may explain the deterministic checks, never decide them.
        A non-zero stored rate means that guarantee has stopped holding."""
        agents = agent_disagreement(session)
        assert agents["compared_summaries"] > 0
        assert agents["stored_disagreements"] == 0
        assert agents["stored_rate"] == 0.0

    def test_refusals_are_counted_as_coverage_signals(
        self, populated: None, session: Session
    ) -> None:
        result = refusals(session)
        assert result["scored_opportunities"] > 0
        assert result["refused_discount_rate"] is not None
        assert result["insufficient_data_rate"] is not None
        assert result["valuation_refusal_rate"] is not None

    def test_provenance_basis_distribution_includes_unknowns(
        self, populated: None, session: Session
    ) -> None:
        prov = provenance_basis(session)
        assert prov["total"] > 0
        assert prov["unknown_share"] is not None
        assert "UNKNOWN" in prov["by_basis"]


@requires_db
class TestReport:
    def test_a_reading_flags_every_threshold(self, populated: None, session: Session) -> None:
        report = collect_quality(session)
        assert {f.key for f in report.flags} == set(THRESHOLDS_BY_KEY)

    def test_overall_status_is_the_worst_flag(self, populated: None, session: Session) -> None:
        report = collect_quality(session)
        worst = max((f.severity.rank for f in report.flags), default=0)
        assert report.overall_status.rank == worst

    def test_the_synthetic_flag_travels_with_the_metrics(
        self, populated: None, session: Session
    ) -> None:
        payload = collect_quality(session).to_dict()
        assert payload["evidence_is_synthetic"] is True
        assert "overall_status" in payload

    def test_a_snapshot_is_stored_and_read_back(self, populated: None, session: Session) -> None:
        report = collect_quality(session)
        stored = persist_snapshot(session, report)
        assert stored.id is not None
        assert stored.overall_status == report.overall_status.value
        assert stored.evidence_is_synthetic is True
        found = latest_snapshot(session)
        assert found is not None and found.id == stored.id
        assert snapshot_history(session, limit=5)
        session.rollback()

    def test_snapshots_accumulate_so_drift_is_visible(
        self, populated: None, session: Session
    ) -> None:
        before = len(list(session.scalars(select(QualitySnapshot))))
        persist_snapshot(session, collect_quality(session))
        persist_snapshot(session, collect_quality(session))
        after = len(list(session.scalars(select(QualitySnapshot))))
        assert after == before + 2
        session.rollback()


def _client() -> object:
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@requires_db
class TestEndpoints:
    """The router is mounted on a bare app rather than `sreoi_api.main`, so a
    failure elsewhere in the auto-discovered router package cannot mask a
    failure here."""

    def test_quality_endpoint_leads_with_the_synthetic_flag(self, populated: None) -> None:
        response = _client().get("/api/v1/admin/quality")  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = response.json()
        assert body["evidence_is_synthetic"] is True
        assert body["overall_status"] in {"OK", "WARN", "FAIL"}
        assert body["flags"]

    def test_running_a_backtest_returns_the_caveat_in_the_payload(self, populated: None) -> None:
        response = _client().post(  # type: ignore[attr-defined]
            "/api/v1/admin/backtest/run?sample=20&seed=5"
        )
        assert response.status_code == 201
        body = response.json()
        assert body["evidence_is_synthetic"] is True
        assert "SYNTHETIC EVIDENCE" in body["caveat"]
        assert body["backtest"]["overall"]["cases"] == 20

    def test_sample_size_is_bounded(self, populated: None) -> None:
        response = _client().post(  # type: ignore[attr-defined]
            "/api/v1/admin/backtest/run?sample=99999"
        )
        assert response.status_code == 422

    def test_latest_backtest_is_served_from_the_stored_run(self, populated: None) -> None:
        client = _client()
        client.post("/api/v1/admin/backtest/run?sample=15&seed=8")  # type: ignore[attr-defined]
        response = client.get("/api/v1/admin/backtest/latest")  # type: ignore[attr-defined]
        assert response.status_code == 200
        body = response.json()
        assert body["evidence_is_synthetic"] is True
        assert body["report"]["backtest"]["overall"]["cases"] == 15
        assert "verdicts" in body

    def test_snapshot_endpoint_stores_a_reading(self, populated: None) -> None:
        response = _client().post("/api/v1/admin/quality/snapshot")  # type: ignore[attr-defined]
        assert response.status_code == 201
        assert response.json()["evidence_is_synthetic"] is True

    def test_dashboard_puts_the_caveat_in_a_banner_not_a_footnote(self, populated: None) -> None:
        html = _client().get("/admin/quality").text  # type: ignore[attr-defined]
        assert 'role="alert"' in html
        assert "INTERNAL CONSISTENCY ONLY" in html
        assert "{{" not in html and "{%" not in html

    def test_status_is_never_signalled_by_colour_alone(self, populated: None) -> None:
        """WCAG 2.1 AA 1.4.1: every status pill carries a shape glyph and a
        word as well as a hue, so the page survives greyscale and a screen
        reader."""
        client = _client()
        # Ensure the back-test panel renders too, so its verdict pills are
        # covered and the test does not depend on another test having run.
        client.post("/api/v1/admin/backtest/run?sample=10&seed=3")  # type: ignore[attr-defined]
        html = client.get("/admin/quality").text  # type: ignore[attr-defined]
        pills = re.findall(
            r'<span class="pill (\w+)"[^>]*>\s*<span aria-hidden="true">(\S)</span>\s*'
            r"([^<]*)</span>",
            html,
        )
        assert len(pills) > 20, "the dashboard should be full of status pills"
        assert {colour for colour, _, _ in pills} & {"good", "warn", "bad"}
        for colour, glyph, label in pills:
            assert glyph.strip(), f"{colour} pill has no shape glyph"
            assert label.strip(), f"{colour} pill has no text label"

    def test_dashboard_renders_in_arabic_with_rtl(self, populated: None) -> None:
        html = _client().get("/admin/quality?lang=ar").text  # type: ignore[attr-defined]
        assert 'dir="rtl"' in html
        assert "الاتساق الداخلي" in html


class TestTranslations:
    def test_every_english_string_has_an_arabic_counterpart(self) -> None:
        keys = [k for k in CATALOGUE["en"] if k.startswith(("quality.", "status."))]
        assert keys
        missing = [k for k in keys if k not in CATALOGUE["ar"]]
        assert missing == []

    def test_a_missing_key_falls_back_rather_than_rendering_blank(self) -> None:
        assert translator("ar")("quality.title")
        assert translator("en")("quality.title")

    @pytest.mark.parametrize(
        "code",
        ["PASS", "FAIL", "NOT_ASSESSED", "OK", "WARN", "CALIBRATED", "OVERCONFIDENT"],
    )
    def test_every_status_code_the_template_renders_has_a_label(self, code: str) -> None:
        assert CATALOGUE["en"][f"status.{code}"]
