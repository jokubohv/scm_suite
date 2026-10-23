"""FS-04 unit: commitment/availability pure rules and balance projection formulas."""

from __future__ import annotations

from decimal import Decimal

from app.modules.inventory_ledger.service import availability


def test_available_cannot_be_negative() -> None:
    assert availability(Decimal("5"), Decimal("5")) == Decimal("0")
    assert availability(Decimal("5"), Decimal("9")) == Decimal("0")


def test_committed_rollup_bounds() -> None:
    on_hand, committed = Decimal("10"), Decimal("3")
    assert committed <= on_hand
    assert availability(on_hand, committed) == Decimal("7")
