"""FS-01 UoM invariant suite (UOM-01..06): factor math, through-base, 12-dp half-up, whole-base guard."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.item_master.uom import (
    UomFactor,
    UomFractionalBase,
    UomTypeMismatch,
    convert_quantity,
    quantize_quantity,
    require_same_units_type,
)

EA = UomFactor(uom_id="ea", uom_type_id="qty", code="EA", factor=Decimal("1"), is_base=True)
CASE = UomFactor(uom_id="case", uom_type_id="qty", code="CASE", factor=Decimal("12"))
KG = UomFactor(uom_id="kg", uom_type_id="weight", code="KG", factor=Decimal("1"), is_base=True)
G = UomFactor(uom_id="g", uom_type_id="weight", code="G", factor=Decimal("0.001"))
LB = UomFactor(uom_id="lb", uom_type_id="weight", code="LB", factor=Decimal("0.453592370000"))


def test_uom_01_factor_math_and_identity() -> None:
    result = convert_quantity(Decimal("2"), from_uom=CASE)
    assert result.quantity_base == Decimal("24.000000000000")
    assert convert_quantity(Decimal("3"), from_uom=EA).quantity_base == Decimal("3.000000000000")
    with pytest.raises(UomTypeMismatch):
        convert_quantity(Decimal("1"), from_uom=EA, to_uom=KG)


def test_uom_02_through_base_conversion() -> None:
    assert convert_quantity(Decimal("1000"), from_uom=G).quantity_base == Decimal("1.000000000000")
    assert convert_quantity(Decimal("2"), from_uom=KG, to_uom=G).quantity_to == Decimal("2000.000000000000")
    assert convert_quantity(Decimal("1"), from_uom=LB).quantity_base == Decimal("0.453592370000")
    assert convert_quantity(Decimal("1000"), from_uom=G, to_uom=KG).quantity_to == Decimal("1.000000000000")


def test_uom_03_quantize_half_up_12dp() -> None:
    assert quantize_quantity(Decimal("1.0000000000005")) == Decimal("1.000000000001")
    assert quantize_quantity(Decimal("1.0000000000004")) == Decimal("1.000000000000")
    conversion = convert_quantity(Decimal("1"), from_uom=LB, to_uom=G)
    assert conversion.quantity_to == Decimal("453.592370000000")
    assert conversion.rounded_to == 12
    # no rounding before multiplying: 3 × LB in G = 1360.77711 exactly (12 dp)
    assert convert_quantity(Decimal("3"), from_uom=LB, to_uom=G).quantity_to == Decimal("1360.777110000000")


def test_uom_04_whole_base_guard() -> None:
    with pytest.raises(UomFractionalBase):
        convert_quantity(Decimal("0.5"), from_uom=EA, whole_base_required=True)
    assert convert_quantity(Decimal("2"), from_uom=EA, whole_base_required=True).quantity_base == Decimal("2.000000000000")
    with pytest.raises(UomFractionalBase):
        convert_quantity(Decimal("1"), from_uom=G, whole_base_required=True)


def test_uom_05_same_units_type_requirement() -> None:
    require_same_units_type(EA, CASE)
    with pytest.raises(UomTypeMismatch):
        require_same_units_type(EA, KG)


def test_uom_06_conversion_override_precedence() -> None:
    # item-level override replaces the units-type factor for the from-UoM
    overridden = convert_quantity(Decimal("1"), from_uom=CASE, item_override_factor=Decimal("10"))
    assert overridden.quantity_base == Decimal("10.000000000000")
    type_override = convert_quantity(Decimal("1"), from_uom=CASE, type_conversion_factor=Decimal("6"))
    assert type_override.quantity_base == Decimal("6.000000000000")
