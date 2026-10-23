"""Invariant PROC-PO-OPEN (FS-08 exit gate): open quantities never negative and never over-allocated.

Runs against the live procurement schema (graceful skip without DB) using the service-level P2P flow.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.modules.procurement.testing import seed_procurement_world

pytestmark = pytest.mark.integration


def _open(line: dict[str, Any]) -> tuple[Decimal, Decimal]:
    return (
        Decimal(str(line["open_for_receipt"])),
        Decimal(str(line["open_for_billing"])),
    )


async def _approved_po(service, world: dict[str, Any], qty: int = 10) -> dict[str, Any]:
    po = await service.create_purchase_order({
        "vendor_id": str(world["vendor_id"]), "po_type": "STANDARD", "transaction_date": "2026-10-23",
        "lines": [{"item_id": str(world["item_id"]), "qty_ordered": qty, "unit_price": "5", "expected_receipt_date": "2026-11-01"}],
    }, actor_id=str(world["app_user_id"]))
    submitted = await service.submit_purchase_order(po["id"], actor_id=str(world["app_user_id"]))
    if submitted["status"] != "APPROVED":
        await service.approve_purchase_order(po["id"], actor_id=str(world["app_user_id"]))
    return await service.get_purchase_order(po["id"])


async def _receive(service, world: dict[str, Any], po: dict[str, Any], qty: int) -> dict[str, Any]:
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


async def test_po_open_qty_identity_and_lifecycle(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVPO")
    service = world["service"]
    po = await _approved_po(service, world, qty=10)
    assert _open(po["lines"][0]) == (Decimal("10"), Decimal("0"))

    await _receive(service, world, po, 4)
    po = await service.get_purchase_order(po["id"])
    open_receipt, open_billing = _open(po["lines"][0])
    assert open_receipt == Decimal("6") and open_billing == Decimal("4")
    assert po["status"] == "PARTIALLY_RECEIVED"

    await _receive(service, world, po, 6)
    po = await service.get_purchase_order(po["id"])
    assert _open(po["lines"][0]) == (Decimal("0"), Decimal("10"))
    assert po["status"] == "RECEIVED"

    for line in po["lines"]:
        open_receipt, open_billing = _open(line)
        assert open_receipt >= 0, "PROC-PO-OPEN: open_for_receipt must never be negative"
        assert open_billing >= 0, "PROC-PO-OPEN: open_for_billing must never be negative"


async def test_po_open_qty_over_receipt_guard(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVOV")
    service = world["service"]
    po = await _approved_po(service, world, qty=5)
    await _receive(service, world, po, 4)
    with pytest.raises(Exception) as exc:
        await _receive(service, world, po, 2)
    assert getattr(exc.value, "code", "") == "QUANTITY_EXCEEDS_ORDER"
    po = await service.get_purchase_order(po["id"])
    assert _open(po["lines"][0])[0] == Decimal("1")
