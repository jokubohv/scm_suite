"""FS-05/FS-08 landed-cost true-up: T-06 absorption variance and T-27 estimated-cost reversal."""

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


async def _posted_receipt_with_estimate(client, world: dict[str, Any], *, estimate: str = "25") -> dict[str, Any]:
    po = await _approved_po(client, world, qty=4)
    created = await client.post(
        "/api/v1/item-receipts",
        json=_receipt_payload(world, po, 4, charges=[{"description": "Freight", "amount": estimate}]),
        headers=world["headers"],
    )
    assert created.status_code == 201, created.text
    receipt = created.json()
    posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    receipt["posted"] = posted.json()
    receipt["po"] = po
    return receipt


async def _bill_with_actual(client, world: dict[str, Any], receipt: dict[str, Any], *, actual: str = "30"):
    return await client.post(
        "/api/v1/vendor-bills",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"TU-{uuid.uuid4().hex[:8]}",
            "purchase_order_id": receipt["po"]["id"], "item_receipt_id": receipt["id"], "transaction_date": "2026-10-23",
            "lines": [
                {"item_id": str(world["item_id"]), "qty": 4, "unit_price": "5",
                 "purchase_order_line_id": receipt["po"]["lines"][0]["id"], "item_receipt_line_id": receipt["lines"][0]["id"]},
                {"is_landed_cost": True, "item_receipt_line_id": receipt["lines"][0]["id"],
                 "item_id": str(world["item_id"]), "description": "Actual freight", "amount": actual},
            ],
        },
        headers=world["headers"],
    )


async def _post_bill(client, world: dict[str, Any], bill_id: str) -> dict[str, Any]:
    matched = await client.post(f"/api/v1/vendor-bills/{bill_id}/match", json={}, headers=world["headers"])
    assert matched.status_code == 200 and matched.json()["match_status"] == "MATCHED", matched.text
    posted = await client.post(f"/api/v1/vendor-bills/{bill_id}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    return posted.json()


async def _variances(db_session: Any, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await db_session.execute(
        text("SELECT * FROM costing.cost_variance WHERE tenant_id=:t AND variance_type='LANDED_COST_ABSORPTION' ORDER BY created_at"),
        {"t": tenant_id},
    )
    return [dict(row._mapping) for row in rows]


async def _txn(db_session: Any, tenant_id: uuid.UUID, txn_id: Any) -> dict[str, Any]:
    rows = await db_session.execute(
        text("SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND id=:id"), {"t": tenant_id, "id": txn_id}
    )
    return dict(rows.mappings().one())


async def test_actual_higher_posts_positive_t06_variance(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="30")
    posted = await _post_bill(client, world, bill.json()["id"])
    variance = posted["landed_cost"]["variance"]
    assert variance["posting_type"] == "T-06" and variance["value_only"] is True
    assert Decimal(variance["estimated_total"]) == Decimal("25")
    assert Decimal(variance["actual_total"]) == Decimal("30")
    assert Decimal(variance["total_variance"]) == Decimal("5")
    assert len(variance["variance_ids"]) == 1

    rows = await _variances(db_session, world["tenant_id"])
    assert len(rows) == 1
    row = rows[0]
    assert Decimal(str(row["amount_base"])) == Decimal("5")
    assert row["vendor_bill_line_id"] is not None and row["status"] == "OPEN"
    assert row["detail"]["estimated_amount"] == "25" and row["detail"]["actual_amount"] == "30"
    txn = await _txn(db_session, world["tenant_id"], row["inventory_transaction_id"])
    assert txn["transaction_type"] == "T-06"
    assert Decimal(str(txn["total_value_base"])) == Decimal("5")
    lines = await db_session.execute(
        text("SELECT qty_base, value_base FROM inventory_ledger.inventory_transaction_line WHERE tenant_id=:t AND inventory_transaction_id=:id"),
        {"t": world["tenant_id"], "id": row["inventory_transaction_id"]},
    )
    line_rows = [dict(item._mapping) for item in lines]
    assert sum((Decimal(str(item["qty_base"])) for item in line_rows), Decimal("0")) == 0
    assert sum((Decimal(str(item["value_base"])) for item in line_rows), Decimal("0")) == Decimal("5")


async def test_actual_lower_posts_negative_t06_variance(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="20")
    posted = await _post_bill(client, world, bill.json()["id"])
    variance = posted["landed_cost"]["variance"]
    assert Decimal(variance["total_variance"]) == Decimal("-5")
    rows = await _variances(db_session, world["tenant_id"])
    assert len(rows) == 1 and Decimal(str(rows[0]["amount_base"])) == Decimal("-5")
    txn = await _txn(db_session, world["tenant_id"], rows[0]["inventory_transaction_id"])
    assert txn["transaction_type"] == "T-06" and Decimal(str(txn["total_value_base"])) == Decimal("-5")


async def test_equal_or_missing_estimate_posts_no_variance(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="25")
    posted = await _post_bill(client, world, bill.json()["id"])
    assert "variance" not in posted["landed_cost"]
    assert await _variances(db_session, world["tenant_id"]) == []

    # A receipt without charges has no estimate to compare against.
    po = await _approved_po(client, world, qty=4)
    plain = await client.post("/api/v1/item-receipts", json=_receipt_payload(world, po, 4), headers=world["headers"])
    assert plain.status_code == 201, plain.text
    await client.post(f"/api/v1/item-receipts/{plain.json()['id']}/post", json={}, headers=world["headers"])
    plain_bill = await _bill_with_actual(client, world, {**plain.json(), "po": po}, actual="12")
    posted_plain = await _post_bill(client, world, plain_bill.json()["id"])
    assert "variance" not in posted_plain["landed_cost"]
    assert len(await _variances(db_session, world["tenant_id"])) == 0


async def test_receipt_reversal_reverses_t27_once(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    estimated = await db_session.execute(
        text("SELECT * FROM costing.landed_cost WHERE tenant_id=:t AND estimated ORDER BY created_at DESC LIMIT 1"),
        {"t": world["tenant_id"]},
    )
    estimated_row = dict(estimated.mappings().one())
    original_txn = estimated_row["posted_transaction_id"]
    assert original_txn is not None

    reversed_resp = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "test reversal"}, headers=world["headers"]
    )
    assert reversed_resp.status_code == 200, reversed_resp.text
    reversal = reversed_resp.json()["landed_cost_reversal"]
    assert reversal["posting_type"] == "T-27" and reversal["value_only"] is True
    assert Decimal(reversal["amount"]) == Decimal("-25")
    assert reversal["original_transaction_id"] == str(original_txn)

    rows = await db_session.execute(
        text("SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND reversal_of_transaction_id=:orig"),
        {"t": world["tenant_id"], "orig": original_txn},
    )
    reversal_rows = [dict(row._mapping) for row in rows]
    assert len(reversal_rows) == 1
    assert reversal_rows[0]["transaction_type"] == "T-27"
    assert Decimal(str(reversal_rows[0]["total_value_base"])) == Decimal("-25")
    status = await db_session.execute(
        text("SELECT status FROM costing.landed_cost WHERE tenant_id=:t AND id=:id"), {"t": world["tenant_id"], "id": estimated_row["id"]}
    )
    assert status.scalar_one() == "REVERSED"

    second = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "again"}, headers=world["headers"]
    )
    assert second.status_code in {409, 422}, second.text
    count = await db_session.execute(
        text("SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND reversal_of_transaction_id=:orig"),
        {"t": world["tenant_id"], "orig": original_txn},
    )
    assert int(count.scalar_one()) == 1


async def test_receipt_reversal_rejected_after_bill_actual(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="30")
    await _post_bill(client, world, bill.json()["id"])
    reverse = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "should be blocked"}, headers=world["headers"]
    )
    assert reverse.status_code == 409, reverse.text
    assert reverse.json()["code"] == "DEPENDENT_DOCUMENTS"
    status = await db_session.execute(
        text("SELECT status FROM costing.landed_cost WHERE tenant_id=:t AND estimated ORDER BY created_at DESC LIMIT 1"),
        {"t": world["tenant_id"]},
    )
    assert status.scalar_one() == "POSTED"
    count = await db_session.execute(
        text("SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND transaction_type='T-27' AND reversal_of_transaction_id IS NOT NULL"),
        {"t": world["tenant_id"]},
    )
    assert int(count.scalar_one()) == 0


async def test_bill_post_replay_does_not_duplicate_variance(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="30")
    await _post_bill(client, world, bill.json()["id"])
    second = await client.post(f"/api/v1/vendor-bills/{bill.json()['id']}/post", json={}, headers=world["headers"])
    assert second.status_code in {409, 422}, second.text
    assert len(await _variances(db_session, world["tenant_id"])) == 1
