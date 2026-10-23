"""FS-02 invariant suite: price-resolution order and determinism (DM-PRICE-01..07).

Pure engine invariants that must hold for every resolution (no database needed):
DM-PRICE-01 precedence order, DM-PRICE-02 quantity modes, DM-PRICE-03 effective-date selection,
DM-PRICE-04 source recording, DM-PRICE-05 currency fallback, DM-PRICE-06 coupon usage gates,
DM-PRICE-07 deterministic priced quote.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.modules.pricing.engine import (
    CouponRow,
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingGroupRow,
    PricingSnapshot,
    RuleAction,
    RuleRow,
    resolve_prices,
)

ITEM = UUID("a0000000-0000-7000-8000-000000000001")
CATEGORY = UUID("a0000000-0000-7000-8000-000000000002")
LEVEL_BASE = UUID("a0000000-0000-7000-8000-000000000010")
LEVEL_RULE = UUID("a0000000-0000-7000-8000-000000000011")
LEVEL_GROUP = UUID("a0000000-0000-7000-8000-000000000012")
ITEM_GROUP = UUID("a0000000-0000-7000-8000-000000000020")
CUSTOMER_GROUP = UUID("a0000000-0000-7000-8000-000000000021")
RULE = UUID("a0000000-0000-7000-8000-000000000030")
COUPON = UUID("a0000000-0000-7000-8000-000000000040")
DOC_DATE = date(2026, 10, 21)


def _price(level: UUID, price: str, *, qty_min: str = "0", currency: str = "USD") -> PriceRow:
    return PriceRow(
        id=uuid4(),
        item_id=ITEM,
        price_level_id=level,
        currency_code=currency,
        qty_min=Decimal(qty_min),
        qty_max=None,
        unit_price=Decimal(price),
        effective_from=date(2026, 1, 1),
        effective_to=None,
        is_active=True,
        created_at=0,
    )


def _base_snapshot(**overrides: object) -> PricingSnapshot:
    snapshot = PricingSnapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY)},
        levels={
            LEVEL_BASE: PriceLevelInfo(
                id=LEVEL_BASE, code="BASE", currency_code="USD", is_inactive=False, is_default=True
            ),
            LEVEL_RULE: PriceLevelInfo(
                id=LEVEL_RULE, code="RULE", currency_code="USD", is_inactive=False, is_default=False
            ),
            LEVEL_GROUP: PriceLevelInfo(
                id=LEVEL_GROUP, code="GROUP", currency_code="USD", is_inactive=False, is_default=False
            ),
        },
        default_level_id=LEVEL_BASE,
        prices=(_price(LEVEL_BASE, "10"),),
    )
    return (
        snapshot
        if not overrides
        else PricingSnapshot(
            items=overrides.get("items", snapshot.items),
            levels=overrides.get("levels", snapshot.levels),
            default_level_id=overrides.get("default_level_id", snapshot.default_level_id),
            prices=overrides.get("prices", snapshot.prices),
            pricing_groups=overrides.get("pricing_groups", ()),
            schedules=overrides.get("schedules", {}),
            rules=overrides.get("rules", ()),
            collections=overrides.get("collections", {}),
            coupons=overrides.get("coupons", {}),
            base_currency=overrides.get("base_currency", "USD"),
            allow_base_fallback=overrides.get("allow_base_fallback", False),
            fx_rates=overrides.get("fx_rates", {}),
        )
    )


def _request(**overrides: object) -> PriceRequest:
    base: dict[str, object] = {
        "lines": (PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("3"), uom="EA"),),
        "currency_code": "USD",
        "document_date": DOC_DATE,
    }
    base.update(overrides)
    return PriceRequest(**base)  # type: ignore[arg-type]


def test_dm_price_01_precedence_order() -> None:
    """DM-PRICE-01: coupon > rule > pricing group > price level > base."""
    coupon = CouponRow(
        id=COUPON,
        code="C10",
        coupon_type="PERCENT",
        value=Decimal("10"),
        currency_code="USD",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        max_uses=None,
        uses_count=0,
        is_active=True,
    )
    group = PricingGroupRow(
        id=uuid4(),
        item_group_id=ITEM_GROUP,
        customer_group_id=CUSTOMER_GROUP,
        price_level_id=LEVEL_GROUP,
        is_default=True,
        discount_percent=Decimal("10"),
    )
    rule = RuleRow(
        id=RULE,
        code="R",
        rule_type="FIXED_PRICE",
        priority=1,
        conflict_policy="HIGHER_PRIORITY",
        is_active=True,
        action=RuleAction(action_type="FIXED_PRICE", value=Decimal("20")),
    )
    snapshot = _base_snapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, pricing_group_id=ITEM_GROUP)},
        prices=(_price(LEVEL_BASE, "10"), _price(LEVEL_GROUP, "30")),
        pricing_groups=(group,),
        rules=(rule,),
        coupons={"C10": coupon},
    )
    without_coupon = resolve_prices(_request(customer_group_id=CUSTOMER_GROUP), snapshot)["lines"][0]
    assert without_coupon["price_source"] == "RULE"
    assert without_coupon["unit_price"] == "20.000000000000"
    with_coupon = resolve_prices(_request(customer_group_id=CUSTOMER_GROUP, coupon_code="C10"), snapshot)["lines"][0]
    assert with_coupon["price_source"] == "COUPON"
    assert with_coupon["unit_price"] == "18.000000000000"


def test_dm_price_02_quantity_mode_basis() -> None:
    """DM-PRICE-02: every resolved line exposes a deterministic quantity basis."""
    result = resolve_prices(_request(), _base_snapshot())["lines"][0]
    assert result["quantity_basis"] == "3.000000000000"
    assert result["calculation_mode"] is None
    assert result["marginal_applied"] is False


def test_dm_price_03_effective_dating() -> None:
    """DM-PRICE-03: the effective row for the document date is selected (not future/expired rows)."""
    snapshot = _base_snapshot(
        prices=(
            PriceRow(
                id=uuid4(),
                item_id=ITEM,
                price_level_id=LEVEL_BASE,
                currency_code="USD",
                qty_min=Decimal(0),
                qty_max=None,
                unit_price=Decimal("99"),
                effective_from=date(2027, 1, 1),
                effective_to=None,
                is_active=True,
                created_at=1,
            ),
            PriceRow(
                id=uuid4(),
                item_id=ITEM,
                price_level_id=LEVEL_BASE,
                currency_code="USD",
                qty_min=Decimal(0),
                qty_max=None,
                unit_price=Decimal("11"),
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 12, 31),
                is_active=True,
                created_at=2,
            ),
        )
    )
    assert resolve_prices(_request(), snapshot)["lines"][0]["unit_price"] == "11.000000000000"


def test_dm_price_04_source_recorded() -> None:
    """DM-PRICE-04: every line records price_source and the resolved level."""
    line = resolve_prices(_request(), _base_snapshot())["lines"][0]
    assert line["price_source"] in {"LEVEL", "PRICING_GROUP", "RULE", "COUPON", "SCHEDULE", "NONE"}
    assert line["price_level_id"] == str(LEVEL_BASE)


def test_dm_price_05_currency_fallback() -> None:
    """DM-PRICE-05: fallback only when enabled and an FX rate exists; marked on the line."""
    snapshot = _base_snapshot(
        base_currency="USD",
        allow_base_fallback=True,
        fx_rates={("USD", "EUR", DOC_DATE): Decimal("0.8")},
    )
    line = resolve_prices(_request(currency_code="EUR"), snapshot)["lines"][0]
    assert line["fallback_used"] is True
    assert line["unit_price"] == "8.000000000000"
    disabled = _base_snapshot(base_currency="USD", allow_base_fallback=False)
    no_fallback = resolve_prices(_request(currency_code="EUR"), disabled)["lines"][0]
    assert no_fallback["fallback_used"] is False
    assert no_fallback["price_source"] == "NONE"


def test_dm_price_06_coupon_usage_gates() -> None:
    """DM-PRICE-06: expired/exhausted/window-mismatched coupons are rejected before pricing."""
    import pytest

    from app.modules.pricing.engine import EngineError

    exhausted = CouponRow(
        id=COUPON,
        code="EX",
        coupon_type="AMOUNT",
        value=Decimal("5"),
        currency_code="USD",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        max_uses=2,
        uses_count=2,
        is_active=True,
    )
    snapshot = _base_snapshot(coupons={"EX": exhausted})
    with pytest.raises(EngineError) as excinfo:
        resolve_prices(_request(coupon_code="EX"), snapshot)
    assert excinfo.value.code == "COUPON_EXHAUSTED"


def test_dm_price_07_deterministic_quote() -> None:
    """DM-PRICE-07: identical inputs produce byte-identical line quotes and quote hashes."""
    first = resolve_prices(_request(), _base_snapshot())
    second = resolve_prices(_request(), _base_snapshot())
    assert first["lines"][0]["quote_hash"] == second["lines"][0]["quote_hash"]
    assert first["priced_quote"]["quote_hash"] == second["priced_quote"]["quote_hash"]
    assert first["priced_quote"]["policy_version"] == "pricing-engine@1.0.0"
