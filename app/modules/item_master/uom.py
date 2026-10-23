"""FS-01 UoM conversion engine (pure): factors, through-base conversion, 12-dp rounding (UOM-01..06).

Rules (ARCHITECTURE §4.1, DATA-MODEL §4):
* ``factor`` = base units per 1 unit; base factor = 1; factor > 0 (UOM-01).
* conversion always goes UoM → type base → UoM; no cross-type conversion (UOM-02).
* results quantize to 12 dp with ROUND_HALF_UP; never round before multiplying value (UOM-03).
* ``must_be_whole_base_unit`` rejects a non-integral base quantity (UOM-04).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TWELVE_DP = Decimal("0.000000000001")
ZERO = Decimal("0")


class UomError(Exception):
    code = "UOM_ERROR"
    http_status = 422


class UomTypeMismatch(UomError):
    code = "UOM_TYPE_MISMATCH"


class UomNotFound(UomError):
    code = "UOM_NOT_FOUND"
    http_status = 404


class UomFractionalBase(UomError):
    code = "UOM_FRACTIONAL_BASE"


class UomInvalidQuantity(UomError):
    code = "UOM_INVALID_QUANTITY"


@dataclass(frozen=True, slots=True)
class UomFactor:
    uom_id: str
    uom_type_id: str
    code: str
    factor: Decimal  # base units per 1 unit
    rounding_precision: int = 4
    is_base: bool = False


@dataclass(frozen=True, slots=True)
class UomConversion:
    quantity: Decimal
    quantity_base: Decimal
    factor: Decimal
    from_uom_code: str
    to_uom_code: str | None = None
    rounded_to: int = 12
    whole_base_required: bool = False
    quantity_to: Decimal | None = None


def as_decimal(value: object, *names: str) -> Decimal:
    raw = value
    for name in names:
        if isinstance(raw, dict):
            raw = raw.get(name)
    try:
        decimal = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UomInvalidQuantity(f"invalid quantity {value!r}") from exc
    if not decimal.is_finite():
        raise UomInvalidQuantity("quantity must be finite")
    return decimal


def quantize_quantity(value: Decimal) -> Decimal:
    """12-dp ROUND_HALF_UP quantisation (NUMERIC(38,12) storage contract)."""
    return value.quantize(TWELVE_DP, rounding=ROUND_HALF_UP)


def validate_factor(factor: Decimal) -> Decimal:
    if factor <= 0:
        raise UomTypeMismatch("conversion factor must be > 0")
    return factor


def resolve_factor(
    *,
    from_uom: UomFactor,
    to_uom: UomFactor | None = None,
    type_conversion_factor: Decimal | None = None,
    item_override_factor: Decimal | None = None,
) -> Decimal:
    """Resolve the *from → to* factor through the units-type base (UOM-01/UOM-02).

    Precedence: per-item ``conversion_override`` (from-unit) → units-type ``uom_conversion`` override →
    unit ``conversion_factor``. ``to_uom`` must belong to the same units type.
    """
    if to_uom is not None and to_uom.uom_type_id != from_uom.uom_type_id:
        raise UomTypeMismatch("UoMs belong to different units types")
    from_factor = validate_factor(item_override_factor if item_override_factor is not None else from_uom.factor)
    if item_override_factor is not None:
        # item override is expressed against the item stock base, not the units-type base; treat it as
        # the from-unit factor for this item.
        from_factor = validate_factor(item_override_factor)
    if type_conversion_factor is not None:
        from_factor = validate_factor(type_conversion_factor)
    if to_uom is None:
        return from_factor
    return from_factor / validate_factor(to_uom.factor)


def convert_quantity(
    quantity: object,
    *,
    from_uom: UomFactor,
    to_uom: UomFactor | None = None,
    type_conversion_factor: Decimal | None = None,
    item_override_factor: Decimal | None = None,
    whole_base_required: bool = False,
) -> UomConversion:
    """Convert ``quantity`` from one UoM to the type base (or to another same-type UoM)."""
    qty = as_decimal(quantity)
    factor = resolve_factor(
        from_uom=from_uom,
        to_uom=to_uom,
        type_conversion_factor=type_conversion_factor,
        item_override_factor=item_override_factor,
    )
    base = qty * from_uom.factor if item_override_factor is None else qty * factor
    if type_conversion_factor is not None and item_override_factor is None:
        base = qty * type_conversion_factor
    result = base if to_uom is None else base / to_uom.factor
    quantized = quantize_quantity(result)
    quantized_base = quantize_quantity(base)
    if whole_base_required and quantized_base != quantized_base.to_integral_value():
        raise UomFractionalBase("base quantity must be a whole unit")
    return UomConversion(
        quantity=qty,
        quantity_base=quantized_base,
        factor=factor,
        from_uom_code=from_uom.code,
        to_uom_code=to_uom.code if to_uom is not None else None,
        rounded_to=12,
        whole_base_required=whole_base_required,
        quantity_to=quantized if to_uom is not None else None,
    )


def require_same_units_type(*uoms: UomFactor) -> None:
    types = {uom.uom_type_id for uom in uoms}
    if len(types) > 1:
        raise UomTypeMismatch("item UoMs must belong to the same units type")


__all__ = [
    "TWELVE_DP",
    "UomConversion",
    "UomError",
    "UomFactor",
    "UomFractionalBase",
    "UomInvalidQuantity",
    "UomNotFound",
    "UomTypeMismatch",
    "as_decimal",
    "convert_quantity",
    "quantize_quantity",
    "require_same_units_type",
    "resolve_factor",
    "validate_factor",
]
