"""Persist an Open Data transaction batch.

Kept separate from the connector so the connector stays free of database
concerns and remains testable without one. The field mapping the connector
resolved is stored on the source record: a batch whose provenance says which
column became `price` can be audited later, and one that does not cannot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import District, Source, SourceRecord, Transaction
from sreoi_sources.base import NormalizedRecord
from sreoi_sources.opendata import OpenDataTransactionSource


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%Y-%m", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10] if len(text) >= 10 else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What the batch actually did, including what it could not do.

    Returned rather than printed so the caller decides how to surface it, and
    so `located_by_district_centroid` cannot be dropped on the floor: a run
    where every row was placed at a centroid is a materially different dataset
    from one with surveyed coordinates.
    """

    written: int
    located_by_district_centroid: int
    skipped_no_location: int
    skipped_unparseable_date: int


def store_transactions(session: Session, record: NormalizedRecord) -> IngestOutcome:
    """Insert the batch and report exactly what happened to every row."""
    connector = OpenDataTransactionSource(dataset=record.external_id)
    source = session.scalar(select(Source).where(Source.key == connector.key))
    if source is None:
        source = Source(
            key=connector.key,
            name=connector.name,
            legal_access_method=connector.legal_access_method.value,
            data_license=connector.data_license,
            availability_label=connector.availability.value,
            source_confidence=Decimal(str(connector.source_confidence)),
            is_synthetic=False,
            enabled=True,
        )
        session.add(source)
        session.flush()

    manifest = {
        "dataset": record.external_id,
        "source_path": record.data.get("source_path"),
        "field_mapping": record.data.get("field_mapping"),
        "observed_fields": record.data.get("observed_fields"),
        "skipped_incomplete": record.data.get("skipped_incomplete"),
    }
    session.add(
        SourceRecord(
            source_id=source.id,
            external_id=f"opendata:{record.external_id}",
            content_hash=hashlib.sha256(
                json.dumps(manifest, sort_keys=True, default=str).encode()
            ).hexdigest(),
            raw_payload=manifest,
        )
    )
    session.flush()

    all_districts = list(session.scalars(select(District)).all())
    districts: dict[str, District] = {}
    for d in all_districts:
        districts[d.name_en.strip().lower()] = d
        districts[d.name_ar.strip()] = d

    # `transactions.location` is NOT NULL, and open-data transaction extracts
    # commonly carry a district name with no coordinates. Two wrong answers
    # were available: drop those rows, which throws away most of the dataset;
    # or quietly plant a point and let the comparable-distance weighting treat
    # a district-level record as if it were surveyed. Instead the district
    # centroid is used AND the substitution is counted, reported by the CLI,
    # stored in the batch manifest, and raised as a validation finding --
    # because every comparable located this way has a distance that is an
    # artefact of the centroid, not a measurement.
    # Read back as WKT so the value is a plain string the ORM can bind, rather
    # than the driver's geography object.
    centroids: dict[Any, str | None] = {
        d.id: session.scalar(
            select(func.st_asewkt(cast(District.centroid, Geometry))).where(District.id == d.id)
        )
        for d in all_districts
    }

    written = 0
    by_centroid = 0
    no_location = 0
    unparseable_date = 0
    for row in record.data.get("transactions") or []:
        transacted = _parse_date(row.get("transacted_on"))
        if transacted is None:
            # A comparable weighted by recency must not borrow a date it never
            # had, so this is skipped rather than dated today.
            unparseable_date += 1
            continue
        district = districts.get(str(row.get("district") or "").strip().lower()) or districts.get(
            str(row.get("district") or "").strip()
        )
        location = None
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is not None and lon is not None:
            location = f"SRID=4326;POINT({lon} {lat})"
        elif district is not None and centroids.get(district.id) is not None:
            location = centroids[district.id]
            by_centroid += 1
        if location is None:
            no_location += 1
            continue
        session.add(
            Transaction(
                source_id=source.id,
                district_id=district.id if district else None,
                location=location,
                price=Decimal(str(round(float(row["price"]), 2))),
                area_sqm=float(row["area_sqm"]),
                transacted_on=transacted,
                property_class=str(row["property_class"]),
            )
        )
        written += 1

    return IngestOutcome(
        written=written,
        located_by_district_centroid=by_centroid,
        skipped_no_location=no_location,
        skipped_unparseable_date=unparseable_date,
    )
