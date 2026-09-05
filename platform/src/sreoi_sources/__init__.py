"""Source connectors. Every source declares a legal basis (ADR-008)."""

from sreoi_sources.base import (
    AvailabilityLabel,
    LegalAccessMethod,
    PropertySource,
    SourceRegistrationError,
)
from sreoi_sources.kapsarc import KapsarcIndexSource
from sreoi_sources.manual import ManualEntrySource
from sreoi_sources.opendata import OpenDataSchemaError, OpenDataTransactionSource

__all__ = [
    "AvailabilityLabel",
    "KapsarcIndexSource",
    "LegalAccessMethod",
    "ManualEntrySource",
    "OpenDataSchemaError",
    "OpenDataTransactionSource",
    "PropertySource",
    "SourceRegistrationError",
]
