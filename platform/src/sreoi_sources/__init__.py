"""Source connectors. Every source declares a legal basis (ADR-008)."""

from sreoi_sources.base import (
    AvailabilityLabel,
    LegalAccessMethod,
    PropertySource,
    SourceRegistrationError,
)
from sreoi_sources.kapsarc import KapsarcIndexSource
from sreoi_sources.manual import ManualEntrySource

__all__ = [
    "AvailabilityLabel",
    "KapsarcIndexSource",
    "LegalAccessMethod",
    "ManualEntrySource",
    "PropertySource",
    "SourceRegistrationError",
]
