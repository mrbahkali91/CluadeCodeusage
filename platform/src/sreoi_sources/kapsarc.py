"""KAPSARC / GASTAT real-estate price index connector.

The only CONFIRMED source in the matrix: verified live, unauthenticated,
Opendatasoft Explore API v2.1. This supplies the time-adjustment index without
which every "discount" figure is wrong by an unknown amount.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from sreoi_sources.base import (
    AvailabilityLabel,
    LegalAccessMethod,
    NormalizedRecord,
    PropertySource,
    RawRecord,
    SourceHealth,
    SourceRef,
    ValidationResult,
)

BASE_URL = "https://data.kapsarc.org/api/explore/v2.1/catalog/datasets"
DATASET = "real-estate-price-index-by-sector-2023-100"
PAGE_LIMIT = 100  # Opendatasoft caps a single page at 100 records
MAX_OFFSET = 10_000


class KapsarcIndexSource(PropertySource):
    key = "kapsarc_rei"
    name = "KAPSARC / GASTAT Real Estate Price Index"
    legal_access_method = LegalAccessMethod.OPEN_DATA
    data_license = "KAPSARC open data (attribution; confirm commercial redistribution terms)"
    availability = AvailabilityLabel.CONFIRMED
    source_confidence = 0.95

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        super().__init__()
        self._client = client or httpx.Client(timeout=timeout)

    def discover(self, since: datetime) -> Iterator[SourceRef]:
        yield SourceRef(external_id=DATASET, url=f"{BASE_URL}/{DATASET}/records")

    def fetch(self, ref: SourceRef, limit: int = 100) -> RawRecord:
        """Fetch index records, paginating because the API caps a page at 100."""
        url = f"{BASE_URL}/{ref.external_id}/records"
        results: list[dict[str, Any]] = []
        total_count = 0
        offset = 0

        while len(results) < limit:
            page_size = min(PAGE_LIMIT, limit - len(results))
            response = self._client.get(
                url,
                params={"limit": page_size, "offset": offset, "order_by": "date desc"},
            )
            response.raise_for_status()
            body = response.json()
            total_count = body.get("total_count", 0)
            page = body.get("results", [])
            results.extend(page)
            offset += len(page)
            if not page or offset >= total_count or offset >= MAX_OFFSET:
                break

        return RawRecord(
            external_id=ref.external_id,
            payload={"total_count": total_count, "results": results},
            url=url,
        )

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        rows: list[dict[str, Any]] = []
        for row in raw.payload.get("results", []):
            if row.get("measure") != "Index":
                continue  # QoQ / YoY rows are derived, we store the level
            if row.get("date") is None or row.get("value") is None:
                continue
            rows.append(
                {
                    "sector": row.get("sector"),
                    "period": row["date"],  # "YYYY-MM"
                    "value": float(row["value"]),
                    "periodicity": row.get("periodicity"),
                }
            )
        return NormalizedRecord(
            external_id=raw.external_id, kind="price_index", data={"points": rows}
        )

    def validate(self, record: NormalizedRecord) -> ValidationResult:
        points = record.data.get("points", [])
        errors: list[str] = []
        if not points:
            errors.append("no index points returned")
        for p in points:
            if not 0 < p["value"] < 1000:
                errors.append(f"index value out of range: {p['value']}")
        return ValidationResult(ok=not errors, errors=tuple(errors))

    def health_check(self) -> SourceHealth:
        started = datetime.now(UTC)
        try:
            response = self._client.get(
                f"{BASE_URL}/{DATASET}/records", params={"limit": 1}, timeout=15.0
            )
            response.raise_for_status()
            elapsed = (datetime.now(UTC) - started).total_seconds() * 1000
            total = response.json().get("total_count", 0)
            return SourceHealth(
                source_key=self.key,
                healthy=True,
                checked_at=started,
                latency_ms=elapsed,
                detail=f"{total} records available",
            )
        except Exception as exc:
            return SourceHealth(
                source_key=self.key, healthy=False, checked_at=started, detail=str(exc)
            )
