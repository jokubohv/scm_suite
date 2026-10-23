"""FS-08 Procurement P2P integration: vendor master → PO → receipts → bill match/post → credit/return/drop-ship."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _po_payload(world: dict[str, Any], *, qty: int = 10, price: str = "5", po_type: str = "STANDARD", external: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "item_id": str(world["item_id"]), "qty_ordered": qty, "unit_price": price,
        "expected_receipt_date": "2026-11-01",
    }
    if external:
        line["external_sales_ref"] = external
        line["is_drop_ship"] = po_type == "DROP_SHIP"
        line["is_special_order"] = po_type == "SPECIAL_ORDER"
    return {
        "vendor_id": str(world["vendor_id"]), "po_type": po_type, "currency_code": "USD",
        "transaction_date": "2026-10-23", "ship_to_location_id": str(world["location_id"]),
        "lines": [line],
    }


async def _create_po(client, world, **kwargs: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/purchase-orders", json=_po_payload(world, **kwargs), headers=world["headers"])
    assert response.status_code == 201, response.text
    return response.json()


async def _submit_and_approve(client, world, po_id: str) -> dict[str, Any]:
    submitted = await client.post(f"/api/v1/purchase-orders/{po_id}/submit", headers=world["headers"])
    assert submitted.status_code == 200, submitted.text
    if submitted.json()["status"] != "APPROVED":
        approved = await client.post(f"/api/v1/purchase-orders/{po_id}/approve", json={}, headers=world["headers"])
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
    detail = await client.get(f"/api/v1/purchase-orders/{po_id}", headers=world["headers"])
    assert detail.json()["status"] == "APPROVED"
    return detail.json()


async def _receipt(client, world, po_detail: dict[str, Any], qty: int, *, post: bool = True) -> dict[str, Any]:
    po_line = po_detail["lines"][0]
    payload = {
        "purchase_order_id": po_detail["id"], "vendor_id": str(world["vendor_id"]),
        "receipt_type": "PO", "received_at": "2026-10-23T10:00:00Z",
        "location_id": str(world["location_id"]),
        "lines": [{
            "purchase_order_line_id": po_line["id"], "item_id": str(world["item_id"]),
            "qty_received": qty, "unit_cost": po_line["unit_price"],
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            "details": [{"qty_received": qty, "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
        }],
    }
    created = await client.post("/api/v1/item-receipts", json=payload, headers=world["headers"])
    assert created.status_code == 201, created.text
    receipt = created.json()
    if post:
        posted = await client.post(f"/api/v1/item-receipts/{receipt['id']}/post", json={}, headers=world["headers"])
        assert posted.status_code == 200, posted.text
        receipt = {**receipt, **posted.json()}
    return receipt


async def _bill(client, world, po_detail: dict[str, Any], receipt: dict[str, Any], qty: int, unit_price: str) -> dict[str, Any]:
    payload = {
        "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"BILL-{uuid.uuid4().hex[:10]}",
        "purchase_order_id": po_detail["id"], "item_receipt_id": receipt["id"],
        "currency_code": "USD", "transaction_date": "2026-10-23",
        "lines": [{
            "item_id": str(world["item_id"]), "qty": qty, "unit_price": unit_price,
            "purchase_order_line_id": po_detail["lines"][0]["id"],
            "item_receipt_line_id": receipt["lines"][0]["id"],
        }],
    }
    created = await client.post("/api/v1/vendor-bills", json=payload, headers=world["headers"])
    assert created.status_code == 201, created.text
    return created.json()


async def test_vendor_master_item_vendor_and_purchase_history(client, world) -> None:
    category = await client.post("/api/v1/vendor-categories", json={"code": "SUPPLIES", "name": "Supplies"}, headers=world["headers"])
    assert category.status_code == 201, category.text
    vendor = await client.post(
        "/api/v1/vendors",
        json={"vendor_number": "V-1001", "name": "Acme Supplies", "vendor_category_id": category.json()["id"], "currency_code": "USD", "credit_limit": "5000"},
        headers=world["headers"],
    )
    assert vendor.status_code == 201, vendor.text
    vendor_id = vendor.json()["id"]
    association = await client.post(
        "/api/v1/item-vendors",
        json={"item_id": str(world["item_id"]), "vendor_id": vendor_id, "is_preferred": True, "lead_time_days": 3, "min_order_qty": "1"},
        headers=world["headers"],
    )
    assert association.status_code == 201, association.text
    price = await client.post(
        f"/api/v1/item-vendors/{association.json()['id']}/prices",
        json={"qty_min": 0, "unit_price": "4.75", "currency_code": "USD", "effective_from": "2026-01-01"},
        headers=world["headers"],
    )
    assert price.status_code == 201, price.text
    prices = await client.get(f"/api/v1/item-vendors/{association.json()['id']}/prices", headers=world["headers"])
    assert prices.status_code == 200 and len(prices.json()["items"]) == 1
    history = await client.get(f"/api/v1/vendors/{vendor_id}/purchase-history", headers=world["headers"])
    assert history.status_code == 200


async def test_partial_receipts_open_qty_bill_match_and_value_only_posting(client, world, db_session) -> None:
    po = await _create_po(client, world, qty=10, price="5")
    detail = await _submit_and_approve(client, world, po["id"])

    first = await _receipt(client, world, detail, 4)
    assert first["status"] == "POSTED"
    status1 = await client.get(f"/api/v1/purchase-orders/{detail['id']}/status", headers=world["headers"])
    assert status1.status_code == 200
    line1 = status1.json()["lines"][0]
    assert line1["qty_received"] == "4" and line1["open_for_receipt"] == "6"
    assert status1.json()["status"] == "PARTIALLY_RECEIVED"

    unbilled = await client.get("/api/v1/item-receipts/unbilled", params={"vendor_id": str(world["vendor_id"])}, headers=world["headers"])
    assert unbilled.status_code == 200 and len(unbilled.json()["items"]) == 1
    assert unbilled.json()["items"][0]["remaining_qty"] == "4"

    bill = await _bill(client, world, detail, first, 4, "5")
    matched = await client.post(f"/api/v1/vendor-bills/{bill['id']}/match", json={}, headers=world["headers"])
    assert matched.status_code == 200, matched.text
    assert matched.json()["match_status"] == "MATCHED", matched.text
    posted = await client.post(f"/api/v1/vendor-bills/{bill['id']}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "OPEN"

    # Value-only posting: a ledger line exists with qty 0 and no inventory balance change.
    ledger_lines = (
        await db_session.execute(
            __import__("sqlalchemy").text(
                "SELECT count(*) FROM inventory_ledger.inventory_transaction_line l "
                "JOIN inventory_ledger.inventory_transaction t ON t.tenant_id=l.tenant_id AND t.id=l.inventory_transaction_id "
                "WHERE l.tenant_id=:tenant AND t.source_doc_id=:bill AND l.qty_base=0"
            ),
            {"tenant": world["tenant_id"], "bill": uuid.UUID(bill["id"])},
        )
    ).scalar_one()
    assert int(ledger_lines) == 1
    on_hand = (
        await db_session.execute(
            __import__("sqlalchemy").text(
                "SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:tenant AND item_id=:item"
            ),
            {"tenant": world["tenant_id"], "item": world["item_id"]},
        )
    ).scalar_one()
    assert str(on_hand) in {"4.000000000000", "4"}

    second = await _receipt(client, world, detail, 6)
    assert second["status"] == "POSTED"
    status2 = await client.get(f"/api/v1/purchase-orders/{detail['id']}/status", headers=world["headers"])
    assert status2.json()["status"] == "RECEIVED" and status2.json()["lines"][0]["open_for_receipt"] == "0"

    bill2 = await _bill(client, world, detail, second, 6, "5")
    await client.post(f"/api/v1/vendor-bills/{bill2['id']}/match", json={}, headers=world["headers"])
    posted2 = await client.post(f"/api/v1/vendor-bills/{bill2['id']}/post", json={}, headers=world["headers"])
    assert posted2.status_code == 200, posted2.text
    status3 = await client.get(f"/api/v1/purchase-orders/{detail['id']}/status", headers=world["headers"])
    assert status3.json()["status"] == "BILLED"

    finance = (
        await db_session.execute(
            __import__("sqlalchemy").text("SELECT count(*) FROM platform.finance_journal_intent WHERE tenant_id=:tenant AND source_doc_id IN (:b1,:b2)"),
            {"tenant": world["tenant_id"], "b1": uuid.UUID(bill["id"]), "b2": uuid.UUID(bill2["id"])},
        )
    ).scalar_one()
    assert int(finance) >= 4


async def test_over_receipt_rejected_and_close_guard(client, world) -> None:
    po = await _create_po(client, world, qty=5, price="5")
    detail = await _submit_and_approve(client, world, po["id"])
    await _receipt(client, world, detail, 4)
    over = await client.post(
        "/api/v1/item-receipts",
        json={
            "purchase_order_id": detail["id"], "vendor_id": str(world["vendor_id"]), "receipt_type": "PO",
            "location_id": str(world["location_id"]),
            "lines": [{
                "purchase_order_line_id": detail["lines"][0]["id"], "item_id": str(world["item_id"]),
                "qty_received": 2, "unit_cost": "5", "location_id": str(world["location_id"]),
                "inventory_status_id": str(world["status_id"]), "details": [{"qty_received": 2}],
            }],
        },
        headers=world["headers"],
    )
    assert over.status_code == 409, over.text
    assert over.json()["code"] == "QUANTITY_EXCEEDS_ORDER"

    # Close is blocked while received-not-billed remains.
    close = await client.post(f"/api/v1/purchase-orders/{detail['id']}/close", json={"reason": "test"}, headers=world["headers"])
    assert close.status_code == 409, close.text


async def test_credit_return_and_drop_ship(client, world, db_session) -> None:
    po = await _create_po(client, world, qty=4, price="5")
    detail = await _submit_and_approve(client, world, po["id"])
    receipt = await _receipt(client, world, detail, 4)

    return_create = await client.post(
        "/api/v1/vendor-returns",
        json={
            "vendor_id": str(world["vendor_id"]), "purchase_order_id": detail["id"], "reason_code": "DAMAGED",
            "transaction_date": "2026-10-23",
            "lines": [{
                "item_id": str(world["item_id"]), "purchase_order_line_id": detail["lines"][0]["id"],
                "item_receipt_line_id": receipt["lines"][0]["id"], "qty_authorized": 2, "unit_cost": "5",
                "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            }],
        },
        headers=world["headers"],
    )
    assert return_create.status_code == 201, return_create.text
    return_id = return_create.json()["id"]
    assert (await client.post(f"/api/v1/vendor-returns/{return_id}/approve", headers=world["headers"])).status_code == 200
    shipped = await client.post(f"/api/v1/vendor-returns/{return_id}/ship", json={}, headers=world["headers"])
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["status"] == "SHIPPED"
    on_hand = (
        await db_session.execute(
            __import__("sqlalchemy").text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:tenant AND item_id=:item"),
            {"tenant": world["tenant_id"], "item": world["item_id"]},
        )
    ).scalar_one()
    assert str(on_hand) in {"2.000000000000", "2"}

    credit = await client.post(
        "/api/v1/vendor-credits",
        json={
            "vendor_id": str(world["vendor_id"]), "vendor_return_id": return_id, "transaction_date": "2026-10-23",
            "lines": [{"item_id": str(world["item_id"]), "qty": 2, "unit_price": "5"}],
        },
        headers=world["headers"],
    )
    assert credit.status_code == 201, credit.text
    credit_id = credit.json()["id"]
    posted_credit = await client.post(f"/api/v1/vendor-credits/{credit_id}/post", json={}, headers=world["headers"])
    assert posted_credit.status_code == 200, posted_credit.text
    credited = await client.post(f"/api/v1/vendor-returns/{return_id}/credit", json={"vendor_credit_id": credit_id}, headers=world["headers"])
    assert credited.status_code == 200 and credited.json()["status"] == "CREDITED"

    drop_po = await _create_po(client, world, qty=3, price="7", po_type="DROP_SHIP", external="SO-100")
    drop_detail = await _submit_and_approve(client, world, drop_po["id"])
    link = await client.post(
        "/api/v1/special-order-links",
        json={
            "link_type": "DROP_SHIP", "external_sales_ref": "SO-100", "external_sales_line_ref": "1",
            "item_id": str(world["item_id"]), "purchase_order_id": drop_detail["id"],
            "purchase_order_line_id": drop_detail["lines"][0]["id"], "qty": 3,
        },
        headers=world["headers"],
    )
    assert link.status_code == 201, link.text
    link_id = link.json()["id"]
    fulfilled = await client.post(f"/api/v1/special-order-links/{link_id}/fulfil", json={"qty": 3}, headers=world["headers"])
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json()["status"] == "FULFILLED"
    # T-28 is value-only: inventory qty unchanged (still 2 after the return).
    after = (
        await db_session.execute(
            __import__("sqlalchemy").text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:tenant AND item_id=:item"),
            {"tenant": world["tenant_id"], "item": world["item_id"]},
        )
    ).scalar_one()
    assert str(after) in {"2.000000000000", "2"}


async def test_state_machine_guards(client, world) -> None:
    po = await _create_po(client, world, qty=2, price="5")
    # receipt on a DRAFT PO is rejected
    bad_receipt = await client.post(
        "/api/v1/item-receipts",
        json={
            "purchase_order_id": po["id"], "vendor_id": str(world["vendor_id"]), "receipt_type": "PO",
            "location_id": str(world["location_id"]),
            "lines": [{"purchase_order_line_id": po["lines"][0]["id"], "item_id": str(world["item_id"]), "qty_received": 1,
                       "unit_cost": "5", "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"])}],
        },
        headers=world["headers"],
    )
    assert bad_receipt.status_code == 409, bad_receipt.text
    # reject/cancel from an invalid state
    approve_draft_only = await client.post(f"/api/v1/purchase-orders/{po['id']}/reject", json={"reason": "x"}, headers=world["headers"])
    assert approve_draft_only.status_code == 409
