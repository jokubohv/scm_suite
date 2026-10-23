"""Unit tests for coupon math and usage gates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from app.modules.pricing.engine import CouponRow, coupon_discount, validate_coupon

COUPON_ID = UUID("a1110000-0000-7000-8000-000000000001")
ITEM = {"item_id": UUID("a1110000-0000-7000-8000-000000000002")}


def _coupon(**overrides: object) -> CouponRow:
    base: dict[str, object] = {
        "id": COUPON_ID,
        "code": "C10",
        "coupon_type": "PERCENT",
        "value": Decimal("10"),
        "currency_code": "USD",
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 12, 31),
        "max_uses": None,
        "uses_count": 0,
        "is_active": True,
    }
    base.update(overrides)
    return CouponRow(**base)  # type: ignore[arg-type]


def test_percent_and_amount_discount_clamped() -> None:
    percent = coupon_discount(_coupon(), unit_price=Decimal("10"), quantity=Decimal("3"))
    assert percent == Decimal("3.000000000000") or percent == Decimal("3")
    amount = coupon_discount(
        _coupon(coupon_type="AMOUNT", value=Decimal("100")), unit_price=Decimal("10"), quantity=Decimal("1")
    )
    assert amount == Decimal("10")


def test_usage_window_and_currency_gates() -> None:
    from app.modules.pricing.engine import ItemInfo, PricingSnapshot

    item = ItemInfo(item_id=UUID("a1110000-0000-7000-8000-000000000002"), is_active=True)
    snapshot = PricingSnapshot(
        items={item.item_id: item}, levels={}, default_level_id=None, prices=(), coupons={"C10": _coupon()}
    )
    valid, reason = validate_coupon(
        snapshot, code="C10", item=item, quantity=Decimal("1"), currency_code="USD", document_date=date(2026, 10, 21)
    )
    assert valid is not None and reason is None
    depleted = _coupon(max_uses=1, uses_count=1)
    snapshot2 = PricingSnapshot(
        items={item.item_id: item}, levels={}, default_level_id=None, prices=(), coupons={"C10": depleted}
    )
    invalid, reason2 = validate_coupon(
        snapshot2, code="C10", item=item, quantity=Decimal("1"), currency_code="USD", document_date=date(2026, 10, 21)
    )
    assert invalid is None and reason2 == "COUPON_EXHAUSTED"
