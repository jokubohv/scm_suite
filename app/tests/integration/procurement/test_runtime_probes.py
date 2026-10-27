"""Runtime probes from W0 evidence: FIFO receipt posting and reversal."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.modules.item_master.service import ItemMasterService

pytestmark = pytest.mark.integration


async def _enable(session, tenant, flag):
    await session.execute(
        text(
            "INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source) "
            "VALUES (:id, :tenant, :flag, true, 'TEST') ON CONFLICT (tenant_id, flag_key) "
            "DO UPDATE SET is_enabled = true"
        ),
        {"id": uuid.uuid4(), "tenant": tenant, "flag": flag},
    )


async def _receipt(client, world, item_id, qty=5):
    payload = {
        "vendor_id": str(world["vendor_id"]), "receipt_type": "STANDALONE",
        "location_id": str(world["location_id"]),
        "lines": [{
            "item_id": item_id, "qty_received": qty, "unit_cost": "5",
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            "details": [{"qty_received": qty, "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
        }],
    }
    created = await client.post("/api/v1/item-receipts", json=payload, headers=world["headers"])
    assert created.status_code == 201, created.text
    return created.json()


async def test_average_receipt_post_and_reverse(client, world):
    receipt = await _receipt(client, world, str(world["item_id"]))
    posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    reversed_ = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "probe"}, headers=world["headers"]
    )
    assert reversed_.status_code == 200, reversed_.text
    assert reversed_.json()["status"] == "REVERSED"


async def test_fifo_receipt_post_and_reverse(client, world, db_session):
    await _enable(db_session, world["tenant_id"], "costing.engine.enabled")
    item = await ItemMasterService(db_session, world["tenant_id"]).create_item({
        "item_number": f"PROBE-FIFO-{uuid.uuid4().hex[:6]}",
        "display_name": "Probe FIFO",
        "item_type_code": "INVENTORY",
        "inventory": {"costing_method": "FIFO", "initial_cost": "5"},
    })
    receipt = await _receipt(client, world, item["id"])
    posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    assert posted.json()["posted_transaction_id"]
    layer = await db_session.execute(
        text("SELECT count(*) FROM costing.cost_layer WHERE tenant_id = :t AND item_cost_id IN "
             "(SELECT id FROM costing.item_cost WHERE tenant_id = :t AND item_id = :i)"),
        {"t": world["tenant_id"], "i": uuid.UUID(item["id"])},
    )
    assert int(layer.scalar_one()) == 1
    reversed_ = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "probe"}, headers=world["headers"]
    )
    assert reversed_.status_code == 200, reversed_.text
