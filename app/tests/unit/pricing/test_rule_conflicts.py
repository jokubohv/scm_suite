"""Unit tests for rule conflict policies and criteria evaluation."""

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
    RuleAction,
    RuleCriterion,
    RuleRow,
    matching_rules,
    select_rule,
)

ITEM = UUID("f0000000-0000-7000-8000-000000000001")
CATEGORY = UUID("f0000000-0000-7000-8000-000000000002")
LEVEL = UUID("f0000000-0000-7000-8000-000000000010")


def _rule(**overrides: object) -> RuleRow:
    base: dict[str, object] = {
        "id": uuid4(),
        "code": "R",
        "rule_type": "FIXED_PRICE",
        "priority": 10,
        "conflict_policy": "HIGHER_PRIORITY",
        "is_active": True,
        "action": RuleAction(action_type="FIXED_PRICE", value=Decimal("1")),
    }
    base.update(overrides)
    return RuleRow(**base)  # type: ignore[arg-type]


def _snapshot(rules: tuple[RuleRow, ...]) -> PricingSnapshot:
    return PricingSnapshot(
        items={ITEM: ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY)},
        levels={LEVEL: PriceLevelInfo(id=LEVEL, code="D", currency_code="USD", is_inactive=False, is_default=True)},
        default_level_id=LEVEL,
        prices=(
            PriceRow(
                id=uuid4(),
                item_id=ITEM,
                price_level_id=LEVEL,
                currency_code="USD",
                qty_min=Decimal(0),
                qty_max=None,
                unit_price=Decimal("10"),
                effective_from=date(2026, 1, 1),
                effective_to=None,
                is_active=True,
                created_at=0,
            ),
        ),
        rules=rules,
    )


def _request() -> PriceRequest:
    return PriceRequest(
        lines=(PriceLineInput(line_ref="1", item_id=ITEM, quantity=Decimal("1"), uom="EA"),),
        currency_code="USD",
        document_date=date(2026, 10, 21),
    )


def test_priority_and_conflict_policy_selection() -> None:
    low = _rule(code="LOW", priority=50)
    high = _rule(code="HIGH", priority=1)
    matched = matching_rules(
        _snapshot((low, high)),
        item=ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY),
        quantity=Decimal(1),
        request=_request(),
    )
    winner, stop = select_rule(matched)
    assert winner is not None and winner.code == "HIGH"
    assert stop is False


def test_stop_policy_sets_stop_flag() -> None:
    rule = _rule(conflict_policy="STOP")
    matched = matching_rules(
        _snapshot((rule,)),
        item=ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY),
        quantity=Decimal(1),
        request=_request(),
    )
    winner, stop = select_rule(matched)
    assert winner is not None and stop is True


def test_and_or_criteria_groups() -> None:
    rule = _rule(
        criteria=(
            RuleCriterion(criterion="ITEM_CATEGORY", operator="EQ", value_text=str(CATEGORY), group_no=1),
            RuleCriterion(criterion="QTY", operator="GTE", value_number=Decimal("1"), group_no=1),
        )
    )
    matched = matching_rules(
        _snapshot((rule,)),
        item=ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY),
        quantity=Decimal("2"),
        request=_request(),
    )
    assert len(matched) == 1


def test_criteria_group_or_semantics() -> None:
    rule = _rule(
        criteria=(
            RuleCriterion(criterion="ITEM", operator="EQ", value_text="nope", group_no=1),
            RuleCriterion(criterion="ITEM_CATEGORY", operator="EQ", value_text=str(CATEGORY), group_no=2),
        )
    )
    matched = matching_rules(
        _snapshot((rule,)),
        item=ItemInfo(item_id=ITEM, is_active=True, category_id=CATEGORY),
        quantity=Decimal("2"),
        request=_request(),
    )
    assert len(matched) == 1
