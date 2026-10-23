"""Pure, deterministic pricing-resolution engine (FS-02 §5.1–§5.2).

No I/O: the repository loads a :class:`PricingSnapshot` (prices, levels, groups, rules, schedules,
coupons, item attributes) and this module applies the documented precedence and quantity-pricing
mathematics, returning an authoritative priced quote with a stable ``quote_hash``.

Resolution order (DM-PRICE-01): coupon → price rule (priority + conflict policy) → pricing group →
price level → item base price → fallback 0/NONE. Quantity modes (DM-PRICE-02):
LINE / OVERALL_ITEM / PARENT / SCHEDULE with marginal (tiered) and non-marginal semantics.

Decimal only (no floats); unit prices quantized to 12 dp; money/quantity are decimal strings on the
wire. ``policy_version`` pins the algorithm for reproducible quotes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from app.core.security.idempotency import canonical_json, sha256_hex

POLICY_VERSION = "pricing-engine@1.0.0"
UNIT_QUANT = Decimal("0.000000000001")
DEFAULT_MINOR_UNITS = 2
ZERO = Decimal("0")


class EngineError(Exception):
    """Pure-engine error with a stable domain code (router maps to problem+json)."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


# --------------------------------------------------------------------------- inputs ----------
@dataclass(frozen=True, slots=True)
class CostContext:
    unit_cost: Decimal | None = None
    cost_basis: str | None = None


@dataclass(frozen=True, slots=True)
class PriceLineInput:
    line_ref: str
    item_id: UUID
    quantity: Decimal
    uom: str
    parent_item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PriceRequest:
    lines: tuple[PriceLineInput, ...]
    currency_code: str
    document_date: date
    external_customer_ref: str | None = None
    customer_group_id: UUID | None = None
    price_level_id: UUID | None = None
    coupon_code: str | None = None
    cost_context: CostContext | None = None
    include_trace: bool = True


@dataclass(frozen=True, slots=True)
class ItemInfo:
    item_id: UUID
    is_active: bool
    category_id: UUID | None = None
    pricing_group_id: UUID | None = None
    schedule_id: UUID | None = None
    matrix_parent_id: UUID | None = None
    qty_behavior: str = "LINE"
    initial_cost: Decimal | None = None
    purchase_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PriceLevelInfo:
    id: UUID
    code: str
    currency_code: str
    is_inactive: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class PriceRow:
    id: UUID
    item_id: UUID
    price_level_id: UUID
    currency_code: str
    qty_min: Decimal
    qty_max: Decimal | None
    unit_price: Decimal
    effective_from: date
    effective_to: date | None
    is_active: bool
    cost_plus_basis: str | None = None
    created_at: Any = None


@dataclass(frozen=True, slots=True)
class PricingGroupRow:
    id: UUID
    item_group_id: UUID
    customer_group_id: UUID
    price_level_id: UUID
    is_default: bool
    discount_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ScheduleBreak:
    qty_from: Decimal
    qty_to: Decimal | None
    pricing_method: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    id: UUID
    code: str
    calculation_mode: str
    is_marginal: bool
    currency_code: str
    breaks: tuple[ScheduleBreak, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleCriterion:
    criterion: str
    operator: str
    value_text: str | None = None
    value_number: Decimal | None = None
    value_date: date | None = None
    group_no: int = 1


@dataclass(frozen=True, slots=True)
class RuleAction:
    action_type: str
    price_level_id: UUID | None = None
    value: Decimal | None = None
    cost_basis: str | None = None
    apply_to: str = "UNIT"


@dataclass(frozen=True, slots=True)
class RuleRow:
    id: UUID
    code: str
    rule_type: str
    priority: int
    conflict_policy: str
    is_active: bool
    start_date: date | None = None
    end_date: date | None = None
    currency_code: str | None = None
    criteria: tuple[RuleCriterion, ...] = ()
    action: RuleAction | None = None


@dataclass(frozen=True, slots=True)
class CouponRow:
    id: UUID
    code: str
    coupon_type: str
    value: Decimal
    currency_code: str
    start_date: date
    end_date: date
    max_uses: int | None
    uses_count: int
    is_active: bool
    item_id: UUID | None = None
    item_category_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    items: Mapping[UUID, ItemInfo]
    levels: Mapping[UUID, PriceLevelInfo]
    default_level_id: UUID | None
    prices: tuple[PriceRow, ...]
    pricing_groups: tuple[PricingGroupRow, ...] = ()
    schedules: Mapping[UUID, ScheduleRow] = field(default_factory=dict)
    rules: tuple[RuleRow, ...] = ()
    collections: Mapping[UUID, frozenset[UUID]] = field(default_factory=dict)
    coupons: Mapping[str, CouponRow] = field(default_factory=dict)
    base_currency: str = "USD"
    allow_base_fallback: bool = False
    fx_rates: Mapping[tuple[str, str, date], Decimal] = field(default_factory=dict)
    minor_units: int = DEFAULT_MINOR_UNITS


# --------------------------------------------------------------------------- helpers ----------
def _q(value: Decimal) -> Decimal:
    return value.quantize(UNIT_QUANT, rounding=ROUND_HALF_UP)


def _money(value: Decimal, minor_units: int = DEFAULT_MINOR_UNITS) -> Decimal:
    exponent = Decimal(1).scaleb(-minor_units)
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def _fmt_qty(value: Decimal) -> str:
    return format(_q(value), "f")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _band_contains(row_qty_min: Decimal, row_qty_max: Decimal | None, qty: Decimal) -> bool:
    if qty < row_qty_min:
        return False
    return row_qty_max is None or qty < row_qty_max


def _effective_contains(row: PriceRow, document_date: date) -> bool:
    if not row.is_active:
        return False
    if row.effective_from > document_date:
        return False
    return row.effective_to is None or row.effective_to >= document_date


def _pick_price(rows: Sequence[PriceRow]) -> PriceRow | None:
    # Band specificity first (higher qty_min), then RULE-PRICE-12 latest effective_from, then
    # created_at DESC and id DESC for a deterministic tie-break.
    return (
        max(rows, key=lambda row: (row.qty_min, row.effective_from, row.created_at or 0, str(row.id))) if rows else None
    )


def _select_price(
    snapshot: PricingSnapshot,
    *,
    item_id: UUID,
    level_id: UUID,
    currency_code: str,
    qty: Decimal,
    document_date: date,
) -> PriceRow | None:
    rows = [
        row
        for row in snapshot.prices
        if row.item_id == item_id
        and row.price_level_id == level_id
        and row.currency_code == currency_code
        and _effective_contains(row, document_date)
        and _band_contains(row.qty_min, row.qty_max, qty)
    ]
    return _pick_price(rows)


def _item_cost(snapshot: PricingSnapshot, item: ItemInfo, request: PriceRequest) -> tuple[Decimal | None, str | None]:
    if request.cost_context and request.cost_context.unit_cost is not None:
        return request.cost_context.unit_cost, request.cost_context.cost_basis
    for basis, value in (("LAST_PURCHASE", item.purchase_price), ("AVERAGE", item.initial_cost)):
        if value is not None:
            return value, basis
    return None, None


def _price_row_for_coupon(snapshot: PricingSnapshot, coupon: CouponRow, item: ItemInfo) -> bool:
    if coupon.item_id is not None and coupon.item_id != item.item_id:
        return False
    if coupon.item_category_id is not None and coupon.item_category_id != item.category_id:
        return False
    return True


def validate_coupon(
    snapshot: PricingSnapshot,
    *,
    code: str,
    item: ItemInfo,
    quantity: Decimal,
    currency_code: str,
    document_date: date,
) -> tuple[CouponRow | None, str | None]:
    """Validate a coupon for a line context; returns ``(coupon, reason)`` (no side effects)."""
    coupon = snapshot.coupons.get(code.strip().upper())
    if coupon is None:
        return None, "COUPON_NOT_FOUND"
    if not coupon.is_active:
        return None, "COUPON_INACTIVE"
    if document_date < coupon.start_date or document_date > coupon.end_date:
        return None, "COUPON_OUT_OF_WINDOW"
    if coupon.currency_code != currency_code:
        return None, "COUPON_CURRENCY_MISMATCH"
    if coupon.max_uses is not None and coupon.uses_count >= coupon.max_uses:
        return None, "COUPON_EXHAUSTED"
    if not _price_row_for_coupon(snapshot, coupon, item):
        return None, "COUPON_SCOPE_MISMATCH"
    if quantity <= 0:
        return None, "COUPON_QTY_INVALID"
    return coupon, None


def coupon_discount(coupon: CouponRow, *, unit_price: Decimal, quantity: Decimal) -> Decimal:
    """Discount amount for a line at the given unit price (clamped so unit price never goes below 0)."""
    if coupon.coupon_type == "PERCENT":
        raw = unit_price * quantity * coupon.value / Decimal(100)
    else:
        raw = min(coupon.value, unit_price * quantity)
    return max(ZERO, min(raw, unit_price * quantity))


# --------------------------------------------------------------------------- rules ------------
def _criterion_matches(criterion: RuleCriterion, ctx: Mapping[str, Any]) -> bool:
    actual = ctx.get(criterion.criterion)
    operator = criterion.operator
    if criterion.criterion in {"CUSTOMER"}:
        actual = ctx.get("external_customer_ref")
        expected = criterion.value_text
        if operator == "EQ":
            return actual == expected
        if operator == "NE":
            return actual != expected
        if operator == "IN":
            return actual in {part.strip() for part in str(expected or "").split(",")}
        return False
    if criterion.criterion in {"CUSTOMER_GROUP"}:
        if operator == "EQ":
            return str(actual) == str(criterion.value_text or "")
        if operator == "IN":
            return str(actual) in {part.strip() for part in str(criterion.value_text or "").split(",")}
        if operator == "NE":
            return str(actual) != str(criterion.value_text or "")
        return False
    if criterion.criterion in {"ITEM", "ITEM_COLLECTION", "ITEM_CATEGORY"}:
        if operator == "EQ":
            return str(actual) == str(criterion.value_text or "")
        if operator == "NE":
            return str(actual) != str(criterion.value_text or "")
        if operator == "IN":
            return str(actual) in {part.strip() for part in str(criterion.value_text or "").split(",")}
        return False
    if criterion.criterion == "CURRENCY":
        if operator == "EQ":
            return str(actual) == str(criterion.value_text or "")
        if operator == "NE":
            return str(actual) != str(criterion.value_text or "")
        if operator == "IN":
            return str(actual) in {part.strip() for part in str(criterion.value_text or "").split(",")}
        return False
    if criterion.criterion == "QTY":
        value = criterion.value_number
        if value is None:
            return False
        if operator in {"EQ", "GTE", "LTE"}:
            qty = _dec(actual)
            return {"EQ": qty == value, "GTE": qty >= value, "LTE": qty <= value}[operator]
        if operator == "BETWEEN":
            low, high = _parse_range(criterion.value_text, value, Decimal)
            return low <= _dec(actual) <= high
        return False
    if criterion.criterion == "DATE":
        if operator in {"EQ", "GTE", "LTE"} and criterion.value_date is not None:
            assert actual is not None
            return {
                "EQ": actual == criterion.value_date,
                "GTE": actual >= criterion.value_date,
                "LTE": actual <= criterion.value_date,
            }[operator]
        if operator == "BETWEEN":
            if not isinstance(actual, date):
                return False
            low, high = _parse_date_range(criterion.value_text, criterion.value_date)
            return low <= actual <= high
        return False
    return False


def _parse_range(value_text: str | None, value_number: Decimal | None, kind: type) -> tuple[Any, Any]:
    if value_number is not None:
        return value_number, value_number
    parts = [part.strip() for part in str(value_text or "").split(",") if part.strip()]
    if len(parts) != 2:
        return Decimal("-inf"), Decimal("inf")
    if kind is Decimal:
        return Decimal(parts[0]), Decimal(parts[1])
    return parts[0], parts[1]


def _parse_date_range(value_text: str | None, value_date: date | None) -> tuple[date, date]:
    from datetime import date as _date

    if value_date is not None:
        return value_date, value_date
    parts = [part.strip() for part in str(value_text or "").split(",") if part.strip()]
    if len(parts) != 2:
        return _date.min, _date.max
    return _date.fromisoformat(parts[0]), _date.fromisoformat(parts[1])


def _criteria_match(criteria: Sequence[RuleCriterion], ctx: Mapping[str, Any]) -> bool:
    # same group_no => AND; different groups => OR
    groups: dict[int, list[RuleCriterion]] = {}
    for criterion in criteria:
        groups.setdefault(criterion.group_no, []).append(criterion)
    if not groups:
        return True
    return any(all(_criterion_matches(criterion, ctx) for criterion in group) for group in groups.values())


def rule_context(
    snapshot: PricingSnapshot,
    *,
    item: ItemInfo,
    quantity: Decimal,
    request: PriceRequest,
) -> dict[str, Any]:
    collection_ids = [
        str(collection_id) for collection_id, members in snapshot.collections.items() if item.item_id in members
    ]
    return {
        "CUSTOMER": request.external_customer_ref,
        "CUSTOMER_GROUP": str(request.customer_group_id) if request.customer_group_id else None,
        "ITEM": str(item.item_id),
        "ITEM_COLLECTION": collection_ids[0]
        if len(collection_ids) == 1
        else (",".join(collection_ids) if collection_ids else None),
        "ITEM_CATEGORY": str(item.category_id) if item.category_id else None,
        "QTY": quantity,
        "DATE": request.document_date,
        "CURRENCY": request.currency_code,
    }


def matching_rules(
    snapshot: PricingSnapshot,
    *,
    item: ItemInfo,
    quantity: Decimal,
    request: PriceRequest,
) -> list[RuleRow]:
    ctx = rule_context(snapshot, item=item, quantity=quantity, request=request)
    matched: list[RuleRow] = []
    for rule in snapshot.rules:
        if not rule.is_active or rule.action is None:
            continue
        if rule.start_date is not None and request.document_date < rule.start_date:
            continue
        if rule.end_date is not None and request.document_date > rule.end_date:
            continue
        if rule.currency_code and rule.currency_code != request.currency_code:
            continue
        if _criteria_match(rule.criteria, ctx):
            matched.append(rule)
    return sorted(matched, key=lambda rule: (rule.priority, rule.code))


def select_rule(matched: Sequence[RuleRow]) -> tuple[RuleRow | None, bool]:
    """Apply conflict policy; returns ``(winner, stop_lower_levels)``.

    Rules are already sorted by ``(priority ASC, code ASC)``. ``HIGHER_PRIORITY`` takes the first
    (lowest priority number); ``FIRST_MATCH``/``STOP`` stop at the first match and ``STOP``
    additionally suppresses pricing-group/level/base.
    """
    if not matched:
        return None, False
    winner = matched[0]
    if winner.conflict_policy in {"FIRST_MATCH", "STOP"}:
        return winner, winner.conflict_policy == "STOP"
    # HIGHER_PRIORITY: first in sorted order is the highest priority (lowest number)
    return winner, False


# --------------------------------------------------------------------------- schedule math -----
def _break_for(breaks: Sequence[ScheduleBreak], qty: Decimal) -> ScheduleBreak | None:
    for item in sorted(breaks, key=lambda b: b.qty_from):
        if qty >= item.qty_from and (item.qty_to is None or qty < item.qty_to):
            return item
    return None


def _tiered_amount(breaks: Sequence[ScheduleBreak], qty: Decimal, unit_base: Decimal) -> Decimal:
    """Marginal/tiered value across breaks (per-unit rate semantics)."""
    total = ZERO
    for item in sorted(breaks, key=lambda b: b.qty_from):
        upper = item.qty_to if item.qty_to is not None else Decimal("inf")
        band_qty = min(qty, upper) - item.qty_from
        if band_qty <= 0:
            continue
        if item.pricing_method == "DISCOUNT_PERCENT":
            total += band_qty * unit_base * item.value / Decimal(100)
        elif item.pricing_method == "DISCOUNT_AMOUNT":
            total += band_qty * item.value
        else:  # FIXED_PRICE: value is the per-unit price for the tier; handled by caller
            total += band_qty * item.value
    return total


@dataclass(frozen=True, slots=True)
class ScheduleAdjustment:
    unit_price: Decimal
    extended_price: Decimal
    discount_amount: Decimal
    quantity_basis: Decimal
    calculation_mode: str
    marginal_applied: bool
    break_qty_from: Decimal | None
    break_method: str | None
    schedule_id: UUID | None


def apply_schedule(
    schedule: ScheduleRow,
    *,
    mode: str,
    quantity_basis: Decimal,
    unit_base: Decimal,
    line_quantity: Decimal,
) -> ScheduleAdjustment:
    """Apply one schedule's mode/marginal math to a single line.

    ``quantity_basis`` is the mode basis (line/aggregate/parent/schedule total); for non-marginal
    discounts the single matching break applies to the whole basis; for marginal schedules the
    tiered amount is spread over ``quantity_basis`` and the line receives its quantity share.
    """
    breaks = schedule.breaks
    if not breaks:
        return ScheduleAdjustment(
            unit_price=_q(unit_base),
            extended_price=_q(unit_base * line_quantity),
            discount_amount=ZERO,
            quantity_basis=quantity_basis,
            calculation_mode=mode,
            marginal_applied=schedule.is_marginal,
            break_qty_from=None,
            break_method=None,
            schedule_id=schedule.id,
        )
    if schedule.is_marginal:
        matched = max(
            (item for item in sorted(breaks, key=lambda b: b.qty_from) if quantity_basis >= item.qty_from),
            key=lambda item: item.qty_from,
            default=None,
        )
        if matched is None:
            return ScheduleAdjustment(
                unit_price=_q(unit_base),
                extended_price=_q(unit_base * line_quantity),
                discount_amount=ZERO,
                quantity_basis=quantity_basis,
                calculation_mode=mode,
                marginal_applied=True,
                break_qty_from=None,
                break_method=None,
                schedule_id=schedule.id,
            )
        if matched.pricing_method == "FIXED_PRICE":
            effective_unit = _q(matched.value)
            return ScheduleAdjustment(
                unit_price=effective_unit,
                extended_price=_q(effective_unit * line_quantity),
                discount_amount=_q((unit_base - effective_unit) * line_quantity),
                quantity_basis=quantity_basis,
                calculation_mode=mode,
                marginal_applied=True,
                break_qty_from=matched.qty_from,
                break_method=matched.pricing_method,
                schedule_id=schedule.id,
            )
        total_effect = _tiered_amount(breaks, quantity_basis, unit_base)
        if quantity_basis <= 0:
            per_unit_effect = ZERO
        else:
            per_unit_effect = total_effect / quantity_basis
        discount = _q(per_unit_effect * line_quantity)
        effective_unit = _q(max(ZERO, unit_base - per_unit_effect))
        return ScheduleAdjustment(
            unit_price=effective_unit,
            extended_price=_q(effective_unit * line_quantity),
            discount_amount=discount,
            quantity_basis=quantity_basis,
            calculation_mode=mode,
            marginal_applied=True,
            break_qty_from=matched.qty_from,
            break_method=matched.pricing_method,
            schedule_id=schedule.id,
        )
    matched = _break_for(breaks, quantity_basis)
    if matched is None:
        return ScheduleAdjustment(
            unit_price=_q(unit_base),
            extended_price=_q(unit_base * line_quantity),
            discount_amount=ZERO,
            quantity_basis=quantity_basis,
            calculation_mode=mode,
            marginal_applied=False,
            break_qty_from=None,
            break_method=None,
            schedule_id=schedule.id,
        )
    if matched.pricing_method == "FIXED_PRICE":
        effective_unit = _q(matched.value)
    elif matched.pricing_method == "DISCOUNT_PERCENT":
        effective_unit = _q(unit_base * (Decimal(1) - matched.value / Decimal(100)))
    else:  # DISCOUNT_AMOUNT applied to the whole basis quantity (unit amount distributed)
        unit_discount = matched.value / quantity_basis if quantity_basis > 0 else ZERO
        effective_unit = _q(max(ZERO, unit_base - unit_discount))
    discount = _q((unit_base - effective_unit) * line_quantity)
    return ScheduleAdjustment(
        unit_price=effective_unit,
        extended_price=_q(effective_unit * line_quantity),
        discount_amount=discount,
        quantity_basis=quantity_basis,
        calculation_mode=mode,
        marginal_applied=False,
        break_qty_from=matched.qty_from,
        break_method=matched.pricing_method,
        schedule_id=schedule.id,
    )


# --------------------------------------------------------------------------- resolution --------
def _trace_step(
    steps: list[dict[str, Any]],
    step: str,
    *,
    source: str,
    applied: bool,
    reason: str = "",
    ids: Mapping[str, Any] | None = None,
) -> None:
    entry: dict[str, Any] = {"step": step, "source": source, "applied": applied, "reason": reason}
    if ids:
        entry.update({key: str(value) if value is not None else None for key, value in ids.items()})
    steps.append(entry)


def _level_for_line(
    snapshot: PricingSnapshot,
    *,
    request: PriceRequest,
    item: ItemInfo,
    rule: RuleRow | None,
) -> tuple[UUID | None, UUID | None, Decimal | None]:
    """Return ``(level_id, pricing_group_id, pricing_group_discount_percent)``."""
    if request.price_level_id is not None:
        return request.price_level_id, None, None
    if rule is not None and rule.action is not None and rule.action.action_type == "SET_PRICE_LEVEL":
        if rule.action.price_level_id is not None:
            return rule.action.price_level_id, None, None
    if item.pricing_group_id is not None and request.customer_group_id is not None:
        candidates = [
            group
            for group in snapshot.pricing_groups
            if group.item_group_id == item.pricing_group_id and group.customer_group_id == request.customer_group_id
        ]
        if candidates:
            chosen = sorted(candidates, key=lambda group: (not group.is_default, str(group.id)))[0]
            return chosen.price_level_id, chosen.id, chosen.discount_percent
    return snapshot.default_level_id, None, None


def _resolve_one_line(
    snapshot: PricingSnapshot,
    request: PriceRequest,
    line: PriceLineInput,
    *,
    trace: bool,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    if request.currency_code not in {level.currency_code for level in snapshot.levels.values()} and snapshot.levels:
        # currency may still be a valid document currency with no level; levels are currency-scoped
        pass
    item = snapshot.items.get(line.item_id)
    if item is None:
        raise EngineError("UNKNOWN_ITEM", "item does not exist", details={"item_id": str(line.item_id)})
    if not item.is_active:
        raise EngineError("INACTIVE_REFERENCE", "item is inactive", details={"item_id": str(line.item_id)})
    _trace_step(steps, "1", source="eligibility", applied=True, ids={"item_id": item.item_id})
    qty = _dec(line.quantity)
    rule, stop_lower = select_rule(matching_rules(snapshot, item=item, quantity=qty, request=request))
    if rule is not None:
        _trace_step(
            steps, "2", source="price_rule", applied=True, ids={"price_rule_id": rule.id}, reason=rule.conflict_policy
        )
    level_id, group_id, group_discount = _level_for_line(snapshot, request=request, item=item, rule=rule)
    if group_id is not None:
        _trace_step(steps, "3", source="pricing_group", applied=True, ids={"pricing_group_id": group_id})
    if level_id is None:
        raise EngineError("PRICE_LEVEL_MISSING", "no price level is configured for this tenant")
    if level_id not in snapshot.levels:
        raise EngineError(
            "UNKNOWN_PRICE_LEVEL", "price level does not exist", details={"price_level_id": str(level_id)}
        )
    level = snapshot.levels[level_id]
    if level.is_inactive:
        raise EngineError("INACTIVE_REFERENCE", "price level is inactive", details={"price_level_id": str(level_id)})
    _trace_step(steps, "4", source="price_level", applied=True, ids={"price_level_id": level_id})

    price_row = (
        None
        if stop_lower
        else _select_price(
            snapshot,
            item_id=line.item_id,
            level_id=level_id,
            currency_code=request.currency_code,
            qty=qty,
            document_date=request.document_date,
        )
    )
    fallback_used = False
    if (
        price_row is None
        and not stop_lower
        and snapshot.allow_base_fallback
        and snapshot.base_currency != request.currency_code
    ):
        base_row = _select_price(
            snapshot,
            item_id=line.item_id,
            level_id=level_id,
            currency_code=snapshot.base_currency,
            qty=qty,
            document_date=request.document_date,
        )
        rate = snapshot.fx_rates.get((snapshot.base_currency, request.currency_code, request.document_date))
        if base_row is not None and rate is not None:
            price_row = PriceRow(
                id=base_row.id,
                item_id=base_row.item_id,
                price_level_id=base_row.price_level_id,
                currency_code=request.currency_code,
                qty_min=base_row.qty_min,
                qty_max=base_row.qty_max,
                unit_price=_q(base_row.unit_price * rate),
                effective_from=base_row.effective_from,
                effective_to=base_row.effective_to,
                is_active=base_row.is_active,
                cost_plus_basis=base_row.cost_plus_basis,
                created_at=base_row.created_at,
            )
            fallback_used = True
            _trace_step(steps, "5", source="currency_fallback", applied=True, reason="DM-PRICE-05")
    unit_base = price_row.unit_price if price_row is not None else ZERO
    source = "LEVEL" if price_row is not None else "NONE"
    if price_row is None:
        _trace_step(steps, "6", source="item_base_price", applied=False, reason="NO_PRICE")
    else:
        _trace_step(steps, "6", source="item_base_price", applied=True, ids={"item_price_id": price_row.id})
    if group_discount is not None and price_row is not None:
        unit_base = _q(unit_base * (Decimal(1) - group_discount / Decimal(100)))
        source = "PRICING_GROUP"
    rule_action = rule.action if rule is not None else None
    if rule is not None and rule_action is not None:
        action_type = rule_action.action_type
        if action_type == "FIXED_PRICE":
            unit_base = _q(rule_action.value or ZERO)
            source = "RULE"
        elif action_type == "SET_PRICE_LEVEL":
            source = "RULE"
        elif action_type == "COST_PLUS":
            cost, basis = _item_cost(snapshot, item, request)
            if cost is None:
                raise EngineError(
                    "COST_BASIS_UNAVAILABLE",
                    "cost-plus pricing requires a cost basis",
                    details={"item_id": str(item.item_id)},
                )
            unit_base = _q(cost * (Decimal(1) + (rule_action.value or ZERO) / Decimal(100)))
            source = "RULE"
        elif action_type == "PERCENT_DISCOUNT":
            unit_base = _q(unit_base * (Decimal(1) - (rule_action.value or ZERO) / Decimal(100)))
            source = "RULE"
        elif action_type == "PERCENT_MARKUP":
            unit_base = _q(unit_base * (Decimal(1) + (rule_action.value or ZERO) / Decimal(100)))
            source = "RULE"
        elif action_type == "AMOUNT_DISCOUNT":
            unit_base = _q(max(ZERO, unit_base - (rule_action.value or ZERO)))
            source = "RULE"
        elif action_type == "AMOUNT_MARKUP":
            unit_base = _q(unit_base + (rule_action.value or ZERO))
            source = "RULE"
        _trace_step(steps, "7", source="rule_action", applied=True, ids={"price_rule_id": rule.id}, reason=action_type)

    coupon_code: str | None = None
    pre_coupon_source: str | None = None
    if request.coupon_code:
        coupon, reason = validate_coupon(
            snapshot,
            code=request.coupon_code,
            item=item,
            quantity=qty,
            currency_code=request.currency_code,
            document_date=request.document_date,
        )
        if coupon is None:
            raise EngineError(
                reason or "COUPON_INVALID", "coupon is not applicable", details={"coupon_code": request.coupon_code}
            )
        pre_coupon_source = source
        discount = coupon_discount(coupon, unit_price=unit_base, quantity=qty)
        unit_base = _q(max(ZERO, unit_base - (discount / qty if qty > 0 else ZERO)))
        source = "COUPON"
        coupon_code = coupon.code
        _trace_step(steps, "8", source="coupon", applied=True, ids={"coupon_id": coupon.id})

    schedule_id = item.schedule_id
    schedule = snapshot.schedules.get(schedule_id) if schedule_id else None
    return {
        "line_ref": line.line_ref,
        "item_id": line.item_id,
        "item": item,
        "level_id": level_id,
        "unit_base": _q(unit_base),
        "source": source,
        "pre_coupon_source": pre_coupon_source,
        "coupon_code": coupon_code,
        "coupon_id": snapshot.coupons[coupon_code].id if coupon_code and coupon_code in snapshot.coupons else None,
        "price_row_id": price_row.id if price_row is not None else None,
        "fallback_used": fallback_used,
        "schedule": schedule,
        "quantity": qty,
        "uom": line.uom,
        "parent_item_id": line.parent_item_id or item.matrix_parent_id,
        "steps": steps,
        "effective_from": price_row.effective_from if price_row is not None else None,
        "effective_to": price_row.effective_to if price_row is not None else None,
        "qty_min": price_row.qty_min if price_row is not None else None,
        "qty_max": price_row.qty_max if price_row is not None else None,
        "rule_id": rule.id if rule is not None else None,
        "stop_lower": stop_lower,
    }


def _mode_basis_lines(
    resolved: Sequence[Mapping[str, Any]],
    *,
    mode: str,
) -> dict[Any, list[int]]:
    groups: dict[Any, list[int]] = {}
    for index, row in enumerate(resolved):
        schedule: ScheduleRow | None = row["schedule"]
        if schedule is None:
            continue
        if mode == "LINE":
            key: Any = ("LINE", index)
        elif mode == "OVERALL_ITEM":
            key = ("ITEM", row["item_id"])
        elif mode == "PARENT":
            key = ("PARENT", row["parent_item_id"] or row["item_id"])
        else:
            key = ("SCHEDULE", row["schedule"].id)
        groups.setdefault(key, []).append(index)
    return groups


def resolve_prices(request: PriceRequest, snapshot: PricingSnapshot) -> dict[str, Any]:
    """Run the full deterministic resolution and return the batch response (FS-02 §5.1)."""
    if not request.lines:
        raise EngineError("VALIDATION_FAILED", "at least one line is required")
    if len(request.lines) > 500:
        raise EngineError("VALIDATION_FAILED", "too many lines", details={"max_lines": 500})
    resolved = [_resolve_one_line(snapshot, request, line, trace=request.include_trace) for line in request.lines]
    # Quantity-schedule pass: determine the mode from the first scheduled line; mixed modes per line
    # are resolved with each line's own schedule mode (a request may mix schedules).
    adjustments: dict[int, ScheduleAdjustment] = {}
    handled: set[int] = set()
    for index, row in enumerate(resolved):
        schedule: ScheduleRow | None = row["schedule"]
        if schedule is None or index in handled:
            continue
        mode = schedule.calculation_mode
        groups = _mode_basis_lines(resolved, mode=mode)
        for _key, indexes in groups.items():
            if index not in indexes:
                continue
            relevant = [resolved[i] for i in indexes]
            quantity_basis = sum((row["quantity"] for row in relevant), ZERO)
            for line_index in indexes:
                line_row = resolved[line_index]
                line_adjustment = apply_schedule(
                    line_row["schedule"],
                    mode=mode,
                    quantity_basis=quantity_basis,
                    unit_base=line_row["unit_base"],
                    line_quantity=line_row["quantity"],
                )
                adjustments[line_index] = line_adjustment
                handled.add(line_index)
    payload_lines: list[dict[str, Any]] = []
    for index, row in enumerate(resolved):
        adjustment: ScheduleAdjustment | None = adjustments.get(index)
        if adjustment is not None:
            unit_price = adjustment.unit_price
            extended = adjustment.extended_price
            quantity_basis = adjustment.quantity_basis
            quantity_break = format(adjustment.break_qty_from, "f") if adjustment.break_qty_from is not None else None
            calc_mode = adjustment.calculation_mode
            marginal_applied = adjustment.marginal_applied
            schedule_id = adjustment.schedule_id
            discount_amount = adjustment.discount_amount
            price_source = "SCHEDULE" if row["source"] == "NONE" else row["source"]
        else:
            unit_price = row["unit_base"]
            extended = _q(unit_price * row["quantity"])
            quantity_basis = row["quantity"]
            quantity_break = format(row["qty_min"], "f") if row["qty_min"] is not None else None
            calc_mode = None
            marginal_applied = False
            schedule_id = row["schedule"].id if row["schedule"] is not None else None
            discount_amount = ZERO
            price_source = row["source"]
        effective_at = request.document_date.isoformat()
        line_payload: dict[str, Any] = {
            "line_ref": row["line_ref"],
            "item_id": str(row["item_id"]),
            "uom": row["uom"],
            "quantity": _fmt_qty(row["quantity"]),
            "unit_price": format(unit_price, "f"),
            "extended_price": format(extended, "f"),
            "currency_code": request.currency_code,
            "price_source": price_source,
            "pre_price_source": row["pre_coupon_source"],
            "price_level_id": str(row["level_id"]),
            "price_rule_id": str(row["rule_id"]) if row["rule_id"] is not None else None,
            "coupon_code": row["coupon_code"],
            "schedule_id": str(schedule_id) if schedule_id is not None else None,
            "quantity_break": quantity_break,
            "calculation_mode": calc_mode,
            "quantity_basis": _fmt_qty(quantity_basis),
            "marginal_applied": marginal_applied,
            "fallback_used": row["fallback_used"],
            "discount_amount": _fmt_qty(discount_amount),
            "effective_at": effective_at,
            "policy_version": POLICY_VERSION,
            "warnings": [],
        }
        rules_applied = [str(row["rule_id"])] if row["rule_id"] is not None else []
        line_payload["rules_applied"] = rules_applied
        if not row["steps"] or request.include_trace:
            line_payload["trace"] = row["steps"]
        line_payload["quote_hash"] = _quote_hash(line_payload, request)
        payload_lines.append(line_payload)
    batch = {
        "lines": payload_lines,
        "currency_code": request.currency_code,
        "document_date": request.document_date.isoformat(),
        "policy_version": POLICY_VERSION,
        "priced_quote": build_priced_quote(payload_lines, request),
    }
    return batch


def _quote_payload(line: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "line_ref": line["line_ref"],
        "item_id": line["item_id"],
        "uom": line["uom"],
        "quantity": line["quantity"],
        "unit_price": line["unit_price"],
        "extended_price": line["extended_price"],
        "currency_code": line["currency_code"],
        "price_source": line["price_source"],
        "price_level_id": line["price_level_id"],
        "price_rule_id": line["price_rule_id"],
        "coupon_code": line["coupon_code"],
        "schedule_id": line["schedule_id"],
        "quantity_break": line["quantity_break"],
        "calculation_mode": line["calculation_mode"],
        "quantity_basis": line["quantity_basis"],
        "marginal_applied": line["marginal_applied"],
        "fallback_used": line["fallback_used"],
        "effective_at": line["effective_at"],
        "policy_version": line["policy_version"],
    }


def _quote_hash(line: Mapping[str, Any], request: PriceRequest) -> str:
    return sha256_hex(
        canonical_json(
            {
                "line": _quote_payload(line),
                "customer": request.external_customer_ref,
                "customer_group_id": str(request.customer_group_id) if request.customer_group_id else None,
                "document_date": request.document_date.isoformat(),
            }
        )
    )


def build_priced_quote(lines: Sequence[Mapping[str, Any]], request: PriceRequest) -> dict[str, Any]:
    """Self-contained immutable quote for the external order boundary (D3/Δ-2; no DB persistence)."""
    quote_lines = [
        {
            **_quote_payload(line),
            "quote_hash": line.get("quote_hash"),
            "rules_applied": list(line.get("rules_applied") or []),
        }
        for line in lines
    ]
    payload = {
        "policy_version": POLICY_VERSION,
        "currency_code": request.currency_code,
        "document_date": request.document_date.isoformat(),
        "external_customer_ref": request.external_customer_ref,
        "customer_group_id": str(request.customer_group_id) if request.customer_group_id else None,
        "lines": quote_lines,
    }
    return {
        **payload,
        "quote_hash": sha256_hex(canonical_json(payload)),
        "line_count": len(quote_lines),
    }


def coupon_validation_payload(
    snapshot: PricingSnapshot,
    *,
    code: str,
    item: ItemInfo,
    quantity: Decimal,
    unit_price: Decimal,
    currency_code: str,
    document_date: date,
) -> dict[str, Any]:
    """Side-effect-free coupon validation result for ``POST /api/v1/coupons/validate``."""
    coupon, reason = validate_coupon(
        snapshot,
        code=code,
        item=item,
        quantity=quantity,
        currency_code=currency_code,
        document_date=document_date,
    )
    if coupon is None:
        return {"valid": False, "reason": reason}
    discount = coupon_discount(coupon, unit_price=unit_price, quantity=quantity)
    extended = unit_price * quantity
    result: dict[str, Any] = {"valid": True, "reason": None, "discount_amount": format(discount, "f")}
    if coupon.coupon_type == "PERCENT":
        result["discount_percent"] = format(coupon.value, "f")
    else:
        result["discount_percent"] = format((discount / extended * Decimal(100)) if extended else ZERO, "f")
    result["coupon_code"] = coupon.code
    result["uses_remaining"] = None if coupon.max_uses is None else max(0, coupon.max_uses - coupon.uses_count)
    return result


__all__ = [
    "POLICY_VERSION",
    "CostContext",
    "CouponRow",
    "EngineError",
    "ItemInfo",
    "PriceLevelInfo",
    "PriceLineInput",
    "PriceRequest",
    "PriceRow",
    "PricingGroupRow",
    "PricingSnapshot",
    "RuleAction",
    "RuleCriterion",
    "RuleRow",
    "ScheduleAdjustment",
    "ScheduleBreak",
    "ScheduleRow",
    "apply_schedule",
    "build_priced_quote",
    "coupon_discount",
    "coupon_validation_payload",
    "matching_rules",
    "resolve_prices",
    "rule_context",
    "select_rule",
    "validate_coupon",
]
