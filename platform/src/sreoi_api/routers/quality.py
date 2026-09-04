"""Back-test and data-quality endpoints, plus the admin dashboard.

Discovered by `routers.discover()`, so nothing shared has to be edited to add
it. Its own i18n strings are registered at import time through
`i18n.register_strings` for the same reason.

**The synthetic-evidence caveat is a field, not a footnote.** Every payload
here carries `evidence_is_synthetic` and the caveat sentence at the top level,
because a client that renders only the metrics must not be able to render them
without it. A back-test error figure quoted as market accuracy would be the
most damaging thing this product could publish, and prose in a docstring does
not survive a copy-paste into a slide.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from sreoi_api.i18n import (
    direction,
    format_number,
    localise_digits,
    normalise_locale,
    register_strings,
    translator,
)
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models_quality import BacktestResult, BacktestRun
from sreoi_pipeline.backtest import (
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_SAMPLE,
    DEFAULT_SEED,
    latest_run,
    run_backtest,
)
from sreoi_pipeline.quality import (
    collect_quality,
    latest_snapshot,
    persist_snapshot,
    snapshot_history,
)

API_PREFIX = "/api/v1"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

# Bounded so an admin cannot accidentally back-test the entire corpus
# synchronously and time the request out.
MAX_SAMPLE = 1000

router = APIRouter(tags=["quality"])


register_strings(
    "en",
    {
        "nav.quality": "Quality",
        "quality.title": "Back-testing and data quality",
        "quality.subtitle": (
            "Whether the valuation engine is right, and whether the data feeding it "
            "is still healthy."
        ),
        "quality.banner": (
            "These back-test figures measure INTERNAL CONSISTENCY ONLY. The comparable "
            "corpus is generated fixture data, not registered Saudi sales, so no number "
            "on this page is evidence of accuracy in the real market."
        ),
        "quality.backtest": "Back-test",
        "quality.calibration": "Confidence calibration",
        "quality.segments": "Error by segment",
        "quality.monitoring": "Data-quality monitoring",
        "quality.history": "Snapshot history",
        "quality.no_run": "No back-test has been run yet.",
        "quality.run_hint": "Run: python -m sreoi_pipeline.backtest",
        "quality.metric": "Metric",
        "quality.value": "Value",
        "quality.target": "Target",
        "quality.status": "Status",
        "quality.segment": "Segment",
        "quality.cases": "Cases",
        "quality.valued": "Valued",
        "quality.refused": "Refused",
        "quality.median_error": "Median |error|",
        "quality.mean_error": "Mean |error|",
        "quality.p90_error": "p90 |error|",
        "quality.bias": "Bias (median signed)",
        "quality.coverage": "Interval coverage",
        "quality.width": "Median band width",
        "quality.confidence": "Mean stated confidence",
        "quality.bucket": "Confidence bucket",
        "quality.claimed": "Claimed",
        "quality.realised": "Realised",
        "quality.gap": "Gap",
        "quality.count": "n",
        "quality.brier": "Brier score",
        "quality.skill": "Brier skill vs. a constant",
        "quality.ece": "Expected calibration error",
        "quality.auc": "Discrimination (AUC)",
        "quality.corr": "Confidence vs. error (Spearman)",
        "quality.finding": "Finding",
        "quality.refusal_note": (
            "A refusal is a correct outcome, not a failure. Refused cases are counted "
            "here and excluded from the error metrics."
        ),
        "quality.sampling": "Sampling",
        "quality.captured": "Captured",
        "quality.flag": "Check",
        "quality.note": "What it means",
        "status.PASS": "Meets target",
        "status.FAIL": "Below target",
        "status.NOT_ASSESSED": "Not assessed",
        "status.OK": "Healthy",
        "status.WARN": "Watch",
        "status.CALIBRATED": "Calibrated",
        "status.OVERCONFIDENT": "Over-confident",
        "status.UNDERCONFIDENT": "Under-confident",
        "status.EMPTY": "No data",
    },
)
register_strings(
    "ar",
    {
        "nav.quality": "الجودة",
        "quality.title": "الاختبار الرجعي وجودة البيانات",
        "quality.subtitle": "هل التقييم صحيح، وهل البيانات التي تغذيه سليمة.",
        "quality.banner": (
            "هذه النتائج تقيس الاتساق الداخلي فقط. الصفقات المقارنة مولّدة اصطناعياً "
            "وليست صفقات مسجلة حقيقية، فلا يُعد أي رقم في هذه الصفحة دليلاً على الدقة "
            "في السوق الحقيقي."
        ),
        "quality.backtest": "الاختبار الرجعي",
        "quality.calibration": "معايرة الثقة",
        "quality.segments": "الخطأ حسب الفئة",
        "quality.monitoring": "مراقبة جودة البيانات",
        "quality.history": "سجل القياسات",
        "quality.no_run": "لم يُجرَ أي اختبار رجعي بعد.",
        "quality.run_hint": "التشغيل: python -m sreoi_pipeline.backtest",
        "quality.metric": "المؤشر",
        "quality.value": "القيمة",
        "quality.target": "المستهدف",
        "quality.status": "الحالة",
        "quality.segment": "الفئة",
        "quality.cases": "الحالات",
        "quality.valued": "مُقيَّمة",
        "quality.refused": "مرفوضة",
        "quality.median_error": "وسيط الخطأ المطلق",
        "quality.mean_error": "متوسط الخطأ المطلق",
        "quality.p90_error": "الخطأ عند المئين ٩٠",
        "quality.bias": "الانحياز",
        "quality.coverage": "تغطية النطاق",
        "quality.width": "وسيط عرض النطاق",
        "quality.confidence": "متوسط الثقة المعلنة",
        "quality.bucket": "فئة الثقة",
        "quality.claimed": "المعلنة",
        "quality.realised": "المتحققة",
        "quality.gap": "الفرق",
        "quality.count": "العدد",
        "quality.brier": "درجة براير",
        "quality.skill": "مهارة براير مقابل ثابت",
        "quality.ece": "خطأ المعايرة المتوقع",
        "quality.auc": "قدرة التمييز",
        "quality.corr": "الثقة مقابل الخطأ",
        "quality.finding": "النتيجة",
        "quality.refusal_note": (
            "الرفض نتيجة صحيحة وليس فشلاً. الحالات المرفوضة محسوبة ومستثناة من مؤشرات الخطأ."
        ),
        "quality.sampling": "المعاينة",
        "quality.captured": "وقت القياس",
        "quality.flag": "الفحص",
        "quality.note": "المعنى",
        "status.PASS": "يحقق المستهدف",
        "status.FAIL": "أقل من المستهدف",
        "status.NOT_ASSESSED": "غير مُقيَّم",
        "status.OK": "سليم",
        "status.WARN": "يستدعي المتابعة",
        "status.CALIBRATED": "مُعايَرة",
        "status.OVERCONFIDENT": "ثقة مبالغة",
        "status.UNDERCONFIDENT": "ثقة أقل من الواقع",
        "status.EMPTY": "لا بيانات",
    },
)


def _session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(_session)]


def _run_payload(session: Session, run: BacktestRun) -> dict[str, Any]:
    """Serialise a stored run.

    The stored `report` already holds the whole computation, so it is returned
    verbatim rather than recomputed: a figure that changes between the page
    and the API is worse than either figure alone.
    """
    misses = list(
        session.query(BacktestResult)
        .filter(
            BacktestResult.run_id == run.id,
            BacktestResult.inside_interval.is_(False),
        )
        .order_by(BacktestResult.signed_pct_error)
        .limit(20)
    )
    return {
        "run_id": str(run.id),
        # Deliberately before the metrics.
        "evidence_is_synthetic": run.evidence_is_synthetic,
        "measures": "internal_consistency" if run.evidence_is_synthetic else "accuracy",
        "caveat": run.caveat,
        "started_at": run.started_at.isoformat(),
        "method_version": run.method_version,
        "valuation_method_version": run.valuation_method_version,
        "held_out_count": run.held_out_count,
        "refused_count": run.refused_count,
        "verdicts": {
            "point_error": run.point_error_verdict,
            "coverage": run.coverage_verdict,
            "calibration": run.calibration_verdict,
        },
        "report": run.report,
        "interval_misses": [
            {
                "transaction_id": str(row.transaction_id),
                "district": row.district_name,
                "as_of": row.as_of.isoformat(),
                "realised_price": float(row.realised_price),
                "predicted_base": None if row.predicted_base is None else float(row.predicted_base),
                "predicted_low": None if row.predicted_low is None else float(row.predicted_low),
                "predicted_high": None if row.predicted_high is None else float(row.predicted_high),
                "signed_pct_error": None
                if row.signed_pct_error is None
                else float(row.signed_pct_error),
                "confidence": None if row.confidence is None else float(row.confidence),
                "comparable_count": row.comparable_count,
            }
            for row in misses
        ],
    }


@router.post(f"{API_PREFIX}/admin/backtest/run", status_code=status.HTTP_201_CREATED)
def admin_backtest_run(
    session: SessionDep,
    sample: Annotated[int, Query(ge=1, le=MAX_SAMPLE)] = DEFAULT_SAMPLE,
    seed: int = DEFAULT_SEED,
    min_history_days: Annotated[int, Query(ge=0, le=3650)] = DEFAULT_MIN_HISTORY_DAYS,
) -> dict[str, Any]:
    """Run the harness now and store the result.

    Synchronous on purpose at this scale: a few hundred valuations take under a
    second, and a job queue would add a failure mode without adding an answer.
    """
    outcome = run_backtest(
        session, sample_size=sample, seed=seed, min_history_days=min_history_days
    )
    return outcome.to_dict()


@router.get(f"{API_PREFIX}/admin/backtest/latest")
def admin_backtest_latest(session: SessionDep) -> dict[str, Any]:
    run = latest_run(session)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no back-test has been run; POST /api/v1/admin/backtest/run first",
        )
    return _run_payload(session, run)


@router.get(f"{API_PREFIX}/admin/quality")
def admin_quality(session: SessionDep, snapshot: bool = False) -> dict[str, Any]:
    """Current data-quality reading, optionally storing it as a snapshot."""
    report = collect_quality(session)
    if snapshot:
        persist_snapshot(session, report)
    payload = report.to_dict()
    previous = latest_snapshot(session)
    payload["previous_snapshot"] = (
        None
        if previous is None
        else {
            "captured_at": previous.captured_at.isoformat(),
            "overall_status": previous.overall_status,
            "field_completeness": _f(previous.field_completeness),
            "mean_data_confidence": _f(previous.mean_data_confidence),
            "insufficient_data_rate": _f(previous.insufficient_data_rate),
            "verification_pass_rate": _f(previous.verification_pass_rate),
        }
    )
    return payload


def _f(value: float | None) -> float | None:
    return None if value is None else float(value)


@router.post(f"{API_PREFIX}/admin/quality/snapshot", status_code=status.HTTP_201_CREATED)
def admin_quality_snapshot(session: SessionDep) -> dict[str, Any]:
    """Store a reading. A single snapshot says little; the series is the point."""
    report = collect_quality(session)
    stored = persist_snapshot(session, report)
    return {
        "snapshot_id": str(stored.id),
        "captured_at": stored.captured_at.isoformat(),
        "overall_status": stored.overall_status,
        "evidence_is_synthetic": stored.evidence_is_synthetic,
        "regressions": [f.key for f in report.regressions],
    }


@router.get("/admin/quality", response_class=HTMLResponse)
def ui_quality(request: Request, session: SessionDep) -> HTMLResponse:
    locale = normalise_locale(request.query_params.get("lang"))
    run = latest_run(session)
    report = collect_quality(session)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="quality.html",
        context={
            "locale": locale,
            "dir": direction(locale),
            "t": translator(locale),
            "num": lambda v, d=0: format_number(v, locale, d),
            "digits": lambda s: localise_digits(str(s), locale),
            "other_locale": "en" if locale == "ar" else "ar",
            "query": request.query_params,
            "run": run,
            "backtest": run.report if run is not None else None,
            "quality": report,
            "history": snapshot_history(session, limit=10),
        },
    )
