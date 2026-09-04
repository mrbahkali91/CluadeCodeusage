"""Watchlists, watch rules, alerts and notifications (Slice 3).

Three structural decisions rather than conventions:

  * **Alerts are append-only.** An alert records that at a moment in time a
    named rule at a named version fired for a stated reason. Editing that row
    later would destroy the only evidence of why the user was interrupted.
  * **Every alert carries a reason, enforced by a CHECK constraint.** An alert
    that cannot say why it fired is not an alert, it is noise.
  * **Deduplication is a unique index, not application discipline.** Alert
    fatigue is a defect, so the database refuses the duplicate even if a
    concurrent scan tries to insert one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreoi_persistence.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class Watchlist(Base):
    """A saved monitoring intent belonging to one user."""

    __tablename__ = "watchlists"
    # Tenant key. Application filters AND row-level security both use it;
    # the RLS policy is the backstop for a query that forgets the filter.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        # Server default = the bootstrap organisation, so write paths that
        # predate tenancy keep working and the column is never null. A
        # multi-tenant caller MUST pass this explicitly; row-level security
        # rejects a row whose tenant does not match the bound one.
        server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    __table_args__ = (Index("ix_watchlists_owner", "owner_ref"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # An opaque reference to the owner. Deliberately not an email column: PDPL
    # posture is to keep contact details in one place, not to scatter them
    # across feature tables (see docs/security/security-architecture.md).
    owner_ref: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    rules: Mapped[list[WatchRule]] = relationship(back_populates="watchlist")


class WatchRule(Base):
    """The filter a watchlist monitors, plus which events may interrupt the user.

    The filter columns mirror `sreoi_api.search.OpportunityFilters` field for
    field. That correspondence is the product promise: a rule that fires must
    correspond to a search that returns the same row, and `tests/test_alerts.py`
    asserts it against the real search implementation.
    """

    __tablename__ = "watch_rules"
    # Tenant key. Application filters AND row-level security both use it;
    # the RLS policy is the backstop for a query that forgets the filter.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        # Server default = the bootstrap organisation, so write paths that
        # predate tenancy keep working and the column is never null. A
        # multi-tenant caller MUST pass this explicitly; row-level security
        # rejects a row whose tenant does not match the bound one.
        server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    __table_args__ = (
        Index("ix_watch_rules_polygon", "polygon", postgresql_using="gist"),
        CheckConstraint("version >= 1", name="ck_watch_rule_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    watchlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watchlists.id"))
    name: Mapped[str] = mapped_column(String(160))
    # Bumped on any change to the filter. Alerts store the version they fired
    # under, so a ranking or alerting change is always explainable as "the rule
    # changed" rather than "the market changed".
    version: Mapped[int] = mapped_column(Integer, default=1)

    districts: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)
    opportunity_types: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    property_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_true_acquisition_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    min_discount_pct: Mapped[float | None] = mapped_column(Numeric(6, 3), nullable=True)
    min_score: Mapped[float | None] = mapped_column(Numeric(7, 4), nullable=True)
    min_gross_yield: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    polygon: Mapped[object | None] = mapped_column(
        Geography("POLYGON", srid=4326, spatial_index=False), nullable=True
    )

    triggers: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)

    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Watermark for the "new relevant comparable" trigger: evidence ingested
    # after this instant is new to this rule.
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    watchlist: Mapped[Watchlist] = relationship(back_populates="rules")


class Alert(Base):
    """Append-only. One row per (rule version, opportunity, reason)."""

    __tablename__ = "alerts"
    # Tenant key. Application filters AND row-level security both use it;
    # the RLS policy is the backstop for a query that forgets the filter.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        # Server default = the bootstrap organisation, so write paths that
        # predate tenancy keep working and the column is never null. A
        # multi-tenant caller MUST pass this explicitly; row-level security
        # rejects a row whose tenant does not match the bound one.
        server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alert_dedupe_key"),
        CheckConstraint("btrim(reason) <> ''", name="ck_alert_has_reason"),
        Index("ix_alerts_rule_time", "watch_rule_id", "created_at"),
        Index("ix_alerts_opportunity", "opportunity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    watchlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watchlists.id"))
    watch_rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watch_rules.id"))
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    # The version the rule had when this fired, copied not referenced.
    rule_version: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    # (rule, rule version, opportunity, trigger, trigger discriminator).
    dedupe_key: Mapped[str] = mapped_column(String(64))
    # The rule snapshot and the figures that satisfied it, so the alert can be
    # explained years later without re-deriving anything.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    notifications: Mapped[list[Notification]] = relationship(back_populates="alert")
    feedback: Mapped[list[AlertFeedback]] = relationship(back_populates="alert")


class Notification(Base):
    """One delivery attempt on one channel.

    `status` distinguishes DELIVERED from LOGGED_NOT_SENT. The email channel in
    this slice has no transport, and a stub that recorded DELIVERED would be a
    lie the operator would discover only when a user missed an auction.
    """

    __tablename__ = "notifications"
    # Tenant key. Application filters AND row-level security both use it;
    # the RLS policy is the backstop for a query that forgets the filter.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        # Server default = the bootstrap organisation, so write paths that
        # predate tenancy keep working and the column is never null. A
        # multi-tenant caller MUST pass this explicitly; row-level security
        # rejects a row whose tenant does not match the bound one.
        server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    __table_args__ = (
        UniqueConstraint("alert_id", "channel", name="uq_notification_alert_channel"),
        Index("ix_notifications_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id"))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    alert: Mapped[Alert] = relationship(back_populates="notifications")


class AlertFeedback(Base):
    """Acknowledgement and usefulness, kept off the alert row.

    Separate table because the alert itself is append-only, and because
    `alert_precision` (backlog E7.9) is computed from these rows.
    """

    __tablename__ = "alert_feedback"
    # Tenant key. Application filters AND row-level security both use it;
    # the RLS policy is the backstop for a query that forgets the filter.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        # Server default = the bootstrap organisation, so write paths that
        # predate tenancy keep working and the column is never null. A
        # multi-tenant caller MUST pass this explicitly; row-level security
        # rejects a row whose tenant does not match the bound one.
        server_default=text("'00000000-0000-0000-0000-000000000001'"),
    )
    __table_args__ = (
        CheckConstraint(
            "action IN ('ACKNOWLEDGED','USEFUL','NOT_USEFUL')", name="ck_alert_feedback_action"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id"))
    action: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    alert: Mapped[Alert] = relationship(back_populates="feedback")
