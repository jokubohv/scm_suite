"""Pure unit tests for the deterministic FS-02 pricing engine (precedence, modes, rules, coupons)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.modules.pricing.engine import (
    CostContext,
    CouponRow,
    EngineError,
    ItemInfo,
    PriceLevelInfo,
    PriceLineInput,
    PriceRequest,
    PriceRow,
    PricingGroupRow,
    PricingSnapshot,
    RuleAction,
    RuleCriterion,
    RuleRow,
    ScheduleBreak,
    ScheduleRow,
    build_priced_quote,
    resolve_prices,
)

ITEM = UUID("10000000-0000-7000-8000-000000000001")
PARENT = UUID("10000000-0000-7000-8000-0000000000ff")
CATEGORY = UUID("30000000-0000-7000-8000-000000000001")
LEVEL_DEFAULT = UUID("40000000-0000-7000-8000-000000000001")
LEVEL_GROUP = UUID("40000000-0000-7000-8000-000000000002")
LEVEL_RULE = UUID("40000000-0000-7000-8000-000000000003")
ITEM_GROUP = UUID("50000000-0000-7000-8000-000000000001")
CUSTOMER_GROUP = UUID("60000000-0000-7000-8000-000000000001")
SCHEDULE = UUID("70000000-0000-7000-8000-000000000001")
RULE = UUID("80000000-0000-7000-8000-000000000001")
COUPON_ID = UUID("90000000-0000-7000-8000-000000000001")
DOC_DATE = date(2026, 10, 21)


def _price(
    level: UUID = LEVEL_DEFAULT,
    *,
    price: str = "10",
    qty_min: str = "0",
    qty_max: str | None = None,
    currency: str = "USD",
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> PriceRow:
    return PriceRow(
        id=uuid4(),
        item_id=ITEM,
        price_level_id=level,
        currency_code=currency,
        qty_min=Decimal(qty_min),
        qty_max=Decimal(qty_max) if qty_max else None,
        unit_price=Decimal(price),
        effective_from=effective_from,
        effective_to=effective_to,
        is_active=True,
        created_at=0,
    )


def _snapshot(**overrides: object) -> PricingSnapshot:
    base = dict(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY)},
        levels={
            LEVEL_DEFAULT: PriceLevelInfo(
                id=LEVEL_DEFAULT, code="DEFAULT", currency_code="USD", is_inactive=False, is_default=True
            ),
            LEVEL_GROUP: PriceLevelInfo(
                id=LEVEL_GROUP, code="GROUP", currency_code="USD", is_inactive=False, is_default=False
            ),
            LEVEL_RULE: PriceLevelInfo(
                id=LEVEL_RULE, code="RULE", currency_code="USD", is_inactive=False, is_default=False
            ),
        },
        default_level_id=LEVEL_DEFAULT,
        prices=(_price(),),
    )
    base.update(overrides)
    return PricingSnapshot(**base)  # type: ignore[arg-type]


def _request(**overrides: object) -> PriceRequest:
    base: dict[str, object] = {
        "lines": (PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("5"), uom="EA"),),
        "currency_code": "USD",
        "document_date": DOC_DATE,
    }
    base.update(overrides)
    return PriceRequest(**base)  # type: ignore[arg-type]


def _rule(**overrides: object) -> RuleRow:
    base: dict[str, object] = {
        "id": RULE,
        "code": "R1",
        "rule_type": "FIXED_PRICE",
        "priority": 10,
        "conflict_policy": "HIGHER_PRIORITY",
        "is_active": True,
        "criteria": (),
        "action": RuleAction(action_type="FIXED_PRICE", value=Decimal("7")),
    }
    base.update(overrides)
    return RuleRow(**base)  # type: ignore[arg-type]


class TestPrecedence:
    def test_base_level_price(self) -> None:
        result = resolve_prices(_request(), _snapshot())
        line = result["lines"][0]
        assert line["unit_price"] == "10.000000000000"
        assert line["price_source"] == "LEVEL"
        assert line["price_level_id"] == str(LEVEL_DEFAULT)
        assert line["policy_version"] == "pricing-engine@1.0.0"
        assert len(line["quote_hash"]) == 64

    def test_rule_price_overrides_level(self) -> None:
        snapshot = _snapshot(rules=(_rule(),))
        line = resolve_prices(_request(), snapshot)["lines"][0]
        assert line["unit_price"] == "7.000000000000"
        assert line["price_source"] == "RULE"

    def test_rule_set_price_level(self) -> None:
        snapshot = _snapshot(
            prices=(_price(level=LEVEL_RULE, price="4"), _price(level=LEVEL_DEFAULT)),
            rules=(_rule(action=RuleAction(action_type="SET_PRICE_LEVEL", price_level_id=LEVEL_RULE)),),
        )
        line = resolve_prices(_request(), snapshot)["lines"][0]
        assert line["price_level_id"] == str(LEVEL_RULE)
        assert line["price_source"] == "RULE"

    def test_pricing_group_level_and_discount(self) -> None:
        group = PricingGroupRow(
            id=uuid4(),
            item_group_id=ITEM_GROUP,
            customer_group_id=CUSTOMER_GROUP,
            price_level_id=LEVEL_GROUP,
            is_default=True,
            discount_percent=Decimal("10"),
        )
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, pricing_group_id=ITEM_GROUP)},
            prices=(_price(level=LEVEL_GROUP, price="20"), _price(level=LEVEL_DEFAULT, price="10")),
            pricing_groups=(group,),
        )
        line = resolve_prices(_request(customer_group_id=CUSTOMER_GROUP), snapshot)["lines"][0]
        assert line["price_level_id"] == str(LEVEL_GROUP)
        assert line["unit_price"] == "18.000000000000"
        assert line["price_source"] == "PRICING_GROUP"

    def test_coupon_applies_last_and_records_pre_price_source(self) -> None:
        coupon = CouponRow(
            id=COUPON_ID,
            code="SAVE10",
            coupon_type="PERCENT",
            value=Decimal("10"),
            currency_code="USD",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            max_uses=100,
            uses_count=0,
            is_active=True,
        )
        snapshot = _snapshot(rules=(_rule(),), coupons={"SAVE10": coupon})
        line = resolve_prices(_request(coupon_code="SAVE10"), snapshot)["lines"][0]
        assert line["unit_price"] == "6.300000000000"  # 7 - 10%
        assert line["price_source"] == "COUPON"
        assert line["pre_price_source"] == "RULE"
        assert line["coupon_code"] == "SAVE10"

    def test_stop_rule_suppresses_lower_levels(self) -> None:
        snapshot = _snapshot(
            rules=(_rule(conflict_policy="STOP", action=RuleAction(action_type="AMOUNT_MARKUP", value=Decimal("2"))),)
        )
        line = resolve_prices(_request(), snapshot)["lines"][0]
        assert line["unit_price"] == "2.000000000000"
        assert line["price_source"] == "RULE"

    def test_higher_priority_wins(self) -> None:
        low = _rule(
            id=UUID("80000000-0000-7000-8000-00000000000a"),
            code="LOW",
            priority=50,
            action=RuleAction(action_type="FIXED_PRICE", value=Decimal("9")),
        )
        high = _rule(
            id=UUID("80000000-0000-7000-8000-00000000000b"),
            code="HIGH",
            priority=1,
            action=RuleAction(action_type="FIXED_PRICE", value=Decimal("6")),
        )
        line = resolve_prices(_request(), _snapshot(rules=(low, high)))["lines"][0]
        assert line["unit_price"] == "6.000000000000"
        assert line["price_rule_id"] == str(high.id)

    def test_criteria_group_and_filtering(self) -> None:
        criteria = (RuleCriterion(criterion="ITEM_CATEGORY", operator="EQ", value_text=str(CATEGORY)),)
        matching = _rule(criteria=criteria)
        line = resolve_prices(_request(), _snapshot(rules=(matching,)))["lines"][0]
        assert line["price_source"] == "RULE"
        other = _rule(criteria=(RuleCriterion(criterion="ITEM_CATEGORY", operator="EQ", value_text=str(uuid4())),))
        line2 = resolve_prices(_request(), _snapshot(rules=(other,)))["lines"][0]
        assert line2["price_source"] == "LEVEL"

    def test_cost_plus_rule(self) -> None:
        snapshot = _snapshot(
            rules=(_rule(action=RuleAction(action_type="COST_PLUS", value=Decimal("25"), cost_basis="AVERAGE")),)
        )
        line = resolve_prices(
            _request(cost_context=CostContext(unit_cost=Decimal("8"), cost_basis="AVERAGE")), snapshot
        )["lines"][0]
        assert line["unit_price"] == "10.000000000000"

    def test_currency_fallback_with_allow_flag(self) -> None:
        snapshot = _snapshot(
            prices=(_price(currency="USD", price="10"),),
            base_currency="USD",
            allow_base_fallback=True,
            fx_rates={("USD", "EUR", DOC_DATE): Decimal("0.9")},
        )
        line = resolve_prices(_request(currency_code="EUR"), snapshot)["lines"][0]
        assert line["unit_price"] == "9.000000000000"
        assert line["fallback_used"] is True

    def test_no_price_returns_zero_none(self) -> None:
        snapshot = _snapshot(prices=(), default_level_id=LEVEL_DEFAULT)
        line = resolve_prices(_request(), snapshot)["lines"][0]
        assert line["unit_price"] == "0.000000000000"
        assert line["price_source"] == "NONE"

    def test_effective_date_and_band_selection(self) -> None:
        snapshot = _snapshot(
            prices=(
                _price(price="10", effective_from=date(2026, 1, 1), effective_to=date(2026, 6, 30)),
                _price(price="12", effective_from=date(2026, 7, 1)),
                _price(price="99", effective_from=date(2027, 1, 1)),
                _price(price="8", qty_min="10", effective_from=date(2026, 7, 1)),
            )
        )
        line = resolve_prices(_request(), snapshot)["lines"][0]
        assert line["unit_price"] == "12.000000000000"
        bulk = resolve_prices(
            _request(lines=(PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("10"), uom="EA"),)), snapshot
        )["lines"][0]
        assert bulk["unit_price"] == "8.000000000000"
        assert bulk["quantity_break"] == "10"


class TestQuantityModes:
    def _schedule(self, mode: str, *, marginal: bool = False, method: str = "FIXED_PRICE") -> ScheduleRow:
        return ScheduleRow(
            id=SCHEDULE,
            code="QS",
            calculation_mode=mode,
            is_marginal=marginal,
            currency_code="USD",
            breaks=(
                ScheduleBreak(qty_from=Decimal("0"), qty_to=Decimal("10"), pricing_method=method, value=Decimal("10")),
                ScheduleBreak(qty_from=Decimal("10"), qty_to=None, pricing_method=method, value=Decimal("8")),
            ),
        )

    def test_line_mode_uses_each_line_qty(self) -> None:
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, schedule_id=SCHEDULE)},
            schedules={SCHEDULE: self._schedule("LINE")},
        )
        lines = (
            PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("5"), uom="EA"),
            PriceLineInput(line_ref="2", item_id=ITEM, quantity=Decimal("12"), uom="EA"),
        )
        result = resolve_prices(_request(lines=lines), snapshot)
        assert result["lines"][0]["unit_price"] == "10.000000000000"
        assert result["lines"][1]["unit_price"] == "8.000000000000"
        assert result["lines"][1]["calculation_mode"] == "LINE"

    def test_overall_item_mode_aggregates_qty(self) -> None:
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, schedule_id=SCHEDULE)},
            schedules={SCHEDULE: self._schedule("OVERALL_ITEM")},
        )
        lines = (
            PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("5"), uom="EA"),
            PriceLineInput(line_ref="2", item_id=ITEM, quantity=Decimal("6"), uom="EA"),
        )
        result = resolve_prices(_request(lines=lines), snapshot)
        assert [line["unit_price"] for line in result["lines"]] == ["8.000000000000", "8.000000000000"]
        assert result["lines"][0]["quantity_basis"] == "11.000000000000"

    def test_parent_mode_uses_parent_quantity(self) -> None:
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, schedule_id=SCHEDULE)},
            schedules={SCHEDULE: self._schedule("PARENT")},
        )
        lines = (
            PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("4"), uom="EA", parent_item_id=PARENT),
            PriceLineInput(line_ref="2", item_id=ITEM, quantity=Decimal("8"), uom="EA", parent_item_id=PARENT),
        )
        result = resolve_prices(_request(lines=lines), snapshot)
        assert result["lines"][0]["calculation_mode"] == "PARENT"
        assert result["lines"][0]["quantity_basis"] == "12.000000000000"
        assert result["lines"][0]["unit_price"] == "8.000000000000"

    def test_schedule_mode_groups_all_lines_with_schedule(self) -> None:
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, schedule_id=SCHEDULE)},
            schedules={SCHEDULE: self._schedule("SCHEDULE")},
        )
        lines = (
            PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("2"), uom="EA"),
            PriceLineInput(line_ref="2", item_id=ITEM, quantity=Decimal("9"), uom="EA"),
        )
        result = resolve_prices(_request(lines=lines), snapshot)
        assert {line["calculation_mode"] for line in result["lines"]} == {"SCHEDULE"}
        assert result["lines"][0]["quantity_basis"] == "11.000000000000"

    def test_marginal_percent_tiers(self) -> None:
        schedule = ScheduleRow(
            id=SCHEDULE,
            code="M",
            calculation_mode="LINE",
            is_marginal=True,
            currency_code="USD",
            breaks=(
                ScheduleBreak(
                    qty_from=Decimal("0"), qty_to=Decimal("10"), pricing_method="DISCOUNT_PERCENT", value=Decimal("10")
                ),
                ScheduleBreak(
                    qty_from=Decimal("10"), qty_to=None, pricing_method="DISCOUNT_PERCENT", value=Decimal("20")
                ),
            ),
        )
        snapshot = _snapshot(
            items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY, schedule_id=SCHEDULE)},
            schedules={SCHEDULE: schedule},
        )
        result = resolve_prices(
            _request(lines=(PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("20"), uom="EA"),)), snapshot
        )
        # tier: 10 * 10% of 10 = 10 discount + 10 * 20% of 10 = 20 => 30 total over 20 units => 1.5/unit
        line = result["lines"][0]
        assert line["marginal_applied"] is True
        assert line["unit_price"] == "8.500000000000"
        assert line["discount_amount"] == "30.000000000000"


class TestErrorsAndQuote:
    def test_unknown_and_inactive_items(self) -> None:
        with pytest.raises(EngineError) as excinfo:
            resolve_prices(
                _request(lines=(PriceLineInput(line_ref="1", item_id=uuid4(), quantity=Decimal("1"), uom="EA"),)),
                _snapshot(),
            )
        assert excinfo.value.code == "UNKNOWN_ITEM"
        inactive = _snapshot(items={ITEM: ItemInfo(item_id=ITEM, is_active=False)})
        with pytest.raises(EngineError) as excinfo2:
            resolve_prices(_request(), inactive)
        assert excinfo2.value.code == "INACTIVE_REFERENCE"

    def test_invalid_or_exhausted_coupon(self) -> None:
        coupon = CouponRow(
            id=COUPON_ID,
            code="USED",
            coupon_type="AMOUNT",
            value=Decimal("2"),
            currency_code="USD",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            max_uses=1,
            uses_count=1,
            is_active=True,
        )
        snapshot = _snapshot(coupons={"USED": coupon})
        with pytest.raises(EngineError) as excinfo:
            resolve_prices(_request(coupon_code="USED"), snapshot)
        assert excinfo.value.code == "COUPON_EXHAUSTED"

    def test_quote_hash_is_deterministic_and_quote_shape(self) -> None:
        snapshot = _snapshot()
        first = resolve_prices(_request(), snapshot)
        second = resolve_prices(_request(), snapshot)
        assert first["lines"][0]["quote_hash"] == second["lines"][0]["quote_hash"]
        quote = first["priced_quote"]
        assert quote["policy_version"] == "pricing-engine@1.0.0"
        assert quote["line_count"] == 1
        assert len(quote["quote_hash"]) == 64
        assert quote["lines"][0]["currency_code"] == "USD"

    def test_build_priced_quote_pins_lines(self) -> None:
        result = resolve_prices(_request(), _snapshot())
        quote = build_priced_quote(result["lines"], _request())
        assert quote["lines"][0]["quote_hash"] == result["lines"][0]["quote_hash"]
        assert quote["external_customer_ref"] is None
