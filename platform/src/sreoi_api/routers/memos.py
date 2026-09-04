"""Investment-memo endpoints and the memo panel.

The read endpoint deliberately returns 404 *with the gate reason* when a memo
does not exist. "No memo" is an answer, and the useful part of it is why: the
score was too low, confidence was too thin, or generation was abandoned
because a figure could not be resolved. A bare 404 would hide the difference
between "not eligible" and "we tried and it failed", which are very different
facts for whoever is deciding whether to trust this platform.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from sreoi_agents.memo import (
    MEMO_METHOD_VERSION,
    PROMPT_VERSION,
    MemoRejectedError,
    generate_memo,
    latest_memo,
    load_memo_inputs,
    memo_gate,
)
from sreoi_agents.memo import deterministic_memo_responder as _memo_responder
from sreoi_agents.provider import DeterministicProvider
from sreoi_agents.runtime import AgentContext
from sreoi_api.i18n import (
    direction,
    format_number,
    localise_digits,
    normalise_locale,
    register_strings,
    translator,
)
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models import Opportunity
from sreoi_persistence.models_memos import InvestmentMemoRow

API_PREFIX = "/api/v1"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

router = APIRouter(tags=["memo"])

# Chrome only. The memo body itself is generated per locale by the agent, not
# translated from English -- a memo about Saudi property that reads like a
# translation is a memo nobody in this market will use.
register_strings(
    "en",
    {
        "memo.title": "Investment memo",
        "memo.subtitle": (
            "Synthesis over computed numbers. Every figure resolves to a field "
            "this platform computed."
        ),
        "memo.none": "No investment memo has been generated.",
        "memo.gate": "Why there is no memo",
        "memo.decision": "Recommendation",
        "memo.max_price": "Maximum recommended purchase price",
        "memo.target_margin": "Target margin",
        "memo.figures": "Figures cited, and where each came from",
        "memo.figure": "Figure",
        "memo.field": "Computed field",
        "memo.value": "Value",
        "memo.section": "Section",
        "memo.versions": "Generated against",
        "memo.generated_at": "Generated",
        "memo.provider": "Provider",
        "memo.disclosure": (
            "No language model was called. The narrative is assembled offline by a "
            "deterministic responder recorded as provider deterministic-offline; it "
            "is template text over computed fields, not model reasoning."
        ),
        "memo.not_advice": (
            "Decision support, not investment advice and not a valuation certificate."
        ),
        "memo.back": "Back to the opportunity",
        "memo.rejected": "Memo generation was abandoned",
        "decision.PROCEED_TO_DILIGENCE": "Proceed to diligence",
        "decision.INVESTIGATE": "Investigate further",
        "decision.PASS": "Pass",
    },
)
register_strings(
    "ar",
    {
        "memo.title": "مذكرة استثمارية",
        "memo.subtitle": "تحليل مبني على أرقام محسوبة. كل رقم فيها يعود إلى حقل حسبته المنصة.",
        "memo.none": "لم تُولّد أي مذكرة استثمارية.",
        "memo.gate": "سبب عدم وجود مذكرة",
        "memo.decision": "التوصية",
        "memo.max_price": "أقصى سعر شراء موصى به",
        "memo.target_margin": "الهامش المستهدف",
        "memo.figures": "الأرقام المذكورة ومصدر كل منها",
        "memo.figure": "الرقم",
        "memo.field": "الحقل المحسوب",
        "memo.value": "القيمة",
        "memo.section": "القسم",
        "memo.versions": "مولّدة مقابل",
        "memo.generated_at": "تاريخ التوليد",
        "memo.provider": "المزوّد",
        "memo.disclosure": (
            "لم يُستدعَ أي نموذج لغوي. النص مُجمّع محلياً بمولّد حتمي مسجل بالمزوّد "
            "deterministic-offline، وهو نص قوالب على حقول محسوبة لا استنتاج نموذج."
        ),
        "memo.not_advice": "دعم قرار، وليست نصيحة استثمارية ولا شهادة تقييم.",
        "memo.back": "الرجوع إلى الفرصة",
        "memo.rejected": "تم التخلي عن توليد المذكرة",
        "decision.PROCEED_TO_DILIGENCE": "المضي إلى الفحص النافي للجهالة",
        "decision.INVESTIGATE": "مزيد من الفحص",
        "decision.PASS": "الامتناع",
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
LocaleQuery = Annotated[str | None, Query(alias="lang")]


def _opportunity(session: Session, opportunity_id: uuid.UUID) -> Opportunity:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opportunity not found")
    return opportunity


def _context(session: Session, responder: Any = _memo_responder) -> AgentContext:
    # Offline by construction: no credentials exist here and none are added.
    return AgentContext(session=session, provider=DeterministicProvider(responder))


def _serialise(row: InvestmentMemoRow, locale: str) -> dict[str, Any]:
    return {
        "opportunity_id": str(row.opportunity_id),
        "locale": row.locale,
        "status": row.status,
        "decision": row.decision,
        "max_recommended_purchase_price": (
            float(row.max_recommended_purchase_price)
            if row.max_recommended_purchase_price is not None
            else None
        ),
        "target_margin": float(row.target_margin) if row.target_margin is not None else None,
        "score_total": float(row.score_total) if row.score_total is not None else None,
        "data_confidence": (
            float(row.data_confidence) if row.data_confidence is not None else None
        ),
        "sections": row.sections or [],
        "figures": row.figures or [],
        "versions": {
            "memo": row.memo_method_version,
            "prompt": row.prompt_version,
            "scoring": row.scoring_method_version,
            "valuation": row.valuation_method_version,
            "cost": row.cost_method_version,
            "weight_profile": row.weight_profile_version,
        },
        "evidence_ids": {
            "score_id": str(row.score_id) if row.score_id else None,
            "valuation_id": str(row.valuation_id) if row.valuation_id else None,
            "cost_id": str(row.cost_id) if row.cost_id else None,
        },
        "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
        "provider": row.provider,
        "attempts": row.attempts,
        "generated_at": row.generated_at.isoformat(),
        "reason": row.reason,
        "requested_locale": locale,
        "disclosure": (
            "No language model was called. The narrative is assembled offline by a "
            "deterministic responder and recorded as provider "
            "'deterministic-offline'. Every figure resolves to a computed field, "
            "listed under 'figures'."
        ),
    }


@router.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}/memo")
def get_memo(
    opportunity_id: uuid.UUID, session: SessionDep, lang: LocaleQuery = None
) -> dict[str, Any]:
    """The memo, or a 404 that says exactly why there is not one."""
    locale = normalise_locale(lang)
    opportunity = _opportunity(session, opportunity_id)
    row = latest_memo(session, opportunity_id, locale)
    if row is not None and row.status == "GENERATED":
        return _serialise(row, locale)

    gate = memo_gate(load_memo_inputs(session, opportunity))
    reason = (
        row.reason
        if row is not None and row.reason
        else gate.reason or "no memo has been generated for this opportunity yet"
    )
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        {
            "detail": "no investment memo",
            "reason": reason,
            "gate_passed": gate.allowed,
            "score": gate.score_total,
            "data_confidence": gate.data_confidence,
            "last_status": row.status if row is not None else None,
        },
    )


@router.post(f"{API_PREFIX}/opportunities/{{opportunity_id}}/memo", status_code=201)
def create_memo(
    opportunity_id: uuid.UUID, session: SessionDep, lang: LocaleQuery = None
) -> dict[str, Any]:
    """Generate the memo for this opportunity in one locale.

    A gate refusal is a 409 rather than an error: it is the expected answer for
    most of the population and carries its reason. An abandoned generation is a
    502, because it means the agent could not be made to stay inside the facts.
    """
    locale = normalise_locale(lang)
    opportunity = _opportunity(session, opportunity_id)
    try:
        record = generate_memo(
            session, opportunity=opportunity, context=_context(session), locale=locale
        )
    except MemoRejectedError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {
                "detail": "memo generation was abandoned",
                "reason": str(exc),
                "stored": False,
            },
        ) from exc

    if not record.generated:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": "memo not generated", "reason": record.reason},
        )
    row = latest_memo(session, opportunity_id, locale)
    assert row is not None  # just written
    return _serialise(row, locale)


@router.get("/opportunities/{opportunity_id}/memo", response_class=HTMLResponse)
def ui_memo(opportunity_id: uuid.UUID, request: Request, session: SessionDep) -> HTMLResponse:
    """The memo panel. Renders the gate reason when there is no memo."""
    locale = normalise_locale(request.query_params.get("lang"))
    opportunity = _opportunity(session, opportunity_id)
    row = latest_memo(session, opportunity_id, locale)
    memo = row if row is not None and row.status == "GENERATED" else None
    gate = memo_gate(load_memo_inputs(session, opportunity))

    return TEMPLATES.TemplateResponse(
        request=request,
        name="memo.html",
        context={
            "locale": locale,
            "dir": direction(locale),
            "t": translator(locale),
            "num": lambda v, d=0: format_number(v, locale, d),
            "digits": lambda s: localise_digits(str(s), locale),
            "other_locale": "en" if locale == "ar" else "ar",
            "opportunity": opportunity,
            "memo": memo,
            "gate": gate,
            "last_status": row.status if row is not None else None,
            "last_reason": row.reason if row is not None else None,
            "memo_method_version": MEMO_METHOD_VERSION,
            "prompt_version": PROMPT_VERSION,
            "target_margin_pct": (
                float(memo.target_margin) * 100.0
                if memo is not None and memo.target_margin is not None
                else None
            ),
            "max_price": (
                Decimal(memo.max_recommended_purchase_price)
                if memo is not None and memo.max_recommended_purchase_price is not None
                else None
            ),
        },
    )
