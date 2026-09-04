"""Natural-language search endpoint.

The response is deliberately ordered so that the compiled filters, the
interpretation of every vague term, and everything that was *not* understood
come before the results. A user who cannot see the filter cannot correct it,
and a search tool that quietly narrows a request is worse than one that asks.

This module is the only place where the compiled intent meets
`OpportunityFilters`: the agent emits keyword arguments, this router builds the
dataclass, and the existing deterministic search layer executes it with bound
parameters. User text never becomes SQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sreoi_agents.nl_search import (
    MAX_LIMIT,
    PROMPT_VERSION,
    compile_search,
    deterministic_nl_responder,
    district_vocabulary,
)
from sreoi_agents.provider import DeterministicProvider
from sreoi_agents.runtime import AgentContext
from sreoi_api.i18n import register_strings
from sreoi_api.search import SORTS, OpportunityFilters, search
from sreoi_persistence.db import get_session_factory

API_PREFIX = "/api/v1"

router = APIRouter(tags=["search"])

register_strings(
    "en",
    {
        "nl.title": "Search in your own words",
        "nl.placeholder": "apartments in Riyadh under SAR 1.2M with at least 15% discount",
        "nl.compiled": "Compiled filters",
        "nl.interpreted": "How each vague term was read",
        "nl.unmapped": "Not understood",
        "nl.not_enforced": "Understood but not applied",
        "nl.confidence": "Compilation confidence",
        "nl.refused": "This request was refused",
        "nl.edit": "Edit these filters",
    },
)
register_strings(
    "ar",
    {
        "nl.title": "ابحث بكلماتك",
        # Arabic-Indic numerals are a supported input form, so the example uses
        # them. RUF001 flags them as confusable with Latin letters; here they
        # are genuinely Arabic digits.
        "nl.placeholder": "شقق في الرياض تحت ١.٢ مليون بخصم ١٥٪ على الأقل",  # noqa: RUF001
        "nl.compiled": "المرشحات المُجمّعة",
        "nl.interpreted": "كيف فُهم كل تعبير غير محدد",
        "nl.unmapped": "غير مفهوم",
        "nl.not_enforced": "مفهوم لكن غير مطبق",
        "nl.confidence": "ثقة التجميع",
        "nl.refused": "تم رفض هذا الطلب",
        "nl.edit": "تعديل هذه المرشحات",
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


class NaturalLanguageQuery(BaseModel):
    query: str = Field(min_length=1, max_length=600)
    # Results are optional so a client can compile and show the filters first,
    # then fetch. The filters come back either way.
    include_results: bool = True
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)


@router.post(f"{API_PREFIX}/search/natural-language")
def natural_language_search(payload: NaturalLanguageQuery, session: SessionDep) -> dict[str, Any]:
    """Compile a request into visible filters, then run the ordinary search."""
    districts = district_vocabulary(session)
    if not districts:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no district reference data is loaded, so no request can be compiled",
        )

    context = AgentContext(
        session=session, provider=DeterministicProvider(deterministic_nl_responder)
    )
    compiled = compile_search(query=payload.query, districts=districts, context=context)
    intent = compiled.intent
    if payload.limit is not None:
        intent = intent.model_copy(update={"limit": payload.limit})

    kwargs = intent.filter_kwargs()
    filters = OpportunityFilters(**kwargs)

    body: dict[str, Any] = {
        "query": payload.query,
        "filters": intent.describe(),
        "filters_applied": filters.describe(),
        "interpreted": dict(intent.interpreted),
        "unmapped": list(intent.unmapped),
        "not_enforced": [u.model_dump() for u in intent.not_enforced],
        "confidence": intent.confidence,
        "refused": intent.refused,
        "refusal_reason": intent.refusal_reason,
        "injection_flagged": compiled.injection_flagged,
        "injection_summary": compiled.injection_summary,
        "sorts_available": SORTS,
        "prompt_version": PROMPT_VERSION,
        "agent_run_id": str(compiled.run_id) if compiled.run_id else None,
        "provider": context.provider.name,
        "disclosure": (
            "No language model was called. Compilation is rule-based and offline, "
            "recorded as provider 'deterministic-offline'. The compiled filters "
            "above are what was executed; nothing else was inferred."
        ),
    }

    if intent.refused or not payload.include_results:
        body["count"] = 0
        body["results"] = []
        return body

    results = search(session, filters)
    body["count"] = len(results)
    body["results"] = [
        {
            **row,
            "id": str(row["id"]),
            "true_acquisition_cost": (
                float(row["true_acquisition_cost"])
                if row["true_acquisition_cost"] is not None
                else None
            ),
            "fair_value_base": (
                float(row["fair_value_base"]) if row["fair_value_base"] is not None else None
            ),
        }
        for row in results
    ]
    return body
