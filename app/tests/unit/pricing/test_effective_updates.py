"""Unit tests for effective-dated price selection rules (apply/revert semantics are DB integration tests)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.modules.pricing.engine import (
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingSnapshot,
    resolve_prices,
)

ITEM = UUID("a2220000-0000-7000-8000-000000000001")
LEVEL = UUID("a2220000-0000-7000-8000-000000000010")


def _price(price: str, effective_from: date, effective_to: date | None = None) -> PriceRow:
    return PriceRow(
        id=uuid4(),
        item_id=ITEM,
        price_level_id=LEVEL,
        currency_code="USD",
        qty_min=Decimal(0),
        qty_max=None,
        unit_price=Decimal(price),
        effective_from=effective_from,
        effective_to=effective_to,
        is_active=True,
        created_at=0,
    )


def _snapshot(prices: tuple[PriceRow, ...]) -> PricingSnapshot:
    return PricingSnapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True)},
        levels={LEVEL: PriceLevelInfo(id=LEVEL, code="D", currency_code="USD", is_inactive=False, is_default=True)},
        default_level_id=LEVEL,
        prices=prices,
    )


def _request(as_of: date) -> PriceRequest:
    return PriceRequest(
        lines=(PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("1"), uom="EA"),),
        currency_code="USD",
        document_date=as_of,
    )


def test_window_selection_respects_document_date() -> None:
    snapshot = _snapshot((_price("10", date(2026, 1, 1), date(2026, 6, 30)), _price("12", date(2026, 7, 1))))
    assert resolve_prices(_request(date(2026, 5, 1)), snapshot)["lines"][0]["unit_price"] == "10.000000000000"
    assert resolve_prices(_request(date(2026, 8, 1)), snapshot)["lines"][0]["unit_price"] == "12.000000000000"


def test_future_rows_never_apply_early() -> None:
    snapshot = _snapshot((_price("10", date(2026, 1, 1)), _price("99", date(2027, 1, 1))))
    assert resolve_prices(_request(date(2026, 12, 31)), snapshot)["lines"][0]["unit_price"] == "10.000000000000"


def test_latest_effective_date_wins_for_same_band() -> None:
    snapshot = _snapshot((_price("10", date(2026, 1, 1)), _price("11", date(2026, 9, 1))))
    assert resolve_prices(_request(date(2026, 10, 21)), snapshot)["lines"][0]["unit_price"] == "11.000000000000"
