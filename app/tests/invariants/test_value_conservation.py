"""FS-05 invariant INV-02/INV-10/INV-11/INV-12/INV-15: deterministic value conservation.

Pure tests over the costing engine (no database): every value movement is balanced by cost entries,
layer consumption/remaining reconstructs exactly, landed-cost allocations sum to the line amount and
rounding residuals are explicit (never float).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.contracts.costing import CostingPort
from app.modules.costing.engine import (
    CostEntrySnapshot,
    CostingEngine,
    CostingSettings,
    CostObject,
    ItemCostState,
    LayerState,
    ReceiptLineBasis,
    ValueMovement,
    allocate_landed_cost,
    allocate_largest_remainder,
    consume_layers,
    create_layer,
    replay_cost_ledger,
    verify_layer_conservation,
    verify_value_conservation,
)


def _equity_entries(result) -> Decimal:
    return sum((entry.value_delta for entry in result.cost_entries), Decimal("0"))


def _ctx(method: str = "AVERAGE") -> CostObject:
    return CostObject(item_id="item-1", costing_method=method, location_id="loc-1")


def test_average_round_trip_conserves_value() -> None:
    engine = CostingEngine()
    settings = CostingSettings()
    receipt = engine.value_movement(
        ValueMovement(item_id="item-1", qty_delta=Decimal("10"), movement_type="IN", transaction_date=date(2026, 1, 2), unit_cost=Decimal("5")),
        _ctx(), ItemCostState(), [], settings,
    )
    assert receipt.value_delta == Decimal("50.000000000000")
    assert _equity_entries(receipt) == receipt.value_delta
    issue = engine.value_movement(
        ValueMovement(item_id="item-1", qty_delta=Decimal("-4"), movement_type="OUT", transaction_date=date(2026, 1, 3)),
        _ctx(), receipt.new_state, [], settings,
    )
    assert issue.value_delta == Decimal("-20.000000000000")
    assert issue.new_state.qty_on_hand == Decimal("6.000000000000")
    assert issue.new_state.value_base == Decimal("30.000000000000")
    verify_value_conservation(issue.cost_entries, issue.value_delta)
    # replay reconstructs the position
    entries = [
        CostEntrySnapshot("item-1", date(2026, 1, 2), "IN", Decimal("10"), Decimal("5"), Decimal("50")),
        CostEntrySnapshot("item-1", date(2026, 1, 3), "OUT", Decimal("-4"), Decimal("5"), Decimal("-20")),
    ]
    qty, value = replay_cost_ledger(entries)
    assert (qty, value) == (Decimal("6.000000000000"), Decimal("30.000000000000"))


def test_fifo_layer_conservation_and_no_negative_layers() -> None:
    layers: list[LayerState] = []
    first, layers = create_layer(layers, qty=10, unit_cost=5, layer_date=date(2026, 1, 2))
    second, layers = create_layer(layers, qty=5, unit_cost=7, layer_date=date(2026, 1, 4))
    remaining, actions, consumed, estimated, _ = consume_layers(
        layers, 12, method="FIFO", allow_negative=True, last_purchase_price=Decimal("7"),
        estimated_allowed=True, layer_date=date(2026, 1, 5),
    )
    assert estimated is False
    assert consumed == Decimal("64.000000000000")  # 10*5 + 2*7
    assert sum((layer.qty_remaining for layer in remaining), Decimal("0")) == Decimal("3.000000000000")
    assert sum((layer.value_remaining for layer in remaining), Decimal("0")) == Decimal("21.000000000000")
    assert first.layer_id in {action.layer_id for action in actions}
    verify_layer_conservation(remaining)
    for layer in remaining:
        assert layer.qty_remaining >= 0 and layer.value_remaining >= 0


def test_landed_cost_allocation_is_exact_invariant_12() -> None:
    lines = [
        ReceiptLineBasis("r1", "item-1", qty=Decimal("1"), weight=Decimal("1"), amount=Decimal("10")),
        ReceiptLineBasis("r2", "item-2", qty=Decimal("1"), weight=Decimal("2"), amount=Decimal("10")),
        ReceiptLineBasis("r3", "item-3", qty=Decimal("1"), weight=Decimal("3"), amount=Decimal("10")),
    ]
    for basis in ("QUANTITY", "WEIGHT", "AMOUNT", "PER_LINE"):
        allocation = allocate_landed_cost(Decimal("100"), basis, lines)
        assert sum(allocation.values()) == Decimal("100.000000000000")
    # explicit rounding residual: 10 / 3 -> 3.333... with residual assigned exactly
    parts = allocate_largest_remainder(Decimal("10"), [Decimal("1"), Decimal("1"), Decimal("1")])
    assert sum(parts) == Decimal("10.000000000000")
    assert all(isinstance(part, Decimal) for part in parts)


def test_cost_port_is_in_process_and_balanced() -> None:
    port = CostingPort()
    result = port.value_inbound(
        ValueMovement(item_id="item-1", qty_delta=Decimal("3"), movement_type="IN", transaction_date=date(2026, 2, 1), unit_cost=Decimal("2.5")),
        _ctx(), None, [], CostingSettings(),
    )
    port.assert_complete(result, ledger_line_count=1)
    assert result.new_state is not None
    assert result.new_state.value_base == Decimal("7.500000000000")
