"""Source policy and boundary redaction (ADR-008, PDPL minimisation)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from sreoi_sources.base import (
    AvailabilityLabel,
    LegalAccessMethod,
    NormalizedRecord,
    PropertySource,
    RawRecord,
    SourceHealth,
    SourceRef,
    SourceRegistrationError,
)
from sreoi_sources.manual import ManualEntrySource
from sreoi_sources.redaction import normalize_digits, redact


class _Incomplete(PropertySource):
    key = "incomplete"
    name = "No legal basis declared"

    def discover(self, since: datetime) -> Iterator[SourceRef]:
        raise NotImplementedError

    def fetch(self, ref: SourceRef) -> RawRecord:
        raise NotImplementedError

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError

    def health_check(self) -> SourceHealth:
        raise NotImplementedError


class _Rejected(PropertySource):
    key = "rejected"
    name = "Explicitly out of policy"
    legal_access_method = LegalAccessMethod.PUBLIC_WEB_PERMITTED
    data_license = "n/a"
    availability = AvailabilityLabel.NOT_RECOMMENDED

    def discover(self, since: datetime) -> Iterator[SourceRef]:
        raise NotImplementedError

    def fetch(self, ref: SourceRef) -> RawRecord:
        raise NotImplementedError

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError

    def health_check(self) -> SourceHealth:
        raise NotImplementedError


def test_source_without_legal_basis_cannot_be_constructed() -> None:
    with pytest.raises(SourceRegistrationError, match="legal_access_method"):
        _Incomplete()


def test_not_recommended_source_cannot_be_constructed() -> None:
    with pytest.raises(SourceRegistrationError, match="NOT_RECOMMENDED"):
        _Rejected()


def test_manual_source_declares_its_basis() -> None:
    source = ManualEntrySource()
    assert source.legal_access_method is LegalAccessMethod.MANUAL_UPLOAD
    assert source.data_license


def test_redaction_removes_contact_details() -> None:
    text = "Urgent sale, call 0551234567 or +966 55 123 4567, email me@example.com, ID 1098765432"
    cleaned, counts = redact(text)
    assert "0551234567" not in cleaned
    assert "example.com" not in cleaned
    assert "1098765432" not in cleaned
    assert counts["phone"] >= 1
    assert counts["email"] == 1
    assert counts["national_id"] == 1


def test_redaction_keeps_the_useful_content() -> None:
    cleaned, _ = redact("Assignment in Sidrah, 140 sqm, call 0551234567")
    assert "Assignment in Sidrah" in cleaned
    assert "140" in cleaned


def test_arabic_numerals_are_normalised() -> None:
    assert normalize_digits("مساحة ١٤٠ متر") == "مساحة 140 متر"


def test_manual_validation_requires_core_fields() -> None:
    source = ManualEntrySource()
    ref = source.submit("x", {"property_class": "APARTMENT"})
    record = source.normalize(source.fetch(ref))
    result = source.validate(record)
    assert not result.ok
    assert any("area_sqm" in e for e in result.errors)
    assert any("district" in e for e in result.errors)


def test_manual_validation_rejects_out_of_range_area() -> None:
    source = ManualEntrySource()
    ref = source.submit(
        "y",
        {
            "property_class": "APARTMENT",
            "district": "Sidrah",
            "opportunity_type": "RESALE",
            "area_sqm": 99999,
        },
    )
    record = source.normalize(source.fetch(ref))
    result = source.validate(record)
    # Out of range is an error, never silently clamped.
    assert not result.ok
    assert any("out of plausible range" in e for e in result.errors)


def test_manual_normalisation_redacts_before_storage() -> None:
    source = ManualEntrySource()
    ref = source.submit(
        "z",
        {
            "property_class": "APARTMENT",
            "district": "Sidrah",
            "opportunity_type": "RESALE",
            "area_sqm": 140,
            "description": "call 0551234567",
        },
    )
    record = source.normalize(source.fetch(ref))
    assert "0551234567" not in record.data["description"]
    assert record.data["_redactions"]["phone"] == 1


def test_health_check_reports_rather_than_raises() -> None:
    health = ManualEntrySource().health_check()
    assert health.healthy
    assert health.checked_at <= datetime.now(UTC)
