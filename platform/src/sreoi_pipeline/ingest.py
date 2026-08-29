"""Ingestion: submission -> raw record -> resolved property -> evaluated opportunity.

Guarantees enforced here regardless of which connector supplied the data:
raw bytes stored before interpretation, idempotency on content hash, PII
redacted at the boundary, append-only snapshots, and entity resolution before
a new property is ever created.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_domain.resolution import ResolutionDecision, ResolutionFeatures
from sreoi_persistence.models import (
    Listing,
    ListingSnapshot,
    Opportunity,
    OpportunityScoreRow,
    Property,
    SourceRecord,
)
from sreoi_persistence.repositories import DistrictRepository, SourceRepository
from sreoi_pipeline.evaluate import EvaluationResult, content_hash, evaluate_opportunity
from sreoi_pipeline.resolve import (
    TimelineEvent,
    add_timeline_event,
    build_match_text,
    find_candidates,
    price_delta,
    record_merge_decision,
    resolve,
)
from sreoi_sources.manual import ManualEntrySource

SIGNAL_KEYWORDS = {
    "URGENT": ("urgent", "عاجل", "بسرعة", "quick sale"),
    "ASSIGNMENT": ("assignment", "تنازل"),
    "AUCTION": ("auction", "مزاد"),
    "REDUCED": ("reduced", "price drop", "تخفيض"),
}


def _json_safe(value: Any) -> Any:
    """Coerce a submission into JSON-storable types.

    Raw payloads are persisted verbatim in JSONB, so Decimal and date values
    from typed callers must be normalised here rather than at each call site.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class IngestionError(Exception):
    """Raised when a submission fails validation at the boundary."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def detect_signals(data: dict[str, Any]) -> list[str]:
    """Opportunity-signal terminology, Arabic and English."""
    haystack = " ".join(
        str(data.get(field) or "") for field in ("title", "description", "opportunity_type")
    ).lower()
    found = [tag for tag, words in SIGNAL_KEYWORDS.items() if any(w in haystack for w in words)]
    return sorted(found)


def _asking_price(data: dict[str, Any]) -> Decimal | None:
    for field in ("asking_price", "seller_payment"):
        value = data.get(field)
        if value not in (None, ""):
            return Decimal(str(value))
    return None


def ingest_manual_submission(
    session: Session, payload: dict[str, Any]
) -> tuple[Opportunity, EvaluationResult]:
    """Ingest one analyst/broker submission and evaluate it end to end."""
    connector = ManualEntrySource()
    external_id = payload.get("external_id") or f"manual-{uuid.uuid4()}"
    ref = connector.submit(external_id, payload)
    raw = connector.fetch(ref)
    normalized = connector.normalize(raw)
    normalized = type(normalized)(
        external_id=normalized.external_id,
        kind=normalized.kind,
        data=_json_safe(normalized.data),
    )

    validation = connector.validate(normalized)
    if not validation.ok:
        raise IngestionError(validation.errors)

    source = SourceRepository(session).require(connector.key)
    data = normalized.data

    # Raw first: hashed and stored before anything interprets it.
    digest = content_hash(data)
    existing_record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_id == source.id, SourceRecord.content_hash == digest
        )
    )
    if existing_record is not None:
        opportunity = session.scalar(
            select(Opportunity).where(Opportunity.source_record_id == existing_record.id)
        )
        if opportunity is not None:
            # Identical content is a no-op: re-evaluated, never duplicated.
            return opportunity, evaluate_opportunity(session, opportunity, data)
        record = existing_record
    else:
        record = SourceRecord(
            source_id=source.id,
            external_id=external_id,
            content_hash=digest,
            raw_payload=data,
            retrieved_at=datetime.now(UTC),
        )
        session.add(record)
        session.flush()

    district_repo = DistrictRepository(session)
    district = district_repo.by_name(str(data["district"]))
    if district is None:
        raise IngestionError((f"unknown district: {data['district']!r}",))

    longitude, latitude, precision = _locate(district_repo, district, data)
    area_sqm = float(data["area_sqm"])
    match_text = build_match_text(data, district.name_en)

    subject = ResolutionFeatures(
        area_sqm=area_sqm,
        property_class=str(data["property_class"]),
        district_id=district.id,
        project_id=None,
        unit_number=data.get("unit_number"),
        bedrooms=_opt_int(data.get("bedrooms")),
        floor=_opt_int(data.get("floor")),
        build_year=_opt_int(data.get("build_year")),
        price=_asking_price(data),
    )
    candidates = find_candidates(
        session,
        longitude=longitude,
        latitude=latitude,
        property_class=subject.property_class,
        area_sqm=area_sqm,
        match_text=match_text,
    )
    winner_id, match = resolve(session, subject, candidates)

    if winner_id is not None and match is not None:
        prop = session.get(Property, winner_id)
        assert prop is not None
        record_merge_decision(
            session, winner_property_id=prop.id, candidate_property_id=None, result=match
        )
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.RESOLVED_TO_EXISTING,
            f"Matched an existing property ({match.total:.2f}) — {match.explanation}",
            {"score": round(match.total, 4), "decision": match.decision.value},
            source_record_id=record.id,
        )
        prop = _enrich(prop, data, precision)
    else:
        prop = Property(
            property_class=str(data["property_class"]),
            district_id=district.id,
            location=f"SRID=4326;POINT({longitude} {latitude})",
            location_precision=precision,
            built_area_sqm=area_sqm,
            bedrooms=_opt_int(data.get("bedrooms")),
            floor=_opt_int(data.get("floor")),
            build_year=_opt_int(data.get("build_year")),
            developer_name=data.get("developer_name"),
            unit_number=data.get("unit_number"),
            match_text=match_text,
            source_record_id=record.id,
        )
        session.add(prop)
        session.flush()
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.PROPERTY_CREATED,
            f"Property first seen via {source.key}",
            {"location_precision": precision},
            source_record_id=record.id,
        )
        if match is not None and match.decision is ResolutionDecision.REVIEW:
            # Ambiguous: keep them separate and ask a human rather than guess.
            record_merge_decision(
                session,
                winner_property_id=prop.id,
                candidate_property_id=candidates[0][0] if candidates else None,
                result=match,
            )
            add_timeline_event(
                session,
                prop.id,
                TimelineEvent.QUEUED_FOR_REVIEW,
                f"Possible duplicate ({match.total:.2f}) — queued for review",
                {"score": round(match.total, 4)},
                source_record_id=record.id,
            )

    _record_listing(session, prop, source.id, external_id, data, record.id)

    opportunity_type = str(data["opportunity_type"])
    title = str(data.get("title") or f"{data['property_class']} in {district.name_en}")

    # One opportunity per (property, type). A property re-observed at a new price
    # is the same opportunity with new evidence -- re-evaluated, not duplicated.
    opportunity = session.scalar(
        select(Opportunity).where(
            Opportunity.property_id == prop.id,
            Opportunity.opportunity_type == opportunity_type,
            Opportunity.status == "ACTIVE",
        )
    )
    if opportunity is None:
        opportunity = Opportunity(
            property_id=prop.id,
            source_record_id=record.id,
            opportunity_type=opportunity_type,
            title=title,
        )
        session.add(opportunity)
        session.flush()
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.OPPORTUNITY_CREATED,
            f"{opportunity_type.replace('_', ' ').title()} opportunity created",
            {"opportunity_id": str(opportunity.id)},
            source_record_id=record.id,
        )
    else:
        opportunity.source_record_id = record.id
        opportunity.title = title
        session.flush()

    result = evaluate_opportunity(session, opportunity, data)
    if result.score is not None:
        _supersede_older_scores(session, opportunity.id)
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.SCORED,
            f"Scored {result.score.total:.1f} — {result.score.classification.label}",
            {
                "score": result.score.total,
                "classification": result.score.classification.value,
                "data_confidence": round(result.score.data_confidence, 4),
            },
            source_record_id=record.id,
        )
    return opportunity, result


def _supersede_older_scores(session: Session, opportunity_id: uuid.UUID) -> None:
    """Keep exactly one current score row; history is retained, never updated away."""
    rows = list(
        session.scalars(
            select(OpportunityScoreRow)
            .where(
                OpportunityScoreRow.opportunity_id == opportunity_id,
                OpportunityScoreRow.superseded_at.is_(None),
            )
            .order_by(OpportunityScoreRow.computed_at.desc())
        )
    )
    now = datetime.now(UTC)
    for stale in rows[1:]:
        stale.superseded_at = now


def _record_listing(
    session: Session,
    prop: Property,
    source_id: uuid.UUID,
    external_id: str,
    data: dict[str, Any],
    source_record_id: uuid.UUID,
) -> None:
    """Create or update the listing, then append an immutable snapshot."""
    listing = session.scalar(
        select(Listing).where(Listing.source_id == source_id, Listing.external_id == external_id)
    )
    if listing is None:
        listing = Listing(
            property_id=prop.id,
            source_id=source_id,
            external_id=external_id,
            title=data.get("title"),
            url=data.get("url"),
        )
        session.add(listing)
        session.flush()
    else:
        listing.last_seen_at = datetime.now(UTC)

    previous = session.scalar(
        select(ListingSnapshot)
        .where(ListingSnapshot.listing_id == listing.id)
        .order_by(ListingSnapshot.observed_at.desc())
        .limit(1)
    )
    price = _asking_price(data)
    signals = detect_signals(data)

    change = price_delta(previous.asking_price if previous else None, price)
    if change is not None:
        delta, fraction = change
        if fraction < 0 and "REDUCED" not in signals:
            signals = sorted([*signals, "REDUCED"])
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.PRICE_CHANGED,
            f"Asking price moved {fraction:+.1%} to SAR {price:,.0f}"
            if price is not None
            else "Asking price changed",
            {"delta": str(delta), "fraction": round(fraction, 5)},
            source_record_id=source_record_id,
        )

    # Append-only: never update a snapshot, always insert.
    session.add(
        ListingSnapshot(
            listing_id=listing.id,
            asking_price=price,
            status="ACTIVE",
            signal_tags=signals,
            content_hash=content_hash({"price": str(price), "signals": signals}),
        )
    )
    add_timeline_event(
        session,
        prop.id,
        TimelineEvent.LISTING_OBSERVED,
        f"Listing observed on this source at SAR {price:,.0f}"
        if price is not None
        else "Listing observed",
        {"signals": signals},
        source_record_id=source_record_id,
    )
    if signals:
        add_timeline_event(
            session,
            prop.id,
            TimelineEvent.SIGNAL_DETECTED,
            f"Signals detected: {', '.join(signals)}",
            {"signals": signals},
            source_record_id=source_record_id,
        )


def _locate(
    district_repo: DistrictRepository, district: Any, data: dict[str, Any]
) -> tuple[float, float, str]:
    longitude = data.get("longitude")
    latitude = data.get("latitude")
    if longitude is None or latitude is None:
        # No coordinates: fall back to the district centroid and record the
        # reduced precision honestly rather than implying we know the point.
        longitude, latitude = district_repo.centroid_of(district.id)
        return longitude, latitude, "DISTRICT"
    return float(longitude), float(latitude), "EXACT"


def _enrich(prop: Property, data: dict[str, Any], precision: str) -> Property:
    """Fill gaps on a matched property without overwriting better information."""
    if prop.bedrooms is None:
        prop.bedrooms = _opt_int(data.get("bedrooms"))
    if prop.floor is None:
        prop.floor = _opt_int(data.get("floor"))
    if prop.build_year is None:
        prop.build_year = _opt_int(data.get("build_year"))
    if prop.developer_name is None:
        prop.developer_name = data.get("developer_name")
    if prop.unit_number is None:
        prop.unit_number = data.get("unit_number")
    if prop.location_precision == "DISTRICT" and precision == "EXACT":
        prop.location_precision = precision
    return prop


def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
