"""Seed reference data and a demonstration comparable corpus.

IMPORTANT -- the transaction corpus generated here is SYNTHETIC. It is
deterministic fixture data for exercising the engine, and it is registered
under a source explicitly flagged `is_synthetic=True` so that every valuation
derived from it is visibly labelled in the API and the UI.

Presenting fabricated transactions as real registered sales is precisely the
failure this platform exists to prevent. The synthetic source is replaced by
the Ministry of Justice open-data loader once Assumption A-01 is validated
(see docs/product/risk-register.md).
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import (
    District,
    PriceIndexPoint,
    Source,
    SourceRecord,
    Transaction,
)
from sreoi_sources.base import AvailabilityLabel, LegalAccessMethod
from sreoi_sources.kapsarc import KapsarcIndexSource

SYNTHETIC_SOURCE_KEY = "synthetic_fixture"

# Approximate district centroids in east Riyadh, entered by an analyst.
# Recorded as APPROXIMATE: authoritative boundaries come from the geospatial
# import in Slice 2 (matrix A12).
RIYADH_DISTRICTS: tuple[tuple[str, str, float, float, float, float], ...] = (
    # name_en, name_ar, lon, lat, liquidity, location
    ("Qurtubah", "قرطبة", 46.7600, 24.8200, 88.0, 90.0),
    ("Al Munsiyah", "المونسية", 46.7900, 24.8300, 84.0, 86.0),
    ("Al Rimal", "الرمال", 46.8300, 24.8450, 78.0, 80.0),
    ("Sidrah", "سدرة", 46.8500, 24.8700, 82.0, 88.0),
)

# Plausible Riyadh apartment price levels per district (SAR/m²), used only to
# generate the synthetic corpus.
_BASE_PPSQM = {"Qurtubah": 7100.0, "Al Munsiyah": 6400.0, "Al Rimal": 5800.0, "Sidrah": 6600.0}


def _point(lon: float, lat: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def seed_sources(session: Session) -> dict[str, Source]:
    wanted: list[dict[str, Any]] = [
        {
            "key": SYNTHETIC_SOURCE_KEY,
            "name": "Synthetic comparable corpus (DEMONSTRATION DATA)",
            "legal_access_method": LegalAccessMethod.MANUAL_UPLOAD.value,
            "data_license": "SYNTHETIC — generated fixture, NOT real transaction data",
            "availability_label": AvailabilityLabel.CONFIRMED.value,
            "source_confidence": 0.40,
            "is_synthetic": True,
        },
        {
            "key": "manual_entry",
            "name": "Analyst / broker submission",
            "legal_access_method": LegalAccessMethod.MANUAL_UPLOAD.value,
            "data_license": "First-party, submitted under platform terms with consent",
            "availability_label": AvailabilityLabel.CONFIRMED.value,
            "source_confidence": 0.80,
            "is_synthetic": False,
        },
        {
            "key": "kapsarc_rei",
            "name": "KAPSARC / GASTAT Real Estate Price Index",
            "legal_access_method": LegalAccessMethod.OPEN_DATA.value,
            "data_license": "KAPSARC open data (attribution)",
            "availability_label": AvailabilityLabel.CONFIRMED.value,
            "source_confidence": 0.95,
            "is_synthetic": False,
        },
    ]
    out: dict[str, Source] = {}
    for spec in wanted:
        key = str(spec["key"])
        existing = session.scalar(select(Source).where(Source.key == key))
        if existing is None:
            existing = Source(**spec)
            session.add(existing)
            session.flush()
        out[key] = existing
    return out


def seed_districts(session: Session) -> dict[str, District]:
    out: dict[str, District] = {}
    for name_en, name_ar, lon, lat, liquidity, location in RIYADH_DISTRICTS:
        existing = session.scalar(select(District).where(District.name_en == name_en))
        if existing is None:
            d = 0.018  # ~2 km half-width, approximate
            boundary = (
                f"SRID=4326;POLYGON(({lon - d} {lat - d}, {lon + d} {lat - d}, "
                f"{lon + d} {lat + d}, {lon - d} {lat + d}, {lon - d} {lat - d}))"
            )
            existing = District(
                city="Riyadh",
                name_en=name_en,
                name_ar=name_ar,
                centroid=_point(lon, lat),
                boundary=boundary,
                boundary_precision="APPROXIMATE",
                liquidity_score=liquidity,
                location_score=location,
            )
            session.add(existing)
            session.flush()
        out[name_en] = existing
    return out


def seed_price_index(session: Session, source: Source, *, live: bool = True) -> int:
    """Pull the real KAPSARC index. This source is CONFIRMED and live."""
    if not live:
        return 0
    connector = KapsarcIndexSource()
    ref = next(connector.discover(datetime.now(UTC)))
    raw = connector.fetch(ref, limit=1000)
    record = connector.normalize(raw)
    result = connector.validate(record)
    if not result.ok:
        raise RuntimeError(f"KAPSARC index failed validation: {result.errors}")

    # Raw-first: persist the original payload before interpreting it, exactly as
    # the manual and connector paths do.
    digest = hashlib.sha256(
        json.dumps(raw.payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    if not session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_id == source.id, SourceRecord.content_hash == digest
        )
    ):
        session.add(
            SourceRecord(
                source_id=source.id,
                external_id=ref.external_id,
                content_hash=digest,
                raw_payload={"total_count": raw.payload.get("total_count")},
                url=raw.url,
                retrieved_at=raw.retrieved_at,
            )
        )
        session.flush()

    inserted = 0
    for point in record.data["points"]:
        exists = session.scalar(
            select(PriceIndexPoint).where(
                PriceIndexPoint.tier == "NATIONAL",
                PriceIndexPoint.scope == "Saudi Arabia",
                PriceIndexPoint.sector == point["sector"],
                PriceIndexPoint.period == point["period"],
            )
        )
        if exists:
            continue
        session.add(
            PriceIndexPoint(
                source_id=source.id,
                tier="NATIONAL",
                scope="Saudi Arabia",
                sector=point["sector"],
                period=point["period"],
                value=point["value"],
            )
        )
        inserted += 1
    return inserted


def seed_transactions(
    session: Session,
    source: Source,
    districts: dict[str, District],
    *,
    per_district: int = 60,
    seed: int = 20260829,
) -> int:
    """Generate a deterministic SYNTHETIC comparable corpus."""
    existing = session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.source_id == source.id)
    )
    if existing:
        return 0

    rng = random.Random(seed)
    today = date.today()
    count = 0

    # The generated corpus is itself a source record, so its provenance and
    # freshness are visible on the admin dashboard like any other source.
    manifest = {
        "generator": "seed_transactions",
        "seed": seed,
        "per_district": per_district,
        "districts": sorted(districts),
        "synthetic": True,
    }
    session.add(
        SourceRecord(
            source_id=source.id,
            external_id=f"synthetic-corpus-{seed}",
            content_hash=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            raw_payload=manifest,
        )
    )
    session.flush()

    for name_en, _, lon, lat, _, _ in RIYADH_DISTRICTS:
        district = districts[name_en]
        base = _BASE_PPSQM[name_en]
        for _ in range(per_district):
            days_ago = rng.randint(20, 700)
            transacted = today - timedelta(days=days_ago)
            # Gentle upward drift plus dispersion.
            drift = 1.0 - (days_ago / 365.0) * 0.045
            ppsqm = base * drift * rng.gauss(1.0, 0.11)
            ppsqm = max(2500.0, ppsqm)
            area = round(rng.uniform(95, 210), 1)
            offset = 0.014
            t_lon = lon + rng.uniform(-offset, offset)
            t_lat = lat + rng.uniform(-offset, offset)
            session.add(
                Transaction(
                    source_id=source.id,
                    district_id=district.id,
                    location=_point(t_lon, t_lat),
                    price=Decimal(str(round(ppsqm * area, 2))),
                    area_sqm=area,
                    transacted_on=transacted,
                    property_class="APARTMENT",
                    build_year=rng.randint(2012, 2024),
                    floor=rng.randint(1, 12),
                )
            )
            count += 1
    return count


def seed_all(session: Session, *, live_index: bool = True) -> dict[str, int]:
    sources = seed_sources(session)
    districts = seed_districts(session)
    index_points = seed_price_index(session, sources["kapsarc_rei"], live=live_index)
    transactions = seed_transactions(session, sources[SYNTHETIC_SOURCE_KEY], districts)

    # Rental evidence. Imported here rather than at module scope to keep the
    # import graph of the evaluation path free of this module.
    from sreoi_pipeline.rental import seed_rental_comparables

    leases = seed_rental_comparables(session)

    session.flush()
    return {
        "sources": len(sources),
        "districts": len(districts),
        "index_points": index_points,
        "synthetic_transactions": transactions,
        "synthetic_leases": leases,
    }
