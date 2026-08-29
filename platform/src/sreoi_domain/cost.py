"""True acquisition cost.

The most important invariant in the platform lives here. A seller asking
SAR 120,000 for a unit carrying SAR 600,000 of remaining developer
installments is not a SAR 120,000 opportunity. Comparing an advertised price
against a market value produces an apparent 87% discount and complete nonsense.

So: a discount may only be computed against a complete cost object. When a
material line item is UNKNOWN the discount is REFUSED -- not estimated, not
zero, not silently omitted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sreoi_domain.provenance import Basis, Money

METHOD_VERSION = "cost-v1"


class CostKind(StrEnum):
    SELLER_PAYMENT = "SELLER_PAYMENT"
    WINNING_BID = "WINNING_BID"
    REMAINING_INSTALLMENTS = "REMAINING_INSTALLMENTS"
    AUCTION_COMMISSION = "AUCTION_COMMISSION"
    BROKERAGE = "BROKERAGE"
    TRANSFER_TAX = "TRANSFER_TAX"
    VAT = "VAT"
    REGISTRATION = "REGISTRATION"
    RENOVATION = "RENOVATION"
    KNOWN_LIABILITY = "KNOWN_LIABILITY"

    @property
    def is_material_by_default(self) -> bool:
        """Items large enough that not knowing them invalidates a discount."""
        return self in {
            CostKind.SELLER_PAYMENT,
            CostKind.WINNING_BID,
            CostKind.REMAINING_INSTALLMENTS,
            CostKind.KNOWN_LIABILITY,
        }


@dataclass(frozen=True, slots=True)
class CostLineItem:
    kind: CostKind
    amount: Money
    material: bool | None = None
    note: str | None = None

    @property
    def is_material(self) -> bool:
        return self.kind.is_material_by_default if self.material is None else self.material

    @property
    def is_known(self) -> bool:
        return self.amount.is_known

    @property
    def value(self) -> Decimal:
        """The known amount. Zero for an unknown item -- callers must check
        `is_known` before treating a total as meaningful."""
        return self.amount.value if self.amount.value is not None else Decimal("0")


@dataclass(frozen=True, slots=True)
class TrueAcquisitionCost:
    line_items: tuple[CostLineItem, ...]
    method_version: str = METHOD_VERSION

    @property
    def total(self) -> Decimal:
        """Sum of known items. Meaningless on its own when incomplete -- always
        check `is_complete` before presenting this as the cost."""
        return sum((item.value for item in self.line_items if item.is_known), Decimal("0"))

    @property
    def unknown_material_items(self) -> tuple[CostLineItem, ...]:
        return tuple(i for i in self.line_items if i.is_material and not i.is_known)

    @property
    def is_complete(self) -> bool:
        return not self.unknown_material_items

    @property
    def completeness(self) -> float:
        """Fraction of material line items with a known basis."""
        material = [i for i in self.line_items if i.is_material]
        if not material:
            return 0.0
        return sum(1 for i in material if i.is_known) / len(material)

    @property
    def estimated_share(self) -> Decimal:
        """Portion of the total that rests on estimates rather than actuals."""
        if self.total == 0:
            return Decimal("0")
        estimated = sum(
            (i.value for i in self.line_items if i.is_known and i.amount.basis is Basis.ESTIMATE),
            Decimal("0"),
        )
        return estimated / self.total


@dataclass(frozen=True, slots=True)
class DiscountRefused:
    """A refusal that names what is missing.

    Telling the user exactly which figure blocks the calculation is more useful
    than a confident guess, and it is the difference between a tool
    professionals trust and one they check twice.
    """

    missing: tuple[CostKind, ...]

    @property
    def reason(self) -> str:
        names = ", ".join(k.value.replace("_", " ").lower() for k in self.missing)
        return f"{names} unknown — required before a discount can be computed"


@dataclass(frozen=True, slots=True)
class Discount:
    fraction: float
    fair_value_base: Decimal
    true_acquisition_cost: Decimal

    @property
    def percent(self) -> float:
        return self.fraction * 100.0


def compute_discount(
    fair_value_base: Decimal, cost: TrueAcquisitionCost
) -> Discount | DiscountRefused:
    """Discount against true acquisition cost -- never against advertised price."""
    if not cost.is_complete:
        return DiscountRefused(tuple(i.kind for i in cost.unknown_material_items))
    if fair_value_base <= 0:
        raise ValueError("fair value must be positive to compute a discount")
    fraction = float((fair_value_base - cost.total) / fair_value_base)
    return Discount(fraction, fair_value_base, cost.total)


def build_cost(items: Sequence[CostLineItem]) -> TrueAcquisitionCost:
    if not items:
        raise ValueError("a cost must have at least one line item")
    return TrueAcquisitionCost(tuple(items))
