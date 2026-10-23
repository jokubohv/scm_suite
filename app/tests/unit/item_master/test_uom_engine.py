"""Pure UoM engine unit tests (factor math, 12-dp rounding, whole-base guard, overrides)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.item_master.uom import (
    UomFactor,
    UomFractionalBase,
    UomTypeMismatch,
    convert_quantity,
    quantize_quantity,
)

EA = UomFactor("ea", "qty", "EA", Decimal("1"), is_base=True)
CASE = UomFactor("case", "qty", "CASE", Decimal("12"))
KG = UomFactor("kg", "weight", "KG", Decimal("1"), is_base=True)
G = UomFactor("g", "weight", "G", Decimal("0.001"))


def test_case_to_base() -> None:
    assert convert_quantity("2", from_uom=CASE).quantity_base == Decimal("24.000000000000")


def test_grams_to_kilograms() -> None:
    assert convert_quantity(Decimal("1500"), from_uom=G, to_uom=KG).quantity_to == Decimal("1.500000000000")


def test_round_half_up() -> None:
    assert quantize_quantity(Decimal("0.0000000000005")) == Decimal("0.000000000001")


def test_whole_base_rejects_fraction() -> None:
    with pytest.raises(UomFractionalBase):
        convert_quantity(Decimal("0.5"), from_uom=EA, whole_base_required=True)


def test_cross_type_rejected() -> None:
    with pytest.raises(UomTypeMismatch):
        convert_quantity(Decimal("1"), from_uom=EA, to_uom=KG)


def test_item_override_precedence() -> None:
    assert convert_quantity(Decimal("1"), from_uom=CASE, item_override_factor=Decimal("10")).quantity_base == Decimal("10.000000000000")
