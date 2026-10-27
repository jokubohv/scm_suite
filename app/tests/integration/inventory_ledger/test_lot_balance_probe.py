"""W0 probe: receipt lot detail must create lot-dimension balance and allow lot fulfillment."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _movement(world, qty, *, lot_id=None):
    return {
        "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]), "qty": qty,
        "movement_type": "RECEIPT", "source_line_ref": "1",
        "details": [{"qty": qty, "lot_id": str(lot_id) if lot_id else None,
                     "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
    }


async def test_receipt_lot_detail_creates_lot_balance_and_fulfillment(ledger_client, world, db_session):
    lot_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO traceability.lot (id, tenant_id, item_id, lot_number, is_active) "
            "VALUES (:id, :tenant, :item, :number, true)"
        ),
        {"id": lot_id, "tenant": world["tenant_id"], "item": world["item_id"],
         "number": f"PROBE-LOT-{uuid.uuid4().hex[:6]}"},
    )
    posted = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": "T-01", "transaction_date": "2026-10-23",
              "movements": [await _movement(world, "5", lot_id=lot_id)]},
        headers=world["headers"] | {"Idempotency-Key": f"lot-probe-{uuid.uuid4().hex}"},
    )
    assert posted.status_code == 201, posted.text
    # The lot dimension must be visible and the fulfillment must not see an empty aggregate position.
    row = (await db_session.execute(
        text("SELECT qty_on_hand FROM inventory_ledger.inventory_balance "
             "WHERE tenant_id = :t AND item_id = :i AND lot_id = :lot"),
        {"t": world["tenant_id"], "i": world["item_id"], "lot": lot_id},
    )).scalar_one()
    assert str(row) in {"5.000000000000", "5"}
    fulfilled = await ledger_client.post(
        "/api/v1/fulfillments",
        json={"transaction_date": "2026-10-23", "external_ref": "lot-probe",
              "lines": [{"item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
                         "inventory_status_id": str(world["status_id"]), "lot_id": str(lot_id),
                         "qty": "3", "ownership": "OWNED"}]},
        headers=world["headers"] | {"Idempotency-Key": f"lot-fulfil-{uuid.uuid4().hex}"},
    )
    assert fulfilled.status_code == 201, fulfilled.text
    remaining = (await db_session.execute(
        text("SELECT qty_on_hand FROM inventory_ledger.inventory_balance "
             "WHERE tenant_id = :t AND item_id = :i AND lot_id = :lot AND qty_on_hand > 0"),
        {"t": world["tenant_id"], "i": world["item_id"], "lot": lot_id},
    )).scalar_one()
    assert str(remaining) in {"2.000000000000", "2"}
