"""Field-level provenance (ADR-007).

The core idea of the platform: a number without its evidence is not a product.
Every externally-derived value in the domain is wrapped in `Provenanced`, so a
bare `Decimal` cannot reach a money field without a type error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

T = TypeVar("T")
# Covariant: an UNKNOWN value is Provenanced[None], which must be usable
# wherever a Provenanced[Decimal | None] money field is expected.
T_co = TypeVar("T_co", covariant=True)


class Basis(StrEnum):
    """How a value came to be known. Ordered from strongest to weakest."""

    ACTUAL = "ACTUAL"  # observed directly from a source record
    RULE = "RULE"  # derived from a versioned rule table (fees, taxes)
    ESTIMATE = "ESTIMATE"  # modelled from comparable evidence
    INFERRED = "INFERRED"  # deduced from other fields, weaker than an estimate
    UNKNOWN = "UNKNOWN"  # explicitly not known -- never silently zero

    @property
    def is_known(self) -> bool:
        return self is not Basis.UNKNOWN


@dataclass(frozen=True, slots=True)
class Evidence:
    """A pointer back to the exact thing that justifies a value."""

    kind: str  # "text_span" | "document" | "api_response" | "computation"
    locator: str  # char range, "page 4 bbox=...", endpoint, or formula id
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class Provenanced(Generic[T_co]):
    """A value carrying where it came from and how much we trust it."""

    value: T_co
    basis: Basis
    confidence: float = 1.0
    source_record_id: UUID | None = None
    evidence: Evidence | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.basis is Basis.UNKNOWN and self.value is not None:
            raise ValueError("an UNKNOWN value must carry value=None, not a placeholder")

    @property
    def is_known(self) -> bool:
        return self.basis.is_known and self.value is not None


# Money is always provenanced. Using this alias in signatures means mypy rejects
# a bare Decimal reaching a financial field -- the invariant is structural.
# The `| None` is deliberate: an unknown amount must be representable as a
# first-class state rather than being coerced to zero.
Money = Provenanced[Decimal | None]


def actual(
    value: T,
    *,
    confidence: float = 1.0,
    source_record_id: UUID | None = None,
    evidence: Evidence | None = None,
) -> Provenanced[T]:
    return Provenanced(value, Basis.ACTUAL, confidence, source_record_id, evidence)


def rule(value: T, *, rule_id: str, confidence: float = 1.0) -> Provenanced[T]:
    return Provenanced(
        value, Basis.RULE, confidence, None, Evidence(kind="computation", locator=rule_id)
    )


def estimate(value: T, *, confidence: float, method: str) -> Provenanced[T]:
    return Provenanced(
        value, Basis.ESTIMATE, confidence, None, Evidence(kind="computation", locator=method)
    )


def unknown(reason: str) -> Provenanced[None]:
    return Provenanced(None, Basis.UNKNOWN, 0.0, None, Evidence(kind="computation", locator=reason))
