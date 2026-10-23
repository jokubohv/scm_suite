"""FS-05 D5 invariant: pending recalculation blocks valuation unless provisional mode is opt-in."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.contracts.costing import CostingPort
from app.modules.costing.engine import (
    CostingEngine,
    CostingSettings,
    CostObject,
    ItemCostState,
    RecalculationEngine,
    ValueMovement,
)
from app.modules.costing.errors import CostingRecalcRequired


def _ctx() -> CostObject:
    return CostObject(item_id="item-1", costing_method="AVERAGE", location_id="loc-1")


def _movement() -> ValueMovement:
    return ValueMovement(item_id="item-1", qty_delta=Decimal("1"), movement_type="IN", transaction_date=date(2026, 1, 2), unit_cost=Decimal("5"))


def test_pending_recalculation_blocks_postings_by_default() -> None:
    port = CostingPort()
    state = ItemCostState(status="PENDING_RECALC")
    with pytest.raises(CostingRecalcRequired) as exc:
        port.assert_postable(_ctx(), state, CostingSettings(block_when_recalc_pending=True))
    assert exc.value.code == "COSTING_RECALC_REQUIRED"
    assert exc.value.status_code == 409


def test_provisional_mode_allows_pending_recalculation() -> None:
    port = CostingPort()
    state = ItemCostState(status="PENDING_RECALC")
    port.assert_postable(_ctx(), state, CostingSettings(block_when_recalc_pending=False, allow_provisional=True))
    assert RecalculationEngine().block_policy(CostingSettings(block_when_recalc_pending=False, allow_provisional=True), state) == "PROVISIONAL"


def test_recalc_policy_returns_block_proceed() -> None:
    recalc = RecalculationEngine()
    current = ItemCostState(status="CURRENT")
    assert recalc.block_policy(CostingSettings(), current) == "PROCEED"
    assert recalc.block_policy(CostingSettings(block_when_recalc_pending=False), ItemCostState(status="PENDING_RECALC")) in {"BLOCK", "PROVISIONAL"}


def test_closed_period_blocks_cost_valuation() -> None:
    port = CostingPort()
    with pytest.raises(Exception) as exc:
        port.assert_postable(_ctx(), ItemCostState(), CostingSettings(period_locked=True))
    assert getattr(exc.value, "code", None) == "PERIOD_CLOSED"


def test_specific_cost_without_numbered_inventory_is_dependency_blocked() -> None:
    from app.modules.costing.errors import CostingDependencyNotDeployed

    ctx = CostObject(item_id="item-1", costing_method="LOT", location_id="loc-1", lot_id="lot-1")
    with pytest.raises(CostingDependencyNotDeployed):
        CostingEngine().assert_postable(ctx, ItemCostState(), CostingSettings(numbered_inventory_available=False))


def test_recalculation_plan_is_deterministic() -> None:
    recalc = RecalculationEngine()
    from app.modules.costing.engine import CostEntrySnapshot

    recorded = [CostEntrySnapshot("item-1", date(2026, 1, 2), "OUT", Decimal("-2"), Decimal("5"), Decimal("-10"))]
    expected = [CostEntrySnapshot("item-1", date(2026, 1, 2), "OUT", Decimal("-2"), Decimal("5"), Decimal("-9"))]
    first = recalc.plan(recorded_entries=recorded, expected_entries=expected, item_id="item-1", cost_run_id="run-1")
    second = recalc.plan(recorded_entries=recorded, expected_entries=expected, item_id="item-1", cost_run_id="run-1")
    assert [(a.item_id, a.value_delta) for a in first] == [(a.item_id, a.value_delta) for a in second]
    assert first[0].value_delta == Decimal("1.000000000000")
