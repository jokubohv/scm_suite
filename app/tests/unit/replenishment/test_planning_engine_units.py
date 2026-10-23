"""FS-10 unit tests: versioned planning formulas, netting, lot sizing, allocation, DRP, fences."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.modules.replenishment.engine import (
    AllocationDemand,
    AllocationSupply,
    PlannedDemand,
    PlanningError,
    PlanningItemState,
    SupplyRule,
    TimeFence,
    allocate_fefo,
    allocate_supply,
    apply_lot_sizing,
    assert_transition,
    compute_aim,
    demand_change_decision,
    demand_forecast,
    evaluate_demand_change,
    explode_bom,
    lead_time_offset,
    linear_regression,
    moving_average,
    planning_run_key,
    resolve_time_fence,
    run_mrp,
    seasonal_average,
    validate_bod_arcs,
    validate_network_members,
)


def test_demand_methods_fixtures() -> None:
    assert moving_average([Decimal("10"), Decimal("20"), Decimal("30")], 3) == Decimal("20.000000000000")
    assert linear_regression([Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40"), Decimal("50")]) == Decimal("60.000000000000")
    assert seasonal_average([(1, Decimal("100")), (2, Decimal("140"))], 3, 1) == Decimal("100.000000000000")
    assert seasonal_average([(1, Decimal("100")), (1, Decimal("140"))], 3, 1) == Decimal("120.000000000000")
    rows = demand_forecast(method="OPEN_DEMAND", open_demand=[(date(2026, 10, 23), Decimal("40")), (date(2026, 10, 26), Decimal("60"))])
    assert [row["qty_forecast"] for row in rows] == [Decimal("40.000000000000"), Decimal("60.000000000000")]


def test_aim_formula_and_seasonal_clamp() -> None:
    result = compute_aim(
        demand_by_day=[(date(2026, 9, 1) + timedelta(days=index), Decimal("10")) for index in range(30)],
        lead_time_days=10, safety_stock=Decimal("20"), quantity_available=Decimal("100"),
        seasonal_factor_value=Decimal("10"),
    )
    assert result["seasonal_factor"] == Decimal("10.000000000000")
    assert result["reorder_point_calculated"] == Decimal("1020.000000000000")


def test_supply_mrp_fixture_and_lot_sizing() -> None:
    rule = SupplyRule(lot_sizing="LOT_FOR_LOT")
    inputs = [PlanningItemState("A", "L", on_hand=Decimal("40"), safety_stock=Decimal("10"), lead_time_days=4)]
    result = run_mrp(
        items=inputs,
        demands=[PlannedDemand("A", "L", date(2026, 10, 3), Decimal("30")), PlannedDemand("A", "L", date(2026, 10, 10), Decimal("25"))],
        supplies=[],
        rule=rule,
        horizon_start=date(2026, 10, 1), horizon_end=date(2026, 10, 31), as_of=date(2026, 10, 1),
    )
    assert result["lines"]
    fixed = apply_lot_sizing(Decimal("5"), SupplyRule(lot_sizing="FIXED_QTY", fixed_order_qty=Decimal("10")))
    assert fixed == Decimal("10.000000000000")
    assert apply_lot_sizing(Decimal("15"), SupplyRule(lot_sizing="FIXED_QTY", fixed_order_qty=Decimal("10"))) == Decimal("20.000000000000")
    assert lead_time_offset(date(2026, 10, 10), 4) == date(2026, 10, 6)


def test_bom_explosion_with_phantom() -> None:
    requirements = explode_bom(
        parent_item_id="A", qty=Decimal("5"),
        bom_lines=[
            {"component_item_id": "P", "qty_per": Decimal("2"), "is_phantom": True, "parent_item_id": "A"},
            {"component_item_id": "C", "qty_per": Decimal("3"), "parent_item_id": "P"},
        ],
    )
    assert any(row["item_id"] == "C" and row["qty"] == Decimal("30.000000000000") for row in requirements)


def test_allocation_fixtures() -> None:
    supply = {("A", "L"): Decimal("100")}
    demands = [
        AllocationDemand("D1", "1", "A", "L", Decimal("60"), date(2026, 10, 23), priority=1),
        AllocationDemand("D2", "1", "A", "L", Decimal("60"), date(2026, 10, 23), priority=2),
    ]
    results, exceptions = allocate_supply(method="PRIORITY", demands=demands, supply_by_key=supply)
    assert sum((row["qty_allocated"] for row in results), Decimal("0")) == Decimal("100.000000000000")
    assert exceptions[0]["shortfall_qty"] == Decimal("20.000000000000")
    fair_results, _fair_exceptions = allocate_supply(method="FAIR_SHARE", demands=[
        AllocationDemand("D1", "1", "A", "L", Decimal("80"), date(2026, 10, 23)),
        AllocationDemand("D2", "1", "A", "L", Decimal("80"), date(2026, 10, 23)),
    ], supply_by_key=supply)
    assert [row["qty_allocated"] for row in fair_results] == [Decimal("50.000000000000"), Decimal("50.000000000000")]


def test_fefo_lot_order() -> None:
    results, exceptions = allocate_fefo(
        demands=[AllocationDemand("D1", "1", "A", "L", Decimal("70"), date(2026, 10, 23))],
        lots=[
            AllocationSupply("L2", "A", "L", Decimal("60"), lot_id="L2", expiry_date=date(2026, 12, 1), receipt_date=date(2026, 10, 1)),
            AllocationSupply("L1", "A", "L", Decimal("40"), lot_id="L1", expiry_date=date(2026, 11, 1), receipt_date=date(2026, 10, 1)),
        ],
    )
    assert results[0]["lot_id"] == "L1" and results[0]["qty_allocated"] == Decimal("40.000000000000")
    assert results[1]["lot_id"] == "L2" and results[1]["qty_allocated"] == Decimal("30.000000000000")
    assert exceptions == []


def test_drp_validation_and_fences() -> None:
    validate_network_members([{"id": "1", "location_id": "L1", "parent_member_id": None}])
    with pytest.raises(PlanningError):
        validate_network_members([{"id": "1", "location_id": "L1", "parent_member_id": "2"}, {"id": "2", "location_id": "L2", "parent_member_id": "1"}])
    validate_bod_arcs([{"source_location_id": "S", "destination_location_id": "D"}])
    with pytest.raises(PlanningError):
        validate_bod_arcs([{"source_location_id": "S", "destination_location_id": "S"}])
    fence = TimeFence("GLOBAL", None, 10, "FROZEN")
    assert resolve_time_fence([fence], item_id="I") is fence
    with pytest.raises(PlanningError):
        evaluate_demand_change(previous=Decimal("10"), current=Decimal("20"), change_date=date(2026, 10, 25), as_of=date(2026, 10, 20), threshold_percent=Decimal("20"), fence=fence)
    decision = evaluate_demand_change(previous=Decimal("10"), current=Decimal("20"), change_date=date(2026, 11, 25), as_of=date(2026, 10, 20), threshold_percent=Decimal("20"), fence=fence)
    assert decision["allowed"] is True


def test_state_machine_and_run_key_determinism() -> None:
    assert_transition("supply_plan", "RUNNING", "CALCULATED")
    with pytest.raises(PlanningError):
        assert_transition("supply_plan", "APPROVED", "RUNNING")
    first = planning_run_key(kind="supply", scope={"items": ["A"]}, horizon=["2026-10-01", "2026-12-31"], policy_versions={"supply": "supply-mrp-v1/1"}, inputs={"x": 1})
    second = planning_run_key(kind="supply", scope={"items": ["A"]}, horizon=["2026-10-01", "2026-12-31"], policy_versions={"supply": "supply-mrp-v1/1"}, inputs={"x": 1})
    assert first == second
    assert demand_change_decision(previous=Decimal("10"), current=Decimal("10"), threshold_percent=Decimal("20"))[0] is False
