"""Invariant DM-PROC-02/03/04 (FS-08 exit gate): receipt/bill match quantities + value-only bills."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from app.modules.procurement.testing import seed_procurement_world

pytestmark = pytest.mark.integration


def _po_payload(world: dict[str, Any], qty: int, price: str = "5") -> dict[str, Any]:
    return {
        "vendor_id": str(world["vendor_id"]), "po_type": "STANDARD", "transaction_date": "2026-10-23",
        "lines": [{"item_id": str(world["item_id"]), "qty_ordered": qty, "unit_price": price, "expected_receipt_date": "2026-11-01"}],
    }


async def _po(service, world, qty: int, price: str = "5") -> dict[str, Any]:
    po = await service.create_purchase_order(_po_payload(world, qty, price), actor_id=str(world["app_user_id"]))
    submitted = await service.submit_purchase_order(po["id"], actor_id=str(world["app_user_id"]))
    if submitted["status"] != "APPROVED":
        await service.approve_purchase_order(po["id"], actor_id=str(world["app_user_id"]))
    return await service.get_purchase_order(po["id"])


async def _receipt(service, world, po, qty: int) -> dict[str, Any]:
    receipt = await service.create_receipt({
        "purchase_order_id": po["id"], "vendor_id": str(world["vendor_id"]), "receipt_type": "PO",
        "location_id": str(world["location_id"]), "received_at": "2026-10-23T10:00:00Z",
        "lines": [{
            "purchase_order_line_id": po["lines"][0]["id"], "item_id": str(world["item_id"]),
            "qty_received": qty, "unit_cost": po["lines"][0]["unit_price"],
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]),
            "details": [{"qty_received": qty, "inventory_status_id": str(world["status_id"]), "ownership": "OWNED"}],
        }],
    }, actor_id=str(world["app_user_id"]))
    await service.post_receipt(receipt["id"], actor_id=str(world["app_user_id"]))
    return await service.get_receipt(receipt["id"])


async def _bill(service, world, po, receipt, qty: int, price: str) -> dict[str, Any]:
    return await service.create_bill({
        "vendor_id": str(world["vendor_id"]), "vendor_bill_number": f"IB-{qty}-{price}-{__import__('uuid').uuid4().hex[:8]}",
        "purchase_order_id": po["id"], "item_receipt_id": receipt["id"], "transaction_date": "2026-10-23",
        "lines": [{
            "item_id": str(world["item_id"]), "qty": qty, "unit_price": price,
            "purchase_order_line_id": po["lines"][0]["id"], "item_receipt_line_id": receipt["lines"][0]["id"],
        }],
    }, actor_id=str(world["app_user_id"]))


async def test_bill_qty_cannot_exceed_received_and_match_flags_exception(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVBM")
    service = world["service"]
    po = await _po(service, world, qty=4)
    receipt = await _receipt(service, world, po, 4)
    over_bill = await _bill(service, world, po, receipt, 5, "5")
    match = await service.match_bill(over_bill["id"], actor_id=str(world["app_user_id"]))
    assert "QTY_BILLED_GT_RECEIVED" in match["exception_codes"], match
    assert match["requires_override"] is True


async def test_price_variance_tolerance_and_exception_codes(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVPV")
    service = world["service"]
    po = await _po(service, world, qty=4)
    receipt = await _receipt(service, world, po, 4)
    # 20% price variance is far beyond the default 1% tolerance.
    bill = await _bill(service, world, po, receipt, 4, "6")
    match = await service.match_bill(bill["id"], actor_id=str(world["app_user_id"]))
    assert "PRICE_DIFF_GT_TOL" in match["exception_codes"], match
    variances = await service.bill_variances(bill["id"])
    assert any(item["variance_type"] == "PRICE" for item in variances["items"])


async def test_value_only_bill_posting_never_changes_inventory_qty(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVVOB")
    service = world["service"]
    po = await _po(service, world, qty=4)
    receipt = await _receipt(service, world, po, 4)
    before = (
        await db_session.execute(
            text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"),
            {"t": world["tenant_id"], "i": world["item_id"]},
        )
    ).scalar_one()
    bill = await _bill(service, world, po, receipt, 4, "5")
    matched = await service.match_bill(bill["id"], actor_id=str(world["app_user_id"]))
    assert matched["match_status"] == "MATCHED", matched
    await service.post_bill(bill["id"], actor_id=str(world["app_user_id"]))
    after = (
        await db_session.execute(
            text("SELECT coalesce(sum(qty_on_hand),0) FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"),
            {"t": world["tenant_id"], "i": world["item_id"]},
        )
    ).scalar_one()
    assert Decimal(str(before)) == Decimal(str(after)), "DM-PROC-04: a vendor bill must never re-cost/re-quantity received inventory"
    ledger = (
        await db_session.execute(
            text(
                "SELECT coalesce(sum(l.qty_base),0), count(*) FROM inventory_ledger.inventory_transaction_line l "
                "JOIN inventory_ledger.inventory_transaction t ON t.tenant_id=l.tenant_id AND t.id=l.inventory_transaction_id "
                "WHERE l.tenant_id=:t AND t.source_doc_id=:bill"
            ),
            {"t": world["tenant_id"], "bill": __import__("uuid").UUID(bill["id"])},
        )
    ).one()
    assert Decimal(str(ledger[0])) == Decimal("0") and int(ledger[1]) >= 1
