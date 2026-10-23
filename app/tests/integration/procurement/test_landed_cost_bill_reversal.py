"""FS-08/FS-05 residual: posted vendor-bill reversal reverses T-04/T-26/T-06 and unlocks estimate reversal."""

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


def _receipt_payload(world: dict[str, Any], po: dict[str, Any], qty: int, *, estimate: str = "25") -> dict[str, Any]:
    return {
        "purchase_order_id": po["id"], "vendor_id": str(world["vendor_id"]), "receipt_type": "PO",
        "received_at": "2026-10-23T10:00:00Z", "location_id": str(world["location_id"]),
        "lines": [{
            "purchase_order_line_id": po["lines"][0]["id"], "item_id": str(world["item_id"]),
            "qty_received": qty, "unit_cost": po["lines"][0]["unit_price"],
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            "details": [{"qty_received": qty, "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
        }],
        "landed_cost_charges": [{"description": "Freight estimate", "amount": estimate}],
    }


async def _posted_receipt_with_estimate(client, world: dict[str, Any], *, estimate: str = "25") -> dict[str, Any]:
    po = await _approved_po(client, world, qty=4)
    created = await client.post("/api/v1/item-receipts", json=_receipt_payload(world, po, 4, estimate=estimate), headers=world["headers"])
    assert created.status_code == 201, created.text
    receipt = created.json()
    posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    receipt["po"] = po
    receipt["posted"] = posted.json()
    return receipt


async def _bill_with_actual(client, world: dict[str, Any], receipt: dict[str, Any], *, actual: str = "30"):
    return await client.post(
        "/api/v1/vendor-bills",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"REV-{uuid.uuid4().hex[:8]}",
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


async def _scalar(db_session: Any, sql: str, **params: Any) -> Any:
    result = await db_session.execute(text(sql), params)
    return result.scalar_one()


async def test_posted_bill_reversal_reverses_t04_t26_t06_then_estimate_reversal(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="30")
    posted = await _post_bill(client, world, bill.json()["id"])
    bill_id = bill.json()["id"]
    assert Decimal(posted["landed_cost"]["variance"]["total_variance"]) == Decimal("5")

    # receipt reversal is rejected while an active actual landed cost/bill exists
    blocked = await client.post(f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "pre"}, headers=world["headers"])
    assert blocked.status_code == 409, blocked.text

    reversed_resp = await client.post(
        f"/api/v1/vendor-bills/{bill_id}/cancel", json={"reason": "bill reversal test"}, headers=world["headers"]
    )
    assert reversed_resp.status_code == 200, reversed_resp.text
    body = reversed_resp.json()
    assert body["status"] == "VOID" and body["double_reverse_protected"] is True
    assert len(body["landed_cost_reversals"]) == 1
    t26 = body["landed_cost_reversals"][0]
    assert t26["status"] == "REVERSED" and t26["posting_type"] == "T-26"
    assert Decimal(t26["amount"]) == Decimal("-30")
    assert len(body["variance_reversals"]) == 1
    t06 = body["variance_reversals"][0]
    assert t06["status"] == "CLOSED" and t06["posting_type"] == "T-06"
    assert Decimal(t06["amount"]) == Decimal("5")

    # costing state + links
    bill_row = await db_session.execute(text("SELECT status FROM procurement.vendor_bill WHERE tenant_id=:t AND id=:id"), {"t": world["tenant_id"], "id": bill_id})
    assert bill_row.scalar_one() == "VOID"
    lc_rows = await db_session.execute(text("SELECT status, estimated FROM costing.landed_cost WHERE tenant_id=:t ORDER BY created_at"), {"t": world["tenant_id"]})
    lcs = [dict(row._mapping) for row in lc_rows]
    assert [lc["status"] for lc in lcs] == ["POSTED", "REVERSED"]  # estimated stays POSTED; actual reversed
    variance = await db_session.execute(text("SELECT status, detail FROM costing.cost_variance WHERE tenant_id=:t"), {"t": world["tenant_id"]})
    vrow = dict(variance.mappings().one())
    assert vrow["status"] == "CLOSED" and vrow["detail"]["status"] == "CLOSED"
    assert vrow["detail"]["reversal_transaction_id"] is not None

    # ledger reversals: T-04 (-20 non-landed lines), T-26 (-30 actual landed), T-06 (-5 variance), all value-only
    reversals = await db_session.execute(
        text("SELECT transaction_type, total_value_base, total_qty_base FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND reversal_of_transaction_id IS NOT NULL ORDER BY transaction_type"),
        {"t": world["tenant_id"]},
    )
    rev_rows = [dict(row._mapping) for row in reversals]
    by_type = {row["transaction_type"]: row for row in rev_rows}
    assert set(by_type) == {"T-04", "T-26", "T-06"}
    assert Decimal(str(by_type["T-04"]["total_value_base"])) == Decimal("-20")
    assert Decimal(str(by_type["T-26"]["total_value_base"])) == Decimal("-30")
    assert Decimal(str(by_type["T-06"]["total_value_base"])) == Decimal("-5")
    assert all(Decimal(str(row["total_qty_base"])) == 0 for row in rev_rows)
    on_hand_after_bill_reversal = await _scalar(
        db_session,
        "SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i",
        t=world["tenant_id"], i=world["item_id"],
    )
    assert Decimal(str(on_hand_after_bill_reversal)) == Decimal("4")  # value-only bill/T-26/T-06 reversals change no qty

    # now the receipt estimate reversal succeeds
    receipt_reversal = await client.post(
        f"/api/v1/item-receipts/{receipt['id']}/reverse", json={"reason": "after bill reversal"}, headers=world["headers"]
    )
    assert receipt_reversal.status_code == 200, receipt_reversal.text
    estimate_reversal = receipt_reversal.json()["landed_cost_reversal"]
    assert estimate_reversal["posting_type"] == "T-27" and Decimal(estimate_reversal["amount"]) == Decimal("-25")
    estimate = await db_session.execute(text("SELECT status FROM costing.landed_cost WHERE tenant_id=:t AND estimated"), {"t": world["tenant_id"]})
    assert estimate.scalar_one() == "REVERSED"

    # double reversal protected: second cancel fails and produces no extra transactions
    second = await client.post(f"/api/v1/vendor-bills/{bill_id}/cancel", json={"reason": "again"}, headers=world["headers"])
    assert second.status_code == 409, second.text
    count = await _scalar(
        db_session,
        "SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND reversal_of_transaction_id = :orig",
        t=world["tenant_id"], orig=posted["posted_transaction_id"],
    )
    assert int(count) == 1
    on_hand_after_receipt_reversal = await _scalar(
        db_session,
        "SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i",
        t=world["tenant_id"], i=world["item_id"],
    )
    assert Decimal(str(on_hand_after_receipt_reversal)) == Decimal("0")  # the receipt's own T-02 reversal removes qty


async def test_paid_bill_reversal_rejected_and_nothing_reversed(client, world, db_session) -> None:
    receipt = await _posted_receipt_with_estimate(client, world, estimate="25")
    bill = await _bill_with_actual(client, world, receipt, actual="30")
    posted = await _post_bill(client, world, bill.json()["id"])
    bill_id = bill.json()["id"]
    await db_session.execute(
        text("UPDATE procurement.vendor_bill SET amount_paid = 10 WHERE tenant_id=:t AND id=:id"),
        {"t": world["tenant_id"], "id": bill_id},
    )
    rejected = await client.post(f"/api/v1/vendor-bills/{bill_id}/cancel", json={"reason": "paid"}, headers=world["headers"])
    assert rejected.status_code == 409, rejected.text
    state = await db_session.execute(text("SELECT status, amount_paid FROM procurement.vendor_bill WHERE tenant_id=:t AND id=:id"), {"t": world["tenant_id"], "id": bill_id})
    row = dict(state.mappings().one())
    assert row["status"] == "OPEN" and Decimal(str(row["amount_paid"])) == Decimal("10")
    actual_status = await db_session.execute(text("SELECT status FROM costing.landed_cost WHERE tenant_id=:t AND NOT estimated"), {"t": world["tenant_id"]})
    assert actual_status.scalar_one() == "POSTED"
    count = await _scalar(
        db_session,
        "SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id=:t AND reversal_of_transaction_id = :orig",
        t=world["tenant_id"], orig=posted["posted_transaction_id"],
    )
    assert int(count) == 0


async def test_draft_bill_cancel_remains_cancellation_only(client, world, db_session) -> None:
    po = await _approved_po(client, world, qty=2)
    created = await client.post(
        "/api/v1/vendor-bills",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"DRF-{uuid.uuid4().hex[:8]}",
            "purchase_order_id": po["id"], "transaction_date": "2026-10-23",
            "lines": [{"item_id": str(world["item_id"]), "qty": 2, "unit_price": "5", "purchase_order_line_id": po["lines"][0]["id"]}],
        },
        headers=world["headers"],
    )
    assert created.status_code == 201, created.text
    cancelled = await client.post(f"/api/v1/vendor-bills/{created.json()['id']}/cancel", json={"reason": "draft"}, headers=world["headers"])
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
