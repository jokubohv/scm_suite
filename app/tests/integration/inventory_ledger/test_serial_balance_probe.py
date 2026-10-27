"""W0 follow-up: serialized transfers/fulfillments must preserve serial dimensions."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.tests.integration.inventory_ledger.conftest import _insert_location

pytestmark = pytest.mark.integration


async def _serials(db_session, world, count=2):
    ids = []
    for _ in range(count):
        serial_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO traceability.serial_number (id, tenant_id, item_id, serial_number, state, ownership) "
                "VALUES (:id, :tenant, :item, :number, 'AVAILABLE', 'OWNED')"
            ),
            {"id": serial_id, "tenant": world["tenant_id"], "item": world["item_id"],
             "number": f"SN-{uuid.uuid4().hex[:10]}"},
        )
        ids.append(str(serial_id))
    return ids


async def _receipt_serial_stock(ledger_client, world, serial_ids):
    response = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={
            "posting_type": "T-01", "transaction_date": "2026-10-23",
            "movements": [{
                "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
                "inventory_status_id": str(world["status_id"]), "qty": str(len(serial_ids)),
                "movement_type": "RECEIPT", "source_line_ref": "1",
                "details": [{"serial_id": serial_id, "qty": "1",
                             "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}
                            for serial_id in serial_ids],
            }],
        },
        headers=world["headers"] | {"Idempotency-Key": f"serial-recv-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 201, response.text
    return response


async def test_serialized_transfer_and_fulfillment_preserve_serial_balance(ledger_client, world, db_session):
    serial_ids = await _serials(db_session, world, count=2)
    await _receipt_serial_stock(ledger_client, world, serial_ids)
    destination = await _insert_location(db_session, world["tenant_id"], code=f"SN-DEST-{uuid.uuid4().hex[:6]}")

    transfer = await ledger_client.post(
        "/api/v1/inventory-transfers",
        json={
            "transaction_date": "2026-10-23", "from_location_id": str(world["location_id"]),
            "to_location_id": str(destination),
            "lines": [{"item_id": str(world["item_id"]), "qty": "2",
                       "inventory_status_id": str(world["status_id"]), "serial_ids": serial_ids}],
        },
        headers=world["headers"] | {"Idempotency-Key": f"serial-transfer-{uuid.uuid4().hex}"},
    )
    assert transfer.status_code == 201, transfer.text
    posted = await ledger_client.post(
        f"/api/v1/inventory-transfers/{transfer.json()['id']}/post", json={},
        headers=world["headers"] | {"Idempotency-Key": f"serial-transfer-post-{uuid.uuid4().hex}"},
    )
    assert posted.status_code == 200, posted.text

    rows = (await db_session.execute(
        text("SELECT serial_id, location_id, qty_on_hand FROM inventory_ledger.inventory_balance "
             "WHERE tenant_id = :t AND item_id = :i AND serial_id IS NOT NULL"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )).mappings().all()
    by_location = {}
    for row in rows:
        by_location[str(row["location_id"])] = by_location.get(str(row["location_id"]), 0) + row["qty_on_hand"]
    assert str(destination) in by_location
    assert str(by_location[str(destination)]) in {"2", "2.000000000000"}

    fulfilled = await ledger_client.post(
        "/api/v1/fulfillments",
        json={"transaction_date": "2026-10-23", "lines": [{
            "item_id": str(world["item_id"]), "location_id": str(destination),
            "inventory_status_id": str(world["status_id"]), "serial_ids": serial_ids, "qty": "2",
        }]},
        headers=world["headers"] | {"Idempotency-Key": f"serial-fulfil-{uuid.uuid4().hex}"},
    )
    assert fulfilled.status_code == 201, fulfilled.text
    remaining = (await db_session.execute(
        text("SELECT coalesce(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance "
             "WHERE tenant_id = :t AND item_id = :i AND serial_id = ANY(:serials)"),
        {"t": world["tenant_id"], "i": world["item_id"], "serials": [uuid.UUID(s) for s in serial_ids]},
    )).scalar_one()
    from decimal import Decimal
    assert Decimal(str(remaining)) == Decimal("0")


async def test_serialized_adjustment_expands_serial_ids(ledger_client, world, db_session):
    serial_ids = await _serials(db_session, world, count=2)
    await _receipt_serial_stock(ledger_client, world, serial_ids)
    created = await ledger_client.post(
        "/api/v1/inventory-adjustments",
        json={"transaction_date": "2026-10-23", "lines": [{
            "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
            "inventory_status_id": str(world["status_id"]), "qty_delta": "-2", "serial_ids": serial_ids,
        }]},
        headers=world["headers"] | {"Idempotency-Key": f"serial-adj-{uuid.uuid4().hex}"},
    )
    assert created.status_code == 201, created.text
    assert len(created.json()["lines"]) == 2
    submitted = await ledger_client.post(
        f"/api/v1/inventory-adjustments/{created.json()['id']}/submit",
        headers=world["headers"] | {"Idempotency-Key": f"serial-adj-submit-{uuid.uuid4().hex}"},
    )
    assert submitted.status_code == 200, submitted.text
    posted = await ledger_client.post(
        f"/api/v1/inventory-adjustments/{created.json()['id']}/post", json={},
        headers=world["headers"] | {"Idempotency-Key": f"serial-adj-post-{uuid.uuid4().hex}"},
    )
    assert posted.status_code == 200, posted.text
    remaining = (await db_session.execute(
        text("SELECT coalesce(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance "
             "WHERE tenant_id = :t AND item_id = :i AND serial_id = ANY(:serials)"),
        {"t": world["tenant_id"], "i": world["item_id"], "serials": [uuid.UUID(s) for s in serial_ids]},
    )).scalar_one()
    from decimal import Decimal
    assert Decimal(str(remaining)) == Decimal("0")
