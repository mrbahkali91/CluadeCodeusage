"""Investment-memo tables.

A memo is a *dated claim about evidence that moves*. The valuation, the cost
and the score all change as new comparables arrive, so a stored memo that does
not name the artifacts it was written against is unauditable: nobody can later
say whether it was right at the time or merely lucky.

Every row therefore pins the score, valuation and cost rows it narrated, plus
the method versions of each, and the agent run that produced it.

Two invariants are enforced by the database rather than by convention, for the
same reason `verification_checks` enforces its evidence rule there:

  * a GENERATED memo must carry its body, its cited figures and the
    deterministically computed maximum recommended purchase price -- a memo
    whose figures could not be resolved must never reach storage;
  * a memo that was *not* generated must say why, because "no memo" without a
    reason is indistinguishable from a silent failure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sreoi_persistence.models import Base

# Lifecycle of a memo request. Only GENERATED is displayable.
MEMO_STATUSES = ("GENERATED", "NOT_GENERATED", "REJECTED")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class InvestmentMemoRow(Base):
    """One memo, in one locale, for one moment of the evidence."""

    __tablename__ = "investment_memos"
    __table_args__ = (
        Index("ix_investment_memos_lookup", "opportunity_id", "locale", "generated_at"),
        CheckConstraint(
            "status IN ('GENERATED', 'NOT_GENERATED', 'REJECTED')",
            name="ck_memo_status_known",
        ),
        CheckConstraint("locale IN ('en', 'ar')", name="ck_memo_locale_known"),
        # A displayable memo must carry its body, its cited figures and the
        # computed maximum price. Fail-closed, enforced below the application.
        CheckConstraint(
            "status <> 'GENERATED' OR ("
            "sections IS NOT NULL AND figures IS NOT NULL "
            "AND max_recommended_purchase_price IS NOT NULL AND decision IS NOT NULL)",
            name="ck_memo_generated_is_complete",
        ),
        # Not generating is a decision, and a decision needs a reason.
        CheckConstraint(
            "status = 'GENERATED' OR reason IS NOT NULL",
            name="ck_memo_absence_has_a_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("opportunities.id"))
    locale: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(16))
    # Why there is no memo, or why a generated one was rejected.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The evidence of the moment. Nullable because a gate refusal can precede
    # a valuation existing at all.
    score_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("opportunity_scores.id"), nullable=True
    )
    valuation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("valuations.id"), nullable=True
    )
    cost_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("true_acquisition_costs.id"), nullable=True
    )
    score_total: Mapped[float | None] = mapped_column(Numeric(7, 4), nullable=True)
    data_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    # Method versions, so a memo can be re-derived or invalidated wholesale
    # when any of the underlying methods changes.
    scoring_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valuation_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_method_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    memo_method_version: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    weight_profile_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The one number the memo is allowed to headline, computed deterministically
    # as fair_value_low * (1 - target_margin) and merely narrated by the agent.
    target_margin: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    max_recommended_purchase_price: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)

    sections: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Every figure the memo displays, with the computed field it resolved to.
    figures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # The full fact table the memo was validated against, so a reviewer can
    # check any figure without re-running the pipeline.
    facts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id"), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(48), nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
