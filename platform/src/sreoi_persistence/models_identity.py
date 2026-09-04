"""Identity and tenancy.

The tenancy model is a deliberate decision, not an oversight, so it is stated
here rather than left implicit:

**Market data is shared.** Properties, transactions, valuations, comparables,
districts and opportunities describe the Saudi market. Every organisation sees
the same market, because the market is not anyone's private data and
duplicating it per tenant would fragment the comparable evidence the whole
product depends on.

**What a customer creates is theirs.** Watchlists, watch rules, alerts,
notifications, feedback, investment memos and uploaded documents are
tenant-scoped and isolated both by application filters and by PostgreSQL
row-level security, so a forgotten WHERE clause cannot become a cross-tenant
leak.

**Platform operations are neither.** Back-test runs and quality snapshots are
platform-admin data and are not exposed to tenants at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreoi_persistence.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class Role(StrEnum):
    """Ordered least to most privileged; `at_least` does the comparison."""

    VIEWER = "VIEWER"
    ANALYST = "ANALYST"
    ADMIN = "ADMIN"
    ORG_ADMIN = "ORG_ADMIN"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"

    @property
    def rank(self) -> int:
        return {
            "VIEWER": 10,
            "ANALYST": 20,
            "ADMIN": 30,
            "ORG_ADMIN": 40,
            "PLATFORM_ADMIN": 50,
        }[self.value]

    def at_least(self, required: Role) -> bool:
        return self.rank >= required.rank


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Per-organisation scoring thesis (see scoring.WeightProfile).
    weight_profile_version: Mapped[str] = mapped_column(String(32), default="default-v1")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    memberships: Mapped[list[Membership]] = relationship(back_populates="organization")


class User(Base):
    """A person. Passwords are only ever present for the local dev issuer;
    with OIDC the identity provider owns credentials and this row is a mirror."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    subject: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Argon2 hash. Null for OIDC-backed users, which is the normal case.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class Membership(Base):
    """A user's role within one organisation. A user may belong to several."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),
        Index("ix_memberships_org", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(24), default=Role.VIEWER.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")


class ApiKey(Base):
    """For CLI and service access. Only the hash is stored -- the secret is
    shown once at creation and is unrecoverable afterwards."""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24), default=Role.ANALYST.value)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """Append-only. Written in the same transaction as the action, so an action
    cannot succeed unaudited."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_org_time", "organization_id", "occurred_at"),
        Index("ix_audit_events_actor", "actor_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(24), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    outcome: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Tables a tenant owns. Application filters AND row-level security both key on
# organization_id; the RLS policy is the backstop for a forgotten filter.
TENANT_TABLES: tuple[str, ...] = (
    "watchlists",
    "watch_rules",
    "alerts",
    "notifications",
    "alert_feedback",
    "investment_memos",
    "documents",
    "document_extractions",
)
