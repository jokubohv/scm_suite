"""FS-08 ↔ FS-05 landed-cost bridge: receipt charges/T-27 and vendor-bill landed lines/T-26 (value-only)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _po_payload(world: dict[str, Any], qty: int = 4, price: str = "5") -> dict[str, Any]:
    return {
        "vendor_id": str(world["vendor_id"]), "po_type": "STANDARD", "transaction_date": "2026-10-23",
        "lines": [{"item_id": str(world["item_id"]), "qty_ordered": qty, "unit_price": price, "expected_receipt_date": "2026-11-01"}],
    }


async def _approved_po(client, world: dict[str, Any], qty: int = 4) -> dict[str, Any]:
    created = await client.post("/api/v1/purchase-orders", json=_po_payload(world, qty), headers=world["headers"])
    assert created.status_code == 201, created.text
    po_id = created.json()["id"]
    submitted = await client.post(f"/api/v1/purchase-orders/{po_id}/submit", headers=world["headers"])
    assert submitted.status_code == 200, submitted.text
    if submitted.json()["status"] != "APPROVED":
        approved = await client.post(f"/api/v1/purchase-orders/{po_id}/approve", json={}, headers=world["headers"])
        assert approved.status_code == 200, approved.text
    detail = await client.get(f"/api/v1/purchase-orders/{po_id}", headers=world["headers"])
    return detail.json()


def _receipt_payload(world: dict[str, Any], po: dict[str, Any], qty: int, *, charges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "purchase_order_id": po["id"], "vendor_id": str(world["vendor_id"]), "receipt_type": "PO",
        "received_at": "2026-10-23T10:00:00Z", "location_id": str(world["location_id"]),
        "lines": [{
            "purchase_order_line_id": po["lines"][0]["id"], "item_id": str(world["item_id"]),
            "qty_received": qty, "unit_cost": po["lines"][0]["unit_price"],
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            "details": [{"qty_received": qty, "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
        }],
    }
    if charges is not None:
        payload["landed_cost_charges"] = charges
    return payload


async def _landed_cost_rows(db_session: Any, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await db_session.execute(
        text("SELECT * FROM costing.landed_cost WHERE tenant_id=:t ORDER BY created_at"), {"t": tenant_id}
    )
    return [dict(row._mapping) for row in rows]


async def _value_only_ledger(db_session: Any, tenant_id: uuid.UUID, source_doc_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await db_session.execute(
        text(
            "SELECT l.qty_base, l.value_base FROM inventory_ledger.inventory_transaction_line l "
            "JOIN inventory_ledger.inventory_transaction t ON t.tenant_id=l.tenant_id AND t.id=l.inventory_transaction_id "
            "WHERE l.tenant_id=:t AND t.source_doc_id=:doc AND l.qty_base=0"
        ),
        {"t": tenant_id, "doc": source_doc_id},
    )
    return [dict(row._mapping) for row in rows]


async def test_receipt_landed_cost_charges_post_t27_value_only(client, world, db_session) -> None:
    po = await _approved_po(client, world, qty=4)
    created = await client.post(
        "/api/v1/item-receipts",
        json=_receipt_payload(world, po, 4, charges=[{"description": "Freight", "amount": "20"}, {"description": "Duty", "amount": "5"}]),
        headers=world["headers"],
    )
    assert created.status_code == 201, created.text
    receipt = created.json()
    assert receipt["landed_cost"]["status"] == "DRAFT"
    receipt_id = receipt["id"]

    posted = await client.post(f"/api/v1/item-receipts/{receipt_id}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    landed = posted.json()["landed_cost"]
    assert landed["posting_type"] == "T-27" and landed["value_only"] is True
    assert Decimal(landed["allocated_total"]) == Decimal("25"), landed

    rows = await _landed_cost_rows(db_session, world["tenant_id"])
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "POSTED" and rows[0]["estimated"] is True
    assert Decimal(str(rows[0]["total_amount"])) == Decimal("25")
    allocations = await db_session.execute(
        text("SELECT coalesce(sum(allocated_amount),0) FROM costing.landed_cost_allocation WHERE tenant_id=:t AND landed_cost_id=:id"),
        {"t": world["tenant_id"], "id": rows[0]["id"]},
    )
    assert Decimal(str(allocations.scalar_one())) == Decimal("25")
    ledger = await _value_only_ledger(db_session, world["tenant_id"], uuid.UUID(receipt_id))
    # One T-27 value line per charge allocation (Freight 20 + Duty 5), all qty 0.
    assert len(ledger) == 2
    assert sum((Decimal(str(row["qty_base"])) for row in ledger), Decimal("0")) == 0
    assert sum((Decimal(str(row["value_base"])) for row in ledger), Decimal("0")) == Decimal("25")
    on_hand = await db_session.execute(
        text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert Decimal(str(on_hand.scalar_one())) == Decimal("4")


async def test_bill_landed_cost_line_posts_t26_value_only(client, world, db_session) -> None:
    po = await _approved_po(client, world, qty=4)
    receipt_resp = await client.post("/api/v1/item-receipts", json=_receipt_payload(world, po, 4), headers=world["headers"])
    assert receipt_resp.status_code == 201, receipt_resp.text
    receipt = receipt_resp.json()
    posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text

    bill_resp = await client.post(
        "/api/v1/vendor-bills",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"LCB-{uuid.uuid4().hex[:8]}",
            "purchase_order_id": po["id"], "item_receipt_id": receipt["id"], "transaction_date": "2026-10-23",
            "lines": [
                {"item_id": str(world["item_id"]), "qty": 4, "unit_price": "5",
                 "purchase_order_line_id": po["lines"][0]["id"], "item_receipt_line_id": receipt["lines"][0]["id"]},
                {"is_landed_cost": True, "item_receipt_line_id": receipt["lines"][0]["id"],
                 "item_id": str(world["item_id"]), "description": "Freight bill", "amount": "12"},
            ],
        },
        headers=world["headers"],
    )
    assert bill_resp.status_code == 201, bill_resp.text
    bill_id = bill_resp.json()["id"]
    matched = await client.post(f"/api/v1/vendor-bills/{bill_id}/match", json={}, headers=world["headers"])
    assert matched.status_code == 200, matched.text
    assert matched.json()["match_status"] == "MATCHED", matched.text
    bill_posted = await client.post(f"/api/v1/vendor-bills/{bill_id}/post", json={}, headers=world["headers"])
    assert bill_posted.status_code == 200, bill_posted.text
    landed = bill_posted.json()["landed_cost"]
    assert landed["posting_type"] == "T-26" and landed["value_only"] is True
    assert Decimal(landed["allocated_total"]) == Decimal("12"), landed

    rows = await _landed_cost_rows(db_session, world["tenant_id"])
    actual = [row for row in rows if row["vendor_bill_id"] is not None]
    assert len(actual) == 1 and actual[0]["status"] == "POSTED"
    assert Decimal(str(actual[0]["total_amount"])) == Decimal("12")
    ledger = await _value_only_ledger(db_session, world["tenant_id"], uuid.UUID(bill_id))
    # T-04 bill value (20) + T-26 landed-cost value (12), both qty 0.
    assert len(ledger) == 2
    assert sum((Decimal(str(row["qty_base"])) for row in ledger), Decimal("0")) == 0
    assert sum((Decimal(str(row["value_base"])) for row in ledger), Decimal("0")) == Decimal("32")
    on_hand = await db_session.execute(
        text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    assert Decimal(str(on_hand.scalar_one())) == Decimal("4")


async def test_invalid_landed_cost_receipt_ref_rejected_and_rolled_back(client, world, db_session) -> None:
    po = await _approved_po(client, world, qty=2)
    receipt_resp = await client.post("/api/v1/item-receipts", json=_receipt_payload(world, po, 2), headers=world["headers"])
    assert receipt_resp.status_code == 201, receipt_resp.text
    receipt = receipt_resp.json()
    await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])

    bad_bill = await client.post(
        "/api/v1/vendor-bills",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"BAD-{uuid.uuid4().hex[:8]}",
            "item_receipt_id": receipt["id"], "transaction_date": "2026-10-23",
            "lines": [{"is_landed_cost": True, "item_receipt_line_id": str(uuid.uuid4()), "description": "Bogus freight", "amount": "5"}],
        },
        headers=world["headers"],
    )
    assert bad_bill.status_code == 404, bad_bill.text
    count = await db_session.execute(
        text("SELECT count(*) FROM procurement.vendor_bill WHERE tenant_id=:t AND vendor_bill_number LIKE 'BAD-%'"), {"t": world["tenant_id"]}
    )
    assert int(count.scalar_one()) == 0
    landed = await _landed_cost_rows(db_session, world["tenant_id"])
    assert [row for row in landed if row["vendor_bill_id"] is not None] == []


async def test_receipt_landed_cost_replay_is_idempotent(client, world, db_session) -> None:
    po = await _approved_po(client, world, qty=2)
    payload = _receipt_payload(world, po, 2, charges=[{"description": "Freight", "amount": "7"}])
    headers = {**world["headers"], "Idempotency-Key": "landed-receipt-replay-1"}
    first = await client.post("/api/v1/item-receipts", json=payload, headers=headers)
    second = await client.post("/api/v1/item-receipts", json=payload, headers=headers)
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["landed_cost"]["id"] == second.json()["landed_cost"]["id"]
    rows = await _landed_cost_rows(db_session, world["tenant_id"])
    assert len(rows) == 1
