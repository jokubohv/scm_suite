"""FS-11 setup/configuration + warehouse inventory read/write API integration tests."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


async def test_warehouse_is_listed_and_seeded_with_rules(client: Any, seeded: dict[str, Any]) -> None:
    response = await client.get("/api/v1/warehouses")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert any(row["id"] == seeded["warehouse_id"] for row in items)
    detail = await client.get(f"/api/v1/warehouses/{seeded['warehouse_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["location_id"] == seeded["location_id"]
    assert len(detail.json()["rules"]) == 8


async def test_warehouse_create_requires_bin_enabled_location(client: Any, db_session: Any) -> None:
    from sqlalchemy import insert

    from app.modules.traceability import tables as tr

    plain_location = uuid4()
    await db_session.execute(
        insert(tr.location).values(
            id=plain_location, tenant_id=__import__("uuid").UUID("f0000000-0000-7000-8000-000000000001"),
            code="PLAIN", name="Plain", location_type="WAREHOUSE", is_bins_enabled=False,
            is_statuses_enabled=False, is_consignment_allowed=False, allow_commit_from_non_available=False,
            timezone="UTC", is_unallocated=False, is_inactive=False, version=1,
        )
    )
    response = await client.post(
        "/api/v1/warehouses", json={"location_id": str(plain_location), "code": "BAD-WH"}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_setup_entities_crud(client: Any, seeded: dict[str, Any]) -> None:
    family = await client.post(
        "/api/v1/item-process-families", json={"code": "FAM1", "name": "Family 1"}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert family.status_code == 201, family.text
    family_id = family.json()["id"]
    group = await client.post(
        "/api/v1/item-process-groups",
        json={"item_process_family_id": family_id, "code": "GRP1", "name": "Group 1"},
    )
    assert group.status_code == 201, group.text
    assignment = await client.post(
        "/api/v1/item-process-assignments",
        json={"item_id": seeded["item_id"], "item_process_group_id": group.json()["id"]},
    )
    assert assignment.status_code == 201, assignment.text
    stock = await client.post(
        "/api/v1/item-stock-locations",
        json={
            "item_id": seeded["item_id"], "location_id": seeded["location_id"], "bin_id": seeded["pick_bin"],
            "min_qty": "2", "max_qty": "20", "is_primary": True,
            "replenishment_source_bin_id": seeded["storage_bin"],
        },
    )
    assert stock.status_code == 201, stock.text
    patched = await client.patch(
        "/api/v1/item-stock-locations", json={"id": stock.json()["id"], "max_qty": "30"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["max_qty"].startswith("30")
    for path, body in (
        ("/api/v1/order-types", {"code": "OUT", "name": "Outbound", "direction": "OUTBOUND"}),
        ("/api/v1/ucc-code-formats", {"code": "UCC12", "name": "UCC-12", "format_pattern": "(00)"}),
        ("/api/v1/carrier-service-levels", {"carrier_code": "UPS", "service_code": "GND", "name": "UPS Ground"}),
    ):
        response = await client.post(path, json=body)
        assert response.status_code == 201, (path, response.text)
    assert (await client.get("/api/v1/item-process-families")).status_code == 200
    assert (await client.get("/api/v1/item-process-groups")).json()["items"]
    assert (await client.get("/api/v1/order-types")).json()["items"]


async def test_pick_and_putaway_strategies_with_rules(client: Any, seeded: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/pick-strategies",
        json={
            "code": "FIFO2", "name": "FIFO 2", "priority": 2,
            "rules": [{"sequence": 1, "zone_id": seeded["zone_id"], "sort_direction": "FIFO"}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["rules"]) == 1 and body["rules"][0]["sort_direction"] == "FIFO"
    updated = await client.patch(
        "/api/v1/pick-strategies",
        json={"id": body["id"], "priority": 3, "rules": [{"sequence": 1, "sort_direction": "LIFO"}]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["priority"] == 3
    putaway = await client.post(
        "/api/v1/putaway-strategies",
        json={
            "code": "P2", "name": "Putaway 2", "priority": 2,
            "staging_bin_id": seeded["staging_bin"],
            "rules": [{"sequence": 1, "zone_id": seeded["zone_id"], "bin_type": "STORAGE", "max_qty_per_bin": "50"}],
        },
    )
    assert putaway.status_code == 201, putaway.text
    assert putaway.json()["rules"][0]["bin_type"] == "STORAGE"


async def test_system_rules_override_and_bin_blocks(client: Any, seeded: dict[str, Any]) -> None:
    rules = await client.get("/api/v1/system-rules")
    assert rules.status_code == 200
    assert any(row["code"] == "STAGING_AT_RECEIVING" for row in rules.json()["items"])
    override = await client.put(
        f"/api/v1/warehouses/{seeded['warehouse_id']}/system-rules/STAGING_AT_RECEIVING",
        json={"is_enabled": True},
    )
    assert override.status_code == 200, override.text
    assert override.json()["is_enabled"] is True
    scoped = await client.get(f"/api/v1/system-rules?warehouse_id={seeded['warehouse_id']}")
    row = next(entry for entry in scoped.json()["items"] if entry["code"] == "STAGING_AT_RECEIVING")
    assert row["effective_enabled"] is True
    block = await client.post(
        "/api/v1/bin-blocks",
        json={"bin_id": seeded["storage_bin"], "reason": "hold for audit"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert block.status_code == 201, block.text
    assert (await client.get("/api/v1/bin-blocks")).json()["items"]
    released = await client.post(f"/api/v1/bin-blocks/{block.json()['id']}/release", headers={"Idempotency-Key": uuid4().hex})
    assert released.status_code == 200 and released.json()["released_at"] is not None


async def test_item_bin_assignment_and_warehouse_inventory(client: Any, seeded: dict[str, Any]) -> None:
    assigned = await client.put(
        f"/api/v1/warehouses/{seeded['warehouse_id']}/item-bin-assignments",
        json={"bin_id": seeded["pick_bin"], "item_ids": [seeded["item_id"]]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned"] == 1
    created = await client.post(
        f"/api/v1/warehouses/{seeded['warehouse_id']}/inventory",
        json={"item_id": seeded["item_id"], "bin_id": seeded["storage_bin"], "qty": "5", "unit_cost": "2"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert created.status_code == 201, created.text
    assert created.json()["inventory_transaction_id"]
    inventory = await client.get(f"/api/v1/warehouses/{seeded['warehouse_id']}/inventory")
    assert inventory.status_code == 200, inventory.text
    assert any(row["bin_id"] == seeded["storage_bin"] for row in inventory.json()["items"])


async def test_cycle_count_request_delegates_to_counts_engine(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    from app.tests.integration.wms.conftest import seed_stock

    await seed_stock(db_session, item_id=seeded["item_id"], bin_id=seeded["storage_bin"], qty="5")
    response = await client.post(
        f"/api/v1/warehouses/{seeded['warehouse_id']}/cycle-count-requests",
        json={"plan_date": "2026-10-23"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    assert response.json()["count_id"]


async def test_carts_and_device_sessions(client: Any, seeded: dict[str, Any]) -> None:
    cart = await client.post(
        "/api/v1/wms/carts", json={"location_id": seeded["location_id"]}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert cart.status_code == 201, cart.text
    patched = await client.patch(f"/api/v1/wms/carts/{cart.json()['id']}", json={"status": "IN_USE"})
    assert patched.status_code == 200, patched.text
    session = await client.post(
        "/api/v1/wms/device-sessions",
        json={"warehouse_id": seeded["warehouse_id"], "user_id": seeded["app_user_id"], "client_info": {"app": "scanner"}},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert session.status_code == 201, session.text
    assert session.json()["session_token"]
    heartbeat = await client.patch(f"/api/v1/wms/device-sessions/{session.json()['id']}/heartbeat")
    assert heartbeat.status_code == 200, heartbeat.text
    ended = await client.post(f"/api/v1/wms/device-sessions/{session.json()['id']}/end", headers={"Idempotency-Key": uuid4().hex})
    assert ended.status_code == 200 and ended.json()["ended_at"]
