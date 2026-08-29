"""Ingestion service: submission -> raw record -> property -> evaluated opportunity."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_persistence.models import Opportunity, Property, SourceRecord
from sreoi_persistence.repositories import DistrictRepository, SourceRepository
from sreoi_pipeline.evaluate import EvaluationResult, content_hash, evaluate_opportunity
from sreoi_sources.manual import ManualEntrySource


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

    # Raw first: the payload is hashed and stored before anything interprets it.
    digest = content_hash(normalized.data)
    existing = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_id == source.id, SourceRecord.content_hash == digest
        )
    )
    if existing is not None:
        opportunity = session.scalar(
            select(Opportunity).where(Opportunity.source_record_id == existing.id)
        )
        if opportunity is not None:
            # Idempotent: identical content is a no-op, re-evaluated not duplicated.
            return opportunity, evaluate_opportunity(session, opportunity, normalized.data)
        record = existing
    else:
        record = SourceRecord(
            source_id=source.id,
            external_id=external_id,
            content_hash=digest,
            raw_payload=normalized.data,
            retrieved_at=datetime.now(UTC),
        )
        session.add(record)
        session.flush()

    data = normalized.data
    district_repo = DistrictRepository(session)
    district = district_repo.by_name(str(data["district"]))
    if district is None:
        raise IngestionError((f"unknown district: {data['district']!r}",))

    longitude = data.get("longitude")
    latitude = data.get("latitude")
    if longitude is None or latitude is None:
        # No coordinates supplied: fall back to the district centroid and record
        # the reduced precision honestly rather than implying we know the point.
        longitude, latitude = district_repo.centroid_of(district.id)
        precision = "DISTRICT"
    else:
        longitude, latitude = float(longitude), float(latitude)
        precision = "EXACT"

    prop = Property(
        property_class=str(data["property_class"]),
        district_id=district.id,
        location=f"SRID=4326;POINT({longitude} {latitude})",
        location_precision=precision,
        built_area_sqm=float(data["area_sqm"]),
        bedrooms=_opt_int(data.get("bedrooms")),
        floor=_opt_int(data.get("floor")),
        build_year=_opt_int(data.get("build_year")),
        developer_name=data.get("developer_name"),
        source_record_id=record.id,
    )
    session.add(prop)
    session.flush()

    opportunity = Opportunity(
        property_id=prop.id,
        source_record_id=record.id,
        opportunity_type=str(data["opportunity_type"]),
        title=str(data.get("title") or f"{data['property_class']} in {district.name_en}"),
    )
    session.add(opportunity)
    session.flush()

    result = evaluate_opportunity(session, opportunity, data)
    return opportunity, result


def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
