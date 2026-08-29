"""First-party analyst / broker entry (matrix B8).

The strategic answer to the ingestion problem: because most Saudi listing
portals cannot be lawfully ingested, the MVP's opportunity supply comes from
analysts entering public Infath lots and from brokers submitting deals. It uses
the same port as every other connector, so when a licensed feed is signed
nothing downstream changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

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
from sreoi_sources.redaction import normalize_digits, redact

REQUIRED_FIELDS = ("property_class", "area_sqm", "district", "opportunity_type")


class ManualEntrySource(PropertySource):
    key = "manual_entry"
    name = "Analyst / broker submission"
    legal_access_method = LegalAccessMethod.MANUAL_UPLOAD
    data_license = "First-party, submitted under platform terms with consent"
    availability = AvailabilityLabel.CONFIRMED
    source_confidence = 0.80

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[str, dict[str, Any]] = {}

    def submit(self, external_id: str, payload: dict[str, Any]) -> SourceRef:
        self._pending[external_id] = payload
        return SourceRef(external_id=external_id)

    def discover(self, since: datetime) -> Iterator[SourceRef]:
        for external_id in list(self._pending):
            yield SourceRef(external_id=external_id)

    def fetch(self, ref: SourceRef) -> RawRecord:
        if ref.external_id not in self._pending:
            raise KeyError(f"no pending submission {ref.external_id}")
        return RawRecord(external_id=ref.external_id, payload=self._pending[ref.external_id])

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        data = dict(raw.payload)

        # Redact before anything is stored or sent to a model.
        redaction_counts = {"phone": 0, "email": 0, "national_id": 0}
        for field_name in ("description", "notes", "title"):
            if isinstance(data.get(field_name), str):
                cleaned, counts = redact(data[field_name])
                data[field_name] = cleaned
                for k, v in counts.items():
                    redaction_counts[k] += v

        for numeric in ("area_sqm", "asking_price", "seller_payment", "remaining_installments"):
            value = data.get(numeric)
            if isinstance(value, str):
                data[numeric] = normalize_digits(value).replace(",", "").strip() or None

        data["_redactions"] = redaction_counts
        return NormalizedRecord(external_id=raw.external_id, kind="opportunity", data=data)

    def validate(self, record: NormalizedRecord) -> ValidationResult:
        errors: list[str] = []
        for field_name in REQUIRED_FIELDS:
            if record.data.get(field_name) in (None, ""):
                errors.append(f"missing required field: {field_name}")

        area = record.data.get("area_sqm")
        if area is not None:
            try:
                area_f = float(area)
                # Range validation: out of range becomes an error, never clamped.
                if not 20 <= area_f <= 10_000:
                    errors.append(f"area_sqm out of plausible range: {area_f}")
            except (TypeError, ValueError):
                errors.append(f"area_sqm is not numeric: {area!r}")

        for money_field in ("asking_price", "seller_payment", "remaining_installments"):
            value = record.data.get(money_field)
            if value in (None, ""):
                continue
            try:
                amount = float(value)
                if not 0 <= amount <= 500_000_000:
                    errors.append(f"{money_field} out of plausible range: {amount}")
            except (TypeError, ValueError):
                errors.append(f"{money_field} is not numeric: {value!r}")

        return ValidationResult(ok=not errors, errors=tuple(errors))

    def health_check(self) -> SourceHealth:
        return SourceHealth(
            source_key=self.key,
            healthy=True,
            checked_at=datetime.now(UTC),
            detail=f"{len(self._pending)} pending submissions",
        )
