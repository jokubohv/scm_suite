"""FS-13 integration: agreement → T-21 receipt → T-22 consume+bill → T-23 adjust → return → balances."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.integration


async def _agreement_with_item(client, world: dict[str, Any]) -> dict[str, Any]:
    agreement = await client.post(
        "/api/v1/consignment-agreements",
        json={
            "vendor_id": str(world["vendor_id"]),
            "start_date": "2026-01-01",
            "payment_trigger": "ON_SALE",
            "currency_code": "USD",
        },
        headers=world["headers"],
    )
    assert agreement.status_code == 201, agreement.text
    agreement_id = agreement.json()["id"]
    covered = await client.post(
        f"/api/v1/consignment-agreements/{agreement_id}/items",
        json={
            "item_id": str(world["item_id"]),
            "location_id": str(world["location_id"]),
            "consigned_status_id": str(world["consigned_status_id"]),
            "owned_status_id": str(world["owned_status_id"]),
            "unit_cost": "5",
            "min_qty": 0,
            "max_qty": 100,
        },
        headers=world["headers"],
    )
    assert covered.status_code == 201, covered.text
    activated = await client.post(
        f"/api/v1/consignment-agreements/{agreement_id}/activate", json={}, headers=world["headers"]
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "ACTIVE"
    return activated.json()


def _line(world: dict[str, Any], qty: int = 5) -> dict[str, Any]:
    return {
        "item_id": str(world["item_id"]),
        "location_id": str(world["location_id"]),
        "qty": qty,
        "uom_id": None,
        "lot_id": None,
        "serial_ids": [],
        "unit_cost": "5",
    }


async def test_agreement_receipt_consume_bill_adjust_and_return(client, world, db_session) -> None:
    agreement = await _agreement_with_item(client, world)
    agreement_id = agreement["id"]

    receipt = await client.post(
        "/api/v1/consignment-transfers",
        json={
            "transfer_type": "RECEIPT_TO_CONSIGNED",
            "consignment_agreement_id": agreement_id,
            "transaction_date": "2026-10-23",
            "lines": [_line(world, 5)],
        },
        headers=world["headers"],
    )
    assert receipt.status_code == 201, receipt.text
    receipt_id = receipt.json()["id"]
    posted = await client.post(f"/api/v1/consignment-transfers/{receipt_id}/post", json={}, headers=world["headers"])
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "POSTED"

    balances = await client.get(
        "/api/v1/consigned/balances", params={"item_id": str(world["item_id"])}, headers=world["headers"]
    )
    assert balances.status_code == 200, balances.text
    consigned_qty = sum(float(row["qty_on_hand"]) for row in balances.json()["consigned"])
    assert consigned_qty == 5.0

    consume = await client.post(
        "/api/v1/consignment-consumption",
        json={
            "consignment_agreement_id": agreement_id,
            "transaction_date": "2026-10-24",
            "external_ref": "SO-1",
            "lines": [_line(world, 2)],
        },
        headers=world["headers"],
    )
    assert consume.status_code == 200, consume.text
    consumed = consume.json()
    assert consumed["status"] == "POSTED"
    assert consumed["vendor_bill_id"], "T-22 must create the vendor bill in the same transaction (CONS-RULE-02)"
    assert len(consumed["consumption"]) == 1

    balances = await client.get(
        "/api/v1/consigned/balances", params={"item_id": str(world["item_id"])}, headers=world["headers"]
    )
    consigned_qty = sum(float(row["qty_on_hand"]) for row in balances.json()["consigned"])
    owned_qty = sum(float(row["qty_on_hand"]) for row in balances.json()["owned"])
    assert consigned_qty == 3.0 and owned_qty == 2.0

    adjust = await client.post(
        "/api/v1/consignment-transfers",
        json={
            "transfer_type": "ADJUST",
            "consignment_agreement_id": agreement_id,
            "transaction_date": "2026-10-25",
            "lines": [{**_line(world, 1), "ownership": "CONSIGNED"}],
        },
        headers=world["headers"],
    )
    assert adjust.status_code == 201, adjust.text
    adjusted = await client.post(
        f"/api/v1/consignment-transfers/{adjust.json()['id']}/post", json={}, headers=world["headers"]
    )
    assert adjusted.status_code == 200, adjusted.text

    return_doc = await client.post(
        "/api/v1/consignment-transfers",
        json={
            "transfer_type": "RETURN_TO_VENDOR",
            "consignment_agreement_id": agreement_id,
            "transaction_date": "2026-10-26",
            "lines": [_line(world, 1)],
        },
        headers=world["headers"],
    )
    assert return_doc.status_code == 201, return_doc.text
    returned = await client.post(
        f"/api/v1/consignment-transfers/{return_doc.json()['id']}/post", json={}, headers=world["headers"]
    )
    assert returned.status_code == 200, returned.text
    balances = await client.get(
        "/api/v1/consigned/balances", params={"item_id": str(world["item_id"])}, headers=world["headers"]
    )
    final_consigned = sum(float(row["qty_on_hand"]) for row in balances.json()["consigned"])
    assert final_consigned == 3.0  # 5 - 2 consumed - 1 returned + 1 adjusted = 3

    consumption = await client.get(
        "/api/v1/consignment-consumption", params={"agreement_id": agreement_id}, headers=world["headers"]
    )
    assert consumption.status_code == 200 and len(consumption.json()["items"]) == 1
    # finance intents for CONSRCPT / CONSOWN / CONSADJ were recorded (never GL writes).
    intents = await db_session.execute(
        __import__("sqlalchemy").text(
            "SELECT count(*) FROM platform.finance_journal_intent WHERE tenant_id=:t AND source_doc_type='CONSIGNMENT_TRANSFER'"
        ),
        {"t": world["tenant_id"]},
    )
    assert int(intents.scalar_one()) >= 6


async def test_eligibility_and_guards(client, world) -> None:
    eligible = await client.get(
        "/api/v1/consignment/eligibility",
        params={
            "item_id": str(world["item_id"]),
            "location_id": str(world["location_id"]),
            "vendor_id": str(world["vendor_id"]),
        },
        headers=world["headers"],
    )
    assert eligible.status_code == 200 and eligible.json()["eligible"] is True, eligible.text
    assembly = await client.post(
        "/api/v1/consignment-agreements",
        json={"vendor_id": str(world["vendor_id"]), "start_date": "2026-01-01"},
        headers=world["headers"],
    )
    guard = await client.post("/api/v1/consignment/guards/validate", headers=world["headers"])
    assert guard.status_code == 200 and "ok" in guard.json()

    settings = await client.get("/api/v1/consignment/settings", headers=world["headers"])
    assert settings.status_code == 200 and settings.json()["items"]
    patched = await client.patch(
        "/api/v1/consignment/settings", json={"payment_trigger_default": "ON_CONSUMPTION"}, headers=world["headers"]
    )
    assert patched.status_code == 200

    terminate = await client.post(
        f"/api/v1/consignment-agreements/{assembly.json()['id']}/terminate",
        json={"reason": "test"},
        headers=world["headers"],
    )
    assert terminate.status_code in {200, 409}
