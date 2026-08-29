"""The true-acquisition-cost invariant.

This is the single most valuable piece of logic in the platform: it is what
stops a SAR 120k seller ask against a SAR 910k valuation being reported as an
87% discount.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sreoi_domain.cost import (
    CostKind,
    CostLineItem,
    Discount,
    DiscountRefused,
    build_cost,
    compute_discount,
)
from sreoi_domain.provenance import Basis, actual, unknown


def _seller(amount: str) -> CostLineItem:
    return CostLineItem(CostKind.SELLER_PAYMENT, actual(Decimal(amount)))


def test_complete_cost_yields_a_discount() -> None:
    cost = build_cost(
        [
            _seller("120000"),
            CostLineItem(CostKind.REMAINING_INSTALLMENTS, actual(Decimal("600000"))),
        ]
    )
    assert cost.total == Decimal("720000")
    assert cost.is_complete

    result = compute_discount(Decimal("910000"), cost)
    assert isinstance(result, Discount)
    assert result.percent == pytest.approx(20.879, abs=0.01)


def test_unknown_material_item_refuses_the_discount() -> None:
    """The headline safety property. A naive system reports ~87% here."""
    cost = build_cost(
        [
            _seller("120000"),
            CostLineItem(CostKind.REMAINING_INSTALLMENTS, unknown("not supplied")),
        ]
    )
    assert not cost.is_complete

    result = compute_discount(Decimal("910000"), cost)
    assert isinstance(result, DiscountRefused)
    assert CostKind.REMAINING_INSTALLMENTS in result.missing
    # The refusal must name what is missing, not merely decline.
    assert "remaining installments" in result.reason


def test_unknown_is_not_treated_as_zero() -> None:
    cost = build_cost(
        [
            _seller("120000"),
            CostLineItem(CostKind.REMAINING_INSTALLMENTS, unknown("not supplied")),
        ]
    )
    # The known total is 120000, but that must never be presented as the cost.
    assert cost.total == Decimal("120000")
    assert isinstance(compute_discount(Decimal("910000"), cost), DiscountRefused)


def test_immaterial_unknown_does_not_block() -> None:
    cost = build_cost(
        [
            _seller("720000"),
            CostLineItem(CostKind.RENOVATION, unknown("not assessed"), material=False),
        ]
    )
    assert cost.is_complete
    assert isinstance(compute_discount(Decimal("910000"), cost), Discount)


def test_completeness_ratio() -> None:
    cost = build_cost(
        [
            _seller("120000"),
            CostLineItem(CostKind.REMAINING_INSTALLMENTS, unknown("x")),
        ]
    )
    assert cost.completeness == pytest.approx(0.5)


def test_unknown_provenance_cannot_carry_a_value() -> None:
    from sreoi_domain.provenance import Provenanced

    with pytest.raises(ValueError, match="UNKNOWN"):
        Provenanced(Decimal("5"), Basis.UNKNOWN)
