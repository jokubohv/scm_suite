"""FS-11 inventory execution integration tests: status-change tasks and replenishment."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _status_on_hand(db_session: Any, item_id: Any, status_id: Any) -> str:
    value = (
        await db_session.execute(
            text(
                "SELECT COALESCE(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance "
                "WHERE tenant_id = 'f0000000-0000-7000-8000-000000000001' AND item_id = :item "
                "AND inventory_status_id = :status"
            ),
            {"item": item_id, "status": status_id},
        )
    ).scalar_one()
    return format(Decimal(value), ".12f")


async def test_status_change_task_posts_t30(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import seed_stock

    await seed_stock(db_session, item_id=seeded["item_id"], bin_id=seeded["storage_bin"], qty="10")
    created = await client.post(
        "/api/v1/status-change-tasks",
        json={
            "item_id": seeded["item_id"],
            "location_id": seeded["location_id"],
            "bin_id": seeded["storage_bin"],
            "from_status_id": seeded["status_available"],
            "to_status_id": seeded["status_hold"],
            "qty": "4",
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    tasks = (await client.get("/api/v1/wms/tasks?task_type=STATUS_CHANGE")).json()
    assert any(row["id"] == task_id for row in tasks["items"])
    completed = await client.post(
        f"/api/v1/status-change-tasks/{task_id}/complete", json={}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["posted_transaction_id"]
    assert await _status_on_hand(db_session, seeded["item_id"], seeded["status_available"]) == "6.000000000000"
    assert await _status_on_hand(db_session, seeded["item_id"], seeded["status_hold"]) == "4.000000000000"


async def test_replenishment_plan_generates_and_completes_t29(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import bin_on_hand, seed_stock

    await seed_stock(db_session, item_id=seeded["item_id"], bin_id=seeded["storage_bin"], qty="10")
    rule = await client.post(
        "/api/v1/item-stock-locations",
        json={
            "item_id": seeded["item_id"],
            "location_id": seeded["location_id"],
            "bin_id": seeded["pick_bin"],
            "min_qty": "2",
            "max_qty": "8",
            "is_primary": True,
            "replenishment_source_bin_id": seeded["storage_bin"],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert rule.status_code == 201, rule.text
    plan = await client.post(
        "/api/v1/replenishment-plans",
        json={"location_id": seeded["location_id"], "trigger": "MIN_MAX"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert plan.status_code == 201, plan.text
    approved = await client.post(
        f"/api/v1/replenishment-plans/{plan.json()['id']}/approve", json={},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "GENERATED"
    task_ids = approved.json()["task_ids"]
    assert len(task_ids) == 1
    tasks = (await client.get("/api/v1/replenishment-tasks")).json()
    task = next(row for row in tasks["items"] if row["id"] == task_ids[0])
    assert task["from_bin_id"] == seeded["storage_bin"] and task["to_bin_id"] == seeded["pick_bin"]
    completed = await client.post(
        f"/api/v1/replenishment-tasks/{task_ids[0]}/complete", json={},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["posted_transaction_id"]
    assert await bin_on_hand(db_session, seeded["storage_bin"]) == "2.000000000000"
    assert await bin_on_hand(db_session, seeded["pick_bin"]) == "8.000000000000"
