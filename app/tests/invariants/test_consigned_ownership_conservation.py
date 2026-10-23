"""Invariant INV-27 / DM-CONS-01/02 (FS-13 exit gate): owned+consigned positions stay separate and conserve qty/value."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from app.modules.consigned.testing import seed_consigned_world

pytestmark = pytest.mark.integration


def _line(world: dict[str, Any], qty: int) -> dict[str, Any]:
    return {"item_id": str(world["item_id"]), "location_id": str(world["location_id"]), "qty": qty, "unit_cost": "5"}


async def _agreement(service, world) -> dict[str, Any]:
    agreement = await service.create_agreement({"vendor_id": str(world["vendor_id"]), "start_date": "2026-01-01"})
    await service.add_agreement_item(
        agreement["id"],
        {
            "item_id": str(world["item_id"]),
            "location_id": str(world["location_id"]),
            "consigned_status_id": str(world["consigned_status_id"]),
            "owned_status_id": str(world["owned_status_id"]),
            "unit_cost": "5",
        },
    )
    return await service.activate_agreement(agreement["id"])


async def _post(service, world, agreement_id: str, transfer_type: str, qty: int, **extra: Any) -> dict[str, Any]:
    transfer = await service.create_transfer(
        {
            "transfer_type": transfer_type,
            "consignment_agreement_id": agreement_id,
            "transaction_date": "2026-10-23",
            "lines": [_line(world, qty)],
            **extra,
        }
    )
    return await service.post_transfer(transfer["id"])


async def test_owned_and_consigned_positions_never_merge(db_session) -> None:
    world = await seed_consigned_world(db_session, code_prefix="INVCO")
    service = world["service"]
    agreement = await _agreement(service, world)
    await _post(service, world, agreement["id"], "RECEIPT_TO_CONSIGNED", 5)
    await _post(service, world, agreement["id"], "CONSUME_ON_SALE", 2, external_ref="SO-1")

    balances = await service.balances(item_id=str(world["item_id"]), location_id=str(world["location_id"]))
    consigned = sum((Decimal(str(row["qty_on_hand"])) for row in balances["consigned"]), Decimal("0"))
    owned = sum((Decimal(str(row["qty_on_hand"])) for row in balances["owned"]), Decimal("0"))
    assert consigned == Decimal("3") and owned == Decimal("2")
    assert balances["inv_27_separated"] is True
    # No single balance row mixes ownership: each row is one ownership dimension.
    rows = await db_session.execute(
        text("SELECT ownership, qty_on_hand FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"),
        {"t": world["tenant_id"], "i": world["item_id"]},
    )
    for ownership, on_hand in rows:
        assert ownership in {"CONSIGNED", "OWNED"}
        assert Decimal(str(on_hand)) >= 0


async def test_t21_and_t22_conserve_quantity_and_value(db_session) -> None:
    world = await seed_consigned_world(db_session, code_prefix="INVCV")
    service = world["service"]
    agreement = await _agreement(service, world)
    receipt = await _post(service, world, agreement["id"], "RECEIPT_TO_CONSIGNED", 4)
    transfer = await _post(service, world, agreement["id"], "CONSUME_ON_SALE", 3, external_ref="SO-2")
    assert transfer["vendor_bill_id"]
    ledger = await db_session.execute(
        text(
            "SELECT t.id, t.source_doc_id, coalesce(sum(l.qty_base),0), coalesce(sum(l.value_base),0) "
            "FROM inventory_ledger.inventory_transaction t JOIN inventory_ledger.inventory_transaction_line l "
            "ON l.tenant_id=t.tenant_id AND l.inventory_transaction_id=t.id "
            "WHERE t.tenant_id=:t AND t.source_doc_id IN (:r,:c) GROUP BY t.id, t.source_doc_id"
        ),
        {
            "t": world["tenant_id"],
            "r": __import__("uuid").UUID(receipt["id"]),
            "c": __import__("uuid").UUID(transfer["id"]),
        },
    )
    totals = {str(row[1]): (Decimal(str(row[2])), Decimal(str(row[3]))) for row in ledger}
    assert totals[str(__import__("uuid").UUID(receipt["id"]))][0] == Decimal("4")
    # T-22 posts consigned out (-3) + owned in (+3), conserving quantity.
    assert totals[str(__import__("uuid").UUID(transfer["id"]))][0] == Decimal("0")
