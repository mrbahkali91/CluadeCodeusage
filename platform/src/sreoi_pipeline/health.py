"""Source health monitoring.

A silently dead connector is the failure mode that hurts most: the product
keeps serving yesterday's data as though it were current. Freshness is
therefore tracked as a first-class metric, not inferred from logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sreoi_persistence.models import Source, SourceHealthCheck, SourceRecord
from sreoi_sources.base import PropertySource
from sreoi_sources.kapsarc import KapsarcIndexSource
from sreoi_sources.manual import ManualEntrySource

# Beyond this a source is stale even if its last check passed.
FRESHNESS_BUDGET = {
    "kapsarc_rei": timedelta(days=100),  # quarterly series
    "manual_entry": timedelta(days=7),
    "synthetic_fixture": timedelta(days=3650),
}
DEFAULT_FRESHNESS = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    key: str
    name: str
    enabled: bool
    healthy: bool | None
    latency_ms: float | None
    detail: str | None
    checked_at: datetime | None
    last_record_at: datetime | None
    record_count: int
    is_stale: bool
    legal_access_method: str
    data_license: str
    availability_label: str
    is_synthetic: bool

    @property
    def state(self) -> str:
        """A single word for the dashboard, ordered by severity."""
        if not self.enabled:
            return "DISABLED"
        if self.healthy is False:
            return "FAILING"
        if self.is_stale:
            return "STALE"
        if self.healthy is None:
            return "UNKNOWN"
        return "HEALTHY"


def connectors() -> list[PropertySource]:
    """Connectors that can be probed live."""
    return [KapsarcIndexSource(), ManualEntrySource()]


def run_health_checks(session: Session) -> list[SourceHealthCheck]:
    """Probe every live connector and persist the outcome."""
    checks: list[SourceHealthCheck] = []
    for connector in connectors():
        source = session.scalar(select(Source).where(Source.key == connector.key))
        if source is None:
            continue
        result = connector.health_check()
        check = SourceHealthCheck(
            source_id=source.id,
            healthy=result.healthy,
            latency_ms=result.latency_ms,
            detail=result.detail,
            checked_at=result.checked_at,
        )
        session.add(check)
        checks.append(check)
    return checks


def source_statuses(session: Session) -> list[SourceStatus]:
    """Current state of every registered source, for the admin dashboard."""
    now = datetime.now(UTC)
    statuses: list[SourceStatus] = []

    for source in session.scalars(select(Source).order_by(Source.key)):
        latest = session.scalar(
            select(SourceHealthCheck)
            .where(SourceHealthCheck.source_id == source.id)
            .order_by(SourceHealthCheck.checked_at.desc())
            .limit(1)
        )
        last_record_at = session.scalar(
            select(func.max(SourceRecord.retrieved_at)).where(SourceRecord.source_id == source.id)
        )
        count = session.scalar(
            select(func.count())
            .select_from(SourceRecord)
            .where(SourceRecord.source_id == source.id)
        )

        budget = FRESHNESS_BUDGET.get(source.key, DEFAULT_FRESHNESS)
        reference = last_record_at or (latest.checked_at if latest else None)
        is_stale = reference is None or (now - reference) > budget

        statuses.append(
            SourceStatus(
                key=source.key,
                name=source.name,
                enabled=source.enabled,
                healthy=latest.healthy if latest else None,
                latency_ms=float(latest.latency_ms)
                if latest and latest.latency_ms is not None
                else None,
                detail=latest.detail if latest else None,
                checked_at=latest.checked_at if latest else None,
                last_record_at=last_record_at,
                record_count=int(count or 0),
                is_stale=is_stale,
                legal_access_method=source.legal_access_method,
                data_license=source.data_license,
                availability_label=source.availability_label,
                is_synthetic=source.is_synthetic,
            )
        )
    return statuses
