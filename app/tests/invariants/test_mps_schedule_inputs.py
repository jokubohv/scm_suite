"""INV: MPS/scheduling inputs (FS-09 §8) — deterministic backward scheduling from due dates, finite
capacity overload detection, and horizon input validation feeding FS-10 planning."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.tests.integration.manufacturing.conftest import seed_manufacturing_world

pytestmark = pytest.mark.integration


async def _scheduled_world(db_session, *, capacity: str = "8"):
    world = await seed_manufacturing_world(db_session, code_prefix="INVSCH")
    service = world["service"]
    work_center = await service.create_work_center(
        {
            "code": "WC-1",
            "name": "Work Centre 1",
            "location_id": str(world["location_id"]),
            "capacity_hours_per_day": capacity,
            "efficiency_percent": "100",
            "cost_per_hour": "25",
        }
    )
    routing = await service.create_routing(
        {
            "item_id": str(world["assembly_item_master_id"]),
            "routing_code": "RT-1",
            "name": "Routing",
            "effective_from": "2020-01-01",
        }
    )
    await service.add_routing_step(
        routing["id"],
        {
            "sequence": 10,
            "operation_code": "OP10",
            "work_center_id": work_center["id"],
            "description": "Assemble",
            "setup_time_minutes": "30",
            "run_time_per_unit_minutes": "6",
        },
    )
    await service.activate_routing(routing["id"])
    bom = await service.create_bom(
        {"assembly_item_id": str(world["assembly_item_id"]), "bom_code": "SCH-BOM", "name": "SCH BOM"}
    )
    await service.activate_bom(bom["id"])
    revision = await service.create_bom_revision(bom["id"], {"revision_code": "R1", "effective_from": "2020-01-01"})
    await service.add_bom_component(
        revision["id"],
        {
            "line_number": 1,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "1",
            "uom_id": str(world["uom_id"]),
        },
    )
    await service.activate_bom_revision(revision["id"])
    work_orders = []
    due = date.today() + timedelta(days=10)
    for _ in range(2):
        work_order = await service.create_work_order(
            {
                "assembly_item_id": str(world["assembly_item_id"]),
                "qty_ordered": "10",
                "routing_id": routing["id"],
                "start_date": str(due - timedelta(days=5)),
                "end_date": str(due),
            }
        )
        await service.firm_work_order(work_order["id"])
        await service.release_work_order(work_order["id"])
        work_orders.append(work_order)
    return world, due, work_orders


async def test_backward_schedule_is_deterministic(db_session) -> None:
    world, due, work_orders = await _scheduled_world(db_session)
    service = world["service"]
    first = await service.create_scheduling_run(
        {"method": "BACKWARD", "capacity_mode": "INFINITE", "horizon_start": str(date.today())}
    )
    second = await service.create_scheduling_run(
        {"method": "BACKWARD", "capacity_mode": "INFINITE", "horizon_start": str(date.today())}
    )
    first_ops = [
        (op["sequence"], op["planned_start"], op["planned_end"])
        for op in await service.list_scheduled_operations(first["id"])
    ]
    second_ops = [
        (op["sequence"], op["planned_start"], op["planned_end"])
        for op in await service.list_scheduled_operations(second["id"])
    ]
    assert first_ops == second_ops  # same inputs -> identical plan (deterministic inputs to MPS)
    assert first_ops
    for _sequence, planned_start_raw, planned_end_raw in first_ops:
        planned_start = datetime.fromisoformat(planned_start_raw)
        planned_end = datetime.fromisoformat(planned_end_raw)
        assert planned_start < planned_end
        assert planned_end.date() == due  # BACKWARD schedules end at the WO due date
        duration_hours = (planned_end - planned_start).total_seconds() / 3600
        expected = (30 + 6 * 10) / 60  # setup + run minutes for qty 10
        assert duration_hours == pytest.approx(expected, rel=1e-6)
    applied = await service.apply_scheduling_run(first["id"], {})
    assert applied["operations_applied"] == len(first_ops)


async def test_finite_capacity_marks_overload(db_session) -> None:
    world, due, work_orders = await _scheduled_world(db_session, capacity="1")
    service = world["service"]
    run = await service.create_scheduling_run(
        {
            "method": "BACKWARD",
            "capacity_mode": "FINITE",
            "horizon_start": str(due - timedelta(days=5)),
            "horizon_end": str(due + timedelta(days=5)),
        }
    )
    operations = await service.list_scheduled_operations(run["id"])
    assert len(operations) == 2
    violations = [op for op in operations if op["violation_reason"] == "INSUFFICIENT_CAPACITY"]
    assert violations, operations  # 2 WOs * 1.5h vs reviewed day window is capacity-checked per day/bucket
    assert run["method"] == "BACKWARD" and run["capacity_mode"] == "FINITE"
