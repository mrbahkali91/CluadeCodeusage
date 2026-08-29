"""FastAPI application.

The HTTP layer only translates: it validates input, calls a pipeline service or
a read model, and serialises. No business logic lives here (ADR-001).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_api import queries
from sreoi_api.i18n import (
    direction,
    format_number,
    localise_digits,
    normalise_locale,
    translator,
)
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
from sreoi_api.search import (
    SORTS,
    OpportunityFilters,
    district_layer,
    district_metrics,
    geojson,
    search,
)
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models import (
    District,
    Opportunity,
    PropertyMerge,
    PropertyTimelineEvent,
    Source,
    SourceRecord,
)
from sreoi_pipeline.health import run_health_checks, source_statuses
from sreoi_pipeline.ingest import IngestionError, ingest_manual_submission
from sreoi_sources.kapsarc import KapsarcIndexSource

API_PREFIX = "/api/v1"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Saudi Real Estate Opportunity Intelligence",
    version="0.1.0",
    description=(
        "Slice 1 — analyst entry to scored opportunity. Money numbers are "
        "deterministic and reproducible; no LLM participates in any calculation."
    ),
)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


def _filters(
    district: Annotated[list[str] | None, Query()] = None,
    opportunity_type: Annotated[list[str] | None, Query()] = None,
    property_class: str | None = None,
    max_cost: float | None = None,
    min_discount: float | None = None,
    min_score: float | None = None,
    hide_insufficient: bool = False,
    bbox: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float | None = None,
    sort: str = "score",
    limit: int = 100,
) -> OpportunityFilters:
    parsed_bbox: tuple[float, float, float, float] | None = None
    if bbox:
        parts = [float(p) for p in bbox.split(",")]
        if len(parts) != 4:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "bbox must be west,south,east,north"
            )
        parsed_bbox = (parts[0], parts[1], parts[2], parts[3])

    return OpportunityFilters(
        districts=district or [],
        opportunity_types=opportunity_type or [],
        property_class=property_class,
        max_cost=Decimal(str(max_cost)) if max_cost is not None else None,
        min_discount_pct=min_discount,
        min_score=min_score,
        include_insufficient=not hide_insufficient,
        bbox=parsed_bbox,
        centre=(lon, lat) if lon is not None and lat is not None else None,
        radius_m=radius_m,
        sort=sort if sort in SORTS else "score",
        limit=max(1, min(limit, 500)),
    )


FilterDep = Annotated[OpportunityFilters, Depends(_filters)]


@app.get(f"{API_PREFIX}/search/opportunities")
def search_opportunities(filters: FilterDep, session: SessionDep) -> dict[str, Any]:
    """Filtered search. The applied filters are echoed back so they stay visible."""
    results = search(session, filters)
    return {
        "count": len(results),
        "filters_applied": filters.describe(),
        "results": [
            {
                **r,
                "id": str(r["id"]),
                "true_acquisition_cost": float(r["true_acquisition_cost"])
                if r["true_acquisition_cost"] is not None
                else None,
                "fair_value_base": float(r["fair_value_base"])
                if r["fair_value_base"] is not None
                else None,
            }
            for r in results
        ],
    }


@app.get(f"{API_PREFIX}/map/opportunities")
def map_opportunities(filters: FilterDep, session: SessionDep) -> dict[str, Any]:
    """GeoJSON for the map, using the same filters as the list."""
    return geojson(session, filters)


@app.get(f"{API_PREFIX}/map/districts")
def map_districts(session: SessionDep) -> dict[str, Any]:
    return district_layer(session)


@app.get(f"{API_PREFIX}/market/districts/{{district_id}}")
def market_district(district_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    metrics = district_metrics(session, district_id)
    if metrics is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "district not found")
    return metrics


@app.get(f"{API_PREFIX}/opportunities/{{opportunity_id}}/timeline")
def get_timeline(opportunity_id: uuid.UUID, session: SessionDep) -> list[dict[str, Any]]:
    """Full history. Snapshots are append-only, so this is never rewritten."""
    opportunity = _load(session, opportunity_id)
    events = session.scalars(
        select(PropertyTimelineEvent)
        .where(PropertyTimelineEvent.property_id == opportunity.property_id)
        .order_by(PropertyTimelineEvent.occurred_at)
    )
    return [
        {
            "event_type": e.event_type,
            "summary": e.summary,
            "payload": e.payload,
            "occurred_at": e.occurred_at.isoformat(),
        }
        for e in events
    ]


@app.get(f"{API_PREFIX}/admin/resolution")
def resolution_decisions(session: SessionDep, limit: int = 100) -> list[dict[str, Any]]:
    """Entity-resolution decisions, including the human-review queue."""
    rows = session.scalars(
        select(PropertyMerge).order_by(PropertyMerge.decided_at.desc()).limit(limit)
    )
    return [
        {
            "id": str(m.id),
            "decision": m.decision,
            "score": float(m.score),
            "winner_property_id": str(m.winner_property_id),
            "candidate_property_id": str(m.candidate_property_id)
            if m.candidate_property_id
            else None,
            "components": m.components,
            "method_version": m.method_version,
            "decided_by": m.decided_by,
            "decided_at": m.decided_at.isoformat(),
            "reversed_at": m.reversed_at.isoformat() if m.reversed_at else None,
        }
        for m in rows
    ]


@app.get(f"{API_PREFIX}/admin/health")
def admin_health(session: SessionDep) -> list[dict[str, Any]]:
    return [
        {
            "key": s.key,
            "name": s.name,
            "state": s.state,
            "healthy": s.healthy,
            "latency_ms": s.latency_ms,
            "detail": s.detail,
            "is_stale": s.is_stale,
            "record_count": s.record_count,
            "checked_at": s.checked_at.isoformat() if s.checked_at else None,
            "last_record_at": s.last_record_at.isoformat() if s.last_record_at else None,
            "legal_access_method": s.legal_access_method,
            "data_license": s.data_license,
            "availability_label": s.availability_label,
            "is_synthetic": s.is_synthetic,
        }
        for s in source_statuses(session)
    ]


@app.post(f"{API_PREFIX}/admin/health/run", status_code=202)
def admin_health_run(session: SessionDep) -> dict[str, Any]:
    checks = run_health_checks(session)
    return {"checks_run": len(checks)}


# ---------------------------------------------------------------- UI


def _ui_context(request: Request) -> dict[str, Any]:
    locale = normalise_locale(request.query_params.get("lang"))
    return {
        "locale": locale,
        "dir": direction(locale),
        "t": translator(locale),
        "num": lambda v, d=0: format_number(v, locale, d),
        "digits": lambda s: localise_digits(str(s), locale),
        "other_locale": "en" if locale == "ar" else "ar",
        "query": request.query_params,
    }


@app.get("/", response_class=HTMLResponse)
def ui_index(request: Request, session: SessionDep, filters: FilterDep) -> HTMLResponse:
    results = search(session, filters)
    districts = session.scalars(select(District).order_by(District.name_en)).all()
    types = session.scalars(
        select(Opportunity.opportunity_type).distinct().order_by(Opportunity.opportunity_type)
    ).all()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **_ui_context(request),
            "opportunities": results,
            "districts": districts,
            "types": types,
            "filters": filters,
            "filters_applied": filters.describe(),
            "sorts": SORTS,
        },
    )


@app.get("/map", response_class=HTMLResponse)
def ui_map(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request, name="map.html", context=_ui_context(request)
    )


@app.get("/admin/sources", response_class=HTMLResponse)
def ui_admin(request: Request, session: SessionDep) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request,
        name="admin.html",
        context={**_ui_context(request), "statuses": source_statuses(session)},
    )


@app.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
def ui_detail(opportunity_id: uuid.UUID, request: Request, session: SessionDep) -> HTMLResponse:
    opportunity = _load(session, opportunity_id)
    events = session.scalars(
        select(PropertyTimelineEvent)
        .where(PropertyTimelineEvent.property_id == opportunity.property_id)
        .order_by(PropertyTimelineEvent.occurred_at.desc())
    ).all()
    return TEMPLATES.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            **_ui_context(request),
            "o": queries.detail(session, opportunity),
            "provenance": queries.provenance(session, opportunity),
            "timeline": events,
        },
    )
