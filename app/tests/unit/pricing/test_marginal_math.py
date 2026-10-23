"""Unit tests for marginal (tiered) quantity-pricing mathematics."""

from __future__ import annotations

from decimal import Decimal

from app.modules.pricing.engine import ScheduleBreak, ScheduleRow, apply_schedule


def _schedule(*breaks: ScheduleBreak, marginal: bool = True) -> ScheduleRow:
    return ScheduleRow(
        id=__import__("uuid").uuid4(),
        code="S",
        calculation_mode="LINE",
        is_marginal=marginal,
        currency_code="USD",
        breaks=breaks,
    )


def test_marginal_percent_tiers_distribute_across_bands() -> None:
    schedule = _schedule(
        ScheduleBreak(
            qty_from=Decimal("0"), qty_to=Decimal("10"), pricing_method="DISCOUNT_PERCENT", value=Decimal("10")
        ),
        ScheduleBreak(qty_from=Decimal("10"), qty_to=None, pricing_method="DISCOUNT_PERCENT", value=Decimal("20")),
    )
    adjustment = apply_schedule(
        schedule, mode="LINE", quantity_basis=Decimal("20"), unit_base=Decimal("10"), line_quantity=Decimal("20")
    )
    assert adjustment.unit_price == Decimal("8.500000000000")
    assert adjustment.discount_amount == Decimal("30.000000000000")
    assert adjustment.marginal_applied is True


def test_marginal_fixed_price_uses_tier_rate() -> None:
    schedule = _schedule(
        ScheduleBreak(qty_from=Decimal("0"), qty_to=Decimal("10"), pricing_method="FIXED_PRICE", value=Decimal("10")),
        ScheduleBreak(qty_from=Decimal("10"), qty_to=None, pricing_method="FIXED_PRICE", value=Decimal("8")),
    )
    adjustment = apply_schedule(
        schedule, mode="LINE", quantity_basis=Decimal("15"), unit_base=Decimal("10"), line_quantity=Decimal("15")
    )
    assert adjustment.unit_price == Decimal("8.000000000000")


def test_non_marginal_amount_discount_spread_over_basis() -> None:
    schedule = _schedule(
        ScheduleBreak(qty_from=Decimal("10"), qty_to=None, pricing_method="DISCOUNT_AMOUNT", value=Decimal("20")),
        marginal=False,
    )
    adjustment = apply_schedule(
        schedule,
        mode="OVERALL_ITEM",
        quantity_basis=Decimal("20"),
        unit_base=Decimal("10"),
        line_quantity=Decimal("10"),
    )
    assert adjustment.unit_price == Decimal("9.000000000000")
    assert adjustment.calculation_mode == "OVERALL_ITEM"
