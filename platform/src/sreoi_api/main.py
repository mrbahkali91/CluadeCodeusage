"""FastAPI application.

The HTTP layer only translates: it validates input, calls a pipeline service or
a read model, and serialises. No business logic lives here (ADR-001).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_api import queries
from sreoi_api.schemas import (
    OpportunityDetail,
    OpportunitySummary,
    ProvenanceEntry,
    ScoreOut,
    SourceHealthOut,
    SourceOut,
    SubmissionIn,
    ValuationOut,
)
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models import Opportunity, Source, SourceRecord
from sreoi_pipeline.ingest import IngestionError, ingest_manual_submission
from sreoi_sources.kapsarc import KapsarcIndexSource

API_PREFIX = "/api/v1"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(
    title="Saudi Real Estate Opportunity Intelligence",
    version="0.1.0",
    description=(
        "Slice 1 — analyst entry to scored opportunity. Money numbers are "
        "deterministic and reproducible; no LLM participates in any calculation."
    ),
)


def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def _load(session: Session, opportunity_id: uuid.UUID) -> Opportunity:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opportunity not found")
    return opportunity


@app.get("/health")
def health(session: SessionDep) -> dict[str, Any]:
    session.execute(select(1))
    return {"status": "ok", "database": "reachable"}


@app.post(f"{API_PREFIX}/opportunities", response_model=OpportunityDetail, status_code=201)
def create_opportunity(payload: SubmissionIn, session: SessionDep) -> OpportunityDetail:
    """Analyst / broker submission: ingest, evaluate and score in one call."""
    try:
        opportunity, _ = ingest_manual_submission(session, payload.model_dump(exclude_none=True))
    except IngestionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, list(exc.errors)) from exc
    session.flush()
    return queries.detail(session, opportunity)


@app.get(f"{API_PREFIX}/opportunities", response_model=list[OpportunitySummary])
def list_opportunities(
    session: SessionDep, limit: int = 50, min_score: float | None = None
) -> list[OpportunitySummary]:
    opportunities = session.scalars(
        select(Opportunity).order_by(Opportunity.created_at.desc()).limit(limit)
    )
    summaries = [queries.summarize(session, o) for o in opportunities]
    if min_score is not None:
        summaries = [s for s in summaries if s.score is not None and s.score >= min_score]
    return sorted(summaries, key=lambda s: (s.score is None, -(s.score or 0)))


@app.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}", response_model=OpportunityDetail)
def get_opportunity(opportunity_id: uuid.UUID, session: SessionDep) -> OpportunityDetail:
    return queries.detail(session, _load(session, opportunity_id))


@app.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}/score", response_model=ScoreOut)
def get_score(opportunity_id: uuid.UUID, session: SessionDep) -> ScoreOut:
    result = queries.detail(session, _load(session, opportunity_id))
    if result.score_detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no score computed")
    return result.score_detail


@app.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}/valuation", response_model=ValuationOut)
def get_valuation(opportunity_id: uuid.UUID, session: SessionDep) -> ValuationOut:
    result = queries.detail(session, _load(session, opportunity_id))
    if result.valuation is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no valuation: insufficient comparable evidence for this property",
        )
    return result.valuation


@app.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}/comparables")
def get_comparables(opportunity_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    """The actual comparables used, with their weights. Always available."""
    result = queries.detail(session, _load(session, opportunity_id))
    return {
        "count": len([c for c in result.comparables if c.included]),
        "excluded": len([c for c in result.comparables if not c.included]),
        "is_synthetic_evidence": result.valuation.is_synthetic_evidence
        if result.valuation
        else False,
        "comparables": [c.model_dump() for c in result.comparables],
    }


@app.get(
    f"{API_PREFIX}/opportunities/{{opportunity_id}}/provenance",
    response_model=list[ProvenanceEntry],
)
def get_provenance(opportunity_id: uuid.UUID, session: SessionDep) -> list[ProvenanceEntry]:
    return queries.provenance(session, _load(session, opportunity_id))


@app.get(f"{API_PREFIX}/admin/sources", response_model=list[SourceOut])
def list_sources(session: SessionDep) -> list[SourceOut]:
    out: list[SourceOut] = []
    for source in session.scalars(select(Source).order_by(Source.key)):
        count = session.scalar(
            select(func.count())
            .select_from(SourceRecord)
            .where(SourceRecord.source_id == source.id)
        )
        out.append(
            SourceOut(
                key=source.key,
                name=source.name,
                legal_access_method=source.legal_access_method,
                data_license=source.data_license,
                availability_label=source.availability_label,
                source_confidence=float(source.source_confidence),
                is_synthetic=source.is_synthetic,
                enabled=source.enabled,
                record_count=count or 0,
            )
        )
    return out


@app.get(f"{API_PREFIX}/admin/sources/kapsarc_rei/health", response_model=SourceHealthOut)
def kapsarc_health() -> SourceHealthOut:
    """Live check against the one CONFIRMED external source."""
    health_result = KapsarcIndexSource().health_check()
    return SourceHealthOut(
        source_key=health_result.source_key,
        healthy=health_result.healthy,
        checked_at=health_result.checked_at.isoformat(),
        latency_ms=health_result.latency_ms,
        detail=health_result.detail,
    )


# ---------------------------------------------------------------- UI


@app.get("/", response_class=HTMLResponse)
def ui_index(request: Request, session: SessionDep) -> HTMLResponse:
    opportunities = session.scalars(
        select(Opportunity).order_by(Opportunity.created_at.desc()).limit(50)
    )
    summaries = sorted(
        (queries.summarize(session, o) for o in opportunities),
        key=lambda s: (s.score is None, -(s.score or 0)),
    )
    return TEMPLATES.TemplateResponse(
        request=request, name="index.html", context={"opportunities": summaries}
    )


@app.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
def ui_detail(opportunity_id: uuid.UUID, request: Request, session: SessionDep) -> HTMLResponse:
    opportunity = _load(session, opportunity_id)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "o": queries.detail(session, opportunity),
            "provenance": queries.provenance(session, opportunity),
        },
    )
