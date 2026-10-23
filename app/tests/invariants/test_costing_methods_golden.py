"""FS-05 golden corpus: method-deterministic unit cost/value for identical history (RULE-05-02)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.modules.costing.engine import (
    CostingEngine,
    CostingSettings,
    CostObject,
    ItemCostState,
    ValueMovement,
)

TXN_DATE = date(2026, 1, 2)


def _ctx(method: str) -> CostObject:
    return CostObject(item_id="item-1", costing_method=method, location_id="loc-1")


def _receipt(engine: CostingEngine, ctx: CostObject, state, layers, qty: str, cost: str, day: int):
    return engine.value_movement(
        ValueMovement(
            item_id="item-1", qty_delta=Decimal(qty), movement_type="IN",
            transaction_date=date(2026, 1, day), unit_cost=Decimal(cost),
        ),
        ctx, state, layers, CostingSettings(),
    )


def _issue(engine: CostingEngine, ctx: CostObject, state, layers, qty: str, day: int):
    return engine.value_movement(
        ValueMovement(item_id="item-1", qty_delta=Decimal(qty), movement_type="OUT", transaction_date=date(2026, 1, day)),
        ctx, state, layers, CostingSettings(),
    )


def _run_fifo_lifo(method: str) -> tuple[Decimal, Decimal, Decimal]:
    engine = CostingEngine()
    ctx = _ctx(method)
    r1 = _receipt(engine, ctx, ItemCostState(), [], "10", "5", 2)
    r2 = _receipt(engine, ctx, r1.new_state, list(r1.new_layers), "5", "7", 4)
    issue = _issue(engine, ctx, r2.new_state, list(r2.new_layers), "-12", 5)
    return issue.unit_cost, issue.value_delta, issue.new_state.value_base


def test_fifo_and_lifo_are_deterministic_and_order_sensitive() -> None:
    fifo_first = _run_fifo_lifo("FIFO")
    fifo_second = _run_fifo_lifo("FIFO")
    lifo = _run_fifo_lifo("LIFO")
    assert fifo_first == fifo_second  # RULE-05-02 determinism
    assert fifo_first[1] == Decimal("-64.000000000000")  # 10*5 + 2*7
    assert lifo[1] == Decimal("-70.000000000000")  # 5*7 + 7*5
    assert fifo_first != lifo


def test_average_golden_values() -> None:
    engine = CostingEngine()
    ctx = _ctx("AVERAGE")
    r1 = _receipt(engine, ctx, ItemCostState(), [], "10", "5", 2)
    r2 = _receipt(engine, ctx, r1.new_state, [], "10", "7", 3)
    assert r2.new_state.avg_cost == Decimal("6.000000000000")
    assert r2.new_state.value_base == Decimal("120.000000000000")
    issue = _issue(engine, ctx, r2.new_state, [], "-3", 4)
    assert issue.unit_cost == Decimal("6.000000000000")
    assert issue.value_delta == Decimal("-18.000000000000")
    assert issue.new_state.value_base == Decimal("102.000000000000")


def test_standard_cost_golden_values_and_ppv() -> None:
    engine = CostingEngine()
    ctx = _ctx("STANDARD")
    receipt = engine.value_movement(
        ValueMovement(
            item_id="item-1", qty_delta=Decimal("10"), movement_type="IN", transaction_date=TXN_DATE,
            unit_cost=Decimal("6.2"), standard_unit_cost=Decimal("6"), actual_unit_cost=Decimal("6.2"),
        ),
        ctx, ItemCostState(status="CURRENT", avg_cost=Decimal("6")), [], CostingSettings(),
    )
    assert receipt.value_delta == Decimal("60.000000000000")
    assert receipt.variance_amount == Decimal("2.000000000000")
    assert any(entry.movement_type == "VARIANCE" for entry in receipt.cost_entries)
    issue = _issue(engine, ctx, receipt.new_state, [], "-4", 3)
    assert issue.unit_cost == Decimal("6.000000000000")
    assert issue.value_delta == Decimal("-24.000000000000")


def test_specific_cost_uses_identity_layer() -> None:
    engine = CostingEngine()
    ctx = CostObject(item_id="item-1", costing_method="SPECIFIC", location_id="loc-1", serial_id="serial-1")
    settings = CostingSettings(numbered_inventory_available=True)
    receipt = engine.value_movement(
        ValueMovement(item_id="item-1", qty_delta=Decimal("1"), movement_type="IN", transaction_date=TXN_DATE, unit_cost=Decimal("11")),
        ctx, ItemCostState(), [], settings,
    )
    assert receipt.new_layers[0].qty_original == Decimal("1.000000000000")
    issue = engine.value_movement(
        ValueMovement(item_id="item-1", qty_delta=Decimal("-1"), movement_type="OUT", transaction_date=date(2026, 1, 3)),
        ctx, receipt.new_state, list(receipt.new_layers), settings,
    )
    assert issue.value_delta == Decimal("-11.000000000000")
    assert all(layer.status == "EXHAUSTED" for layer in issue.new_layers)


def test_group_average_recompute() -> None:
    from app.modules.costing.engine import GroupMemberBalance, recompute_group_average

    qty, value, avg = recompute_group_average(
        [
            GroupMemberBalance("l1", Decimal("10"), Decimal("50")),
            GroupMemberBalance("l2", Decimal("20"), Decimal("140")),
        ]
    )
    assert (qty, value, avg) == (Decimal("30.000000000000"), Decimal("190.000000000000"), Decimal("6.333333333333"))
