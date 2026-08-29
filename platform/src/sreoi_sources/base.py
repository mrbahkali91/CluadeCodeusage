"""The PropertySource port (ADR-008).

Every source -- an official API, an open-data download, a licensed feed, or an
analyst typing an auction lot into a form -- implements this one interface, so
nothing downstream knows the difference. That is what lets the MVP run on
first-party supply while partnerships are negotiated.

A source cannot be constructed without a recorded legal basis. Compliance is a
build-time property here, not a code-review checklist item.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# Hosts we have explicitly decided not to ingest. Referencing one from a
# connector fails the policy test in CI. See docs/data-sources/matrix.md.
DENIED_HOSTS: frozenset[str] = frozenset(
    {
        "sa.aqar.fm",
        "aqar.fm",
        "haraj.com.sa",
        "bayut.sa",
        "bayutapi.com",
    }
)


class LegalAccessMethod(StrEnum):
    OFFICIAL_API = "OFFICIAL_API"
    OPEN_DATA = "OPEN_DATA"
    PUBLIC_WEB_PERMITTED = "PUBLIC_WEB_PERMITTED"
    LICENSED_API = "LICENSED_API"
    PARTNERSHIP = "PARTNERSHIP"
    USER_AUTHORIZED = "USER_AUTHORIZED"
    MANUAL_UPLOAD = "MANUAL_UPLOAD"


class AvailabilityLabel(StrEnum):
    CONFIRMED = "CONFIRMED"
    REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
    PARTNERSHIP_REQUIRED = "PARTNERSHIP_REQUIRED"
    NOT_RECOMMENDED = "NOT_RECOMMENDED"


@dataclass(frozen=True, slots=True)
class SourceRef:
    external_id: str
    url: str | None = None
    hint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawRecord:
    """Original bytes, stored before anything interprets them."""

    external_id: str
    payload: dict[str, Any]
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    url: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    external_id: str
    kind: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_key: str
    healthy: bool
    checked_at: datetime
    latency_ms: float | None = None
    detail: str | None = None


class SourceRegistrationError(RuntimeError):
    """Raised when a source violates the ingestion policy."""


class PropertySource(abc.ABC):
    """Base class for every connector."""

    key: str
    name: str
    legal_access_method: LegalAccessMethod
    data_license: str
    availability: AvailabilityLabel
    source_confidence: float = 0.5

    def __init__(self) -> None:
        # Enforced at construction: a connector with no recorded legal basis
        # cannot exist, so it cannot be enabled by accident.
        for attr in ("key", "name", "legal_access_method", "data_license", "availability"):
            if not getattr(self, attr, None):
                raise SourceRegistrationError(
                    f"{type(self).__name__} must declare `{attr}` before it can be used "
                    "(ADR-008: source ingestion policy)"
                )
        if self.availability is AvailabilityLabel.NOT_RECOMMENDED:
            raise SourceRegistrationError(
                f"{type(self).__name__} is labelled NOT_RECOMMENDED and must not be ingested"
            )

    @abc.abstractmethod
    def discover(self, since: datetime) -> Iterator[SourceRef]: ...

    @abc.abstractmethod
    def fetch(self, ref: SourceRef) -> RawRecord: ...

    @abc.abstractmethod
    def normalize(self, raw: RawRecord) -> NormalizedRecord: ...

    def validate(self, record: NormalizedRecord) -> ValidationResult:
        return ValidationResult(ok=True)

    @abc.abstractmethod
    def health_check(self) -> SourceHealth: ...
