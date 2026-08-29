"""Entity resolution and property timeline services."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography, Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from sreoi_domain.resolution import (
    BLOCK_AREA_TOLERANCE,
    BLOCK_RADIUS_M,
    MatchResult,
    ResolutionDecision,
    ResolutionFeatures,
    best_match,
)
from sreoi_persistence.models import (
    Listing,
    ListingSnapshot,
    Property,
    PropertyMerge,
    PropertyTimelineEvent,
)


class TimelineEvent:
    PROPERTY_CREATED = "PROPERTY_CREATED"
    LISTING_OBSERVED = "LISTING_OBSERVED"
    PRICE_CHANGED = "PRICE_CHANGED"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    RESOLVED_TO_EXISTING = "RESOLVED_TO_EXISTING"
    QUEUED_FOR_REVIEW = "QUEUED_FOR_REVIEW"
    OPPORTUNITY_CREATED = "OPPORTUNITY_CREATED"
    SCORED = "SCORED"


def build_match_text(data: dict[str, Any], district_name: str) -> str:
    """Normalised text for trigram comparison during resolution."""
    parts = [
        str(data.get("property_class", "")),
        district_name,
        str(data.get("developer_name") or ""),
        str(data.get("project_name") or ""),
        str(data.get("unit_number") or ""),
        str(data.get("title") or ""),
    ]
    return " ".join(p for p in parts if p).lower().strip()


def find_candidates(
    session: Session,
    *,
    longitude: float,
    latitude: float,
    property_class: str,
    area_sqm: float,
    match_text: str,
    exclude_id: uuid.UUID | None = None,
) -> list[tuple[uuid.UUID, ResolutionFeatures, float | None, float | None]]:
    """Blocking step: only plausible candidates are scored at all."""
    point = cast(func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326), Geography)

    # Latest observed asking price per property, so the price component compares
    # real numbers instead of a placeholder.
    latest_price = (
        select(
            Listing.property_id.label("property_id"),
            ListingSnapshot.asking_price.label("asking_price"),
            func.row_number()
            .over(
                partition_by=Listing.property_id,
                order_by=ListingSnapshot.observed_at.desc(),
            )
            .label("rn"),
        )
        .join(ListingSnapshot, ListingSnapshot.listing_id == Listing.id)
        .subquery()
    )

    stmt = (
        select(
            Property,
            func.ST_Distance(Property.location, point).label("distance_m"),
            func.similarity(func.coalesce(Property.match_text, ""), match_text).label("sim"),
            latest_price.c.asking_price,
        )
        .outerjoin(
            latest_price,
            (latest_price.c.property_id == Property.id) & (latest_price.c.rn == 1),
        )
        .where(
            Property.property_class == property_class,
            Property.merged_into_id.is_(None),
            Property.built_area_sqm >= area_sqm * (1 - BLOCK_AREA_TOLERANCE),
            Property.built_area_sqm <= area_sqm * (1 + BLOCK_AREA_TOLERANCE),
            func.ST_DWithin(Property.location, point, BLOCK_RADIUS_M),
        )
        .order_by("distance_m")
        .limit(25)
    )
    if exclude_id is not None:
        stmt = stmt.where(Property.id != exclude_id)

    out: list[tuple[uuid.UUID, ResolutionFeatures, float | None, float | None]] = []
    for prop, distance, similarity, asking_price in session.execute(stmt).all():
        out.append(
            (
                prop.id,
                ResolutionFeatures(
                    area_sqm=float(prop.built_area_sqm),
                    property_class=prop.property_class,
                    district_id=prop.district_id,
                    project_id=prop.project_id,
                    unit_number=prop.unit_number,
                    bedrooms=prop.bedrooms,
                    floor=prop.floor,
                    build_year=prop.build_year,
                    price=Decimal(asking_price) if asking_price is not None else None,
                ),
                float(distance) if distance is not None else None,
                float(similarity) if similarity is not None else None,
            )
        )
    return out


def resolve(
    session: Session,
    subject: ResolutionFeatures,
    candidates: list[tuple[uuid.UUID, ResolutionFeatures, float | None, float | None]],
) -> tuple[uuid.UUID | None, MatchResult | None]:
    """Decide whether the subject is an existing property.

    Returns the winning property id when the evidence supports a merge, and the
    match result whenever any candidate was scored (so a REVIEW decision can be
    recorded even though a new property is still created).
    """
    match = best_match(subject, candidates)
    if match is None:
        return None, None
    candidate_id, result = match
    if result.decision is ResolutionDecision.AUTO_MERGE:
        return candidate_id, result
    return None, result


def record_merge_decision(
    session: Session,
    *,
    winner_property_id: uuid.UUID,
    candidate_property_id: uuid.UUID | None,
    result: MatchResult,
    decided_by: str = "SYSTEM",
) -> PropertyMerge:
    merge = PropertyMerge(
        winner_property_id=winner_property_id,
        candidate_property_id=candidate_property_id,
        decision=result.decision.value,
        score=round(result.total, 6),
        components={
            c.name: {"score": round(c.score, 4), "weight": c.weight, "detail": c.detail}
            for c in result.components
        },
        method_version=result.method_version,
        decided_by=decided_by,
    )
    session.add(merge)
    return merge


def add_timeline_event(
    session: Session,
    property_id: uuid.UUID,
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    source_record_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> PropertyTimelineEvent:
    event = PropertyTimelineEvent(
        property_id=property_id,
        event_type=event_type,
        summary=summary,
        payload=payload or {},
        source_record_id=source_record_id,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)
    return event


def reverse_merge(session: Session, merge_id: uuid.UUID) -> bool:
    """Undo an automatic merge. Both timelines are preserved either way."""
    merge = session.get(PropertyMerge, merge_id)
    if merge is None or merge.reversed_at is not None:
        return False
    merge.reversed_at = datetime.now(UTC)
    if merge.candidate_property_id is not None:
        candidate = session.get(Property, merge.candidate_property_id)
        if candidate is not None:
            candidate.merged_into_id = None
            add_timeline_event(
                session,
                candidate.id,
                TimelineEvent.RESOLVED_TO_EXISTING,
                "Merge reversed; property restored as distinct",
                {"merge_id": str(merge_id)},
            )
    return True


def coordinates_of(session: Session, property_id: uuid.UUID) -> tuple[float, float]:
    geom = cast(Property.location, Geometry)
    row = session.execute(
        select(func.ST_X(geom), func.ST_Y(geom)).where(Property.id == property_id)
    ).one()
    return float(row[0]), float(row[1])


def price_delta(previous: Decimal | None, current: Decimal | None) -> tuple[Decimal, float] | None:
    """Absolute and proportional change between two observed asking prices."""
    if previous is None or current is None or previous <= 0:
        return None
    if previous == current:
        return None
    delta = current - previous
    return delta, float(delta / previous)
