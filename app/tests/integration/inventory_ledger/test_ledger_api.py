"""FS-04 ledger integration tests: posting, idempotency, reversal, documents, commitments, isolation."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.models.platform import outbox_event
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


def _movement(world, qty: str, **overrides):
    movement = {
        "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]), "qty": qty,
        "movement_type": "ADJUSTMENT", "source_line_ref": "1",
    }
    movement.update(overrides)
    return movement


async def _post(client, world, movements, *, key: str, posting_type: str = "T-07", **extra):
    return await client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": posting_type, "transaction_date": "2026-10-23", "movements": movements, **extra},
        headers=world["headers"] | {"Idempotency-Key": key},
    )


async def test_post_adjustment_updates_balances_and_events(ledger_client, world, db_session) -> None:
    response = await _post(ledger_client, world, [_movement(world, "10")], key="adj-1")
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["transaction_type"] == "T-07" and result["status"] == "POSTED"
    transaction = await ledger_client.get(f"/api/v1/inventory-transactions/{result['transaction_id']}", headers=world["headers"])
    assert transaction.status_code == 200
    lines = await ledger_client.get(f"/api/v1/inventory-transactions/{result['transaction_id']}/lines", headers=world["headers"])
    assert lines.status_code == 200 and Decimal(lines.json()["items"][0]["qty_base"]) == Decimal("10")
    details = await ledger_client.get(f"/api/v1/inventory-transactions/{result['transaction_id']}/details", headers=world["headers"])
    assert details.status_code == 200 and len(details.json()["items"]) == 1
    balance = await ledger_client.get(
        "/api/v1/inventory/balances", headers=world["headers"],
        params={"item_id": str(world["item_id"]), "location_id": str(world["location_id"])},
    )
    assert balance.status_code == 200, balance.text
    row = balance.json()["items"][0]
    assert Decimal(row["qty_on_hand"]) == Decimal("10") and Decimal(row["qty_available"]) == Decimal("10")
    events = (await db_session.execute(select(outbox_event.c.event_type).where(outbox_event.c.tenant_id == world["tenant_id"]))).scalars().all()
    assert "inventory.adjusted" in events and "inventory.balance.changed" in events


async def test_negative_inventory_warn_policy_returns_string_warnings(ledger_client, world, db_session) -> None:
    await db_session.execute(
        text(
            "INSERT INTO platform.tenant_setting (id, tenant_id, namespace, key, value) "
            "VALUES (:id, :tenant_id, 'inventory', 'negative_inventory_policy', '\"WARN\"'::jsonb) "
            "ON CONFLICT (tenant_id, namespace, key) DO UPDATE SET value = '\"WARN\"'::jsonb"
        ),
        {"id": str(uuid.uuid4()), "tenant_id": str(world["tenant_id"])},
    )
    await db_session.commit()
    response = await _post(ledger_client, world, [_movement(world, "-1")], key="warn-policy-1")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["warnings"] == ["NEGATIVE_INVENTORY_WARN_POLICY"]
    assert all(isinstance(warning, str) for warning in body["warnings"])


async def test_idempotent_retry_returns_same_transaction(ledger_client, world) -> None:
    first = await _post(ledger_client, world, [_movement(world, "5")], key="idem-1")
    assert first.status_code == 201, first.text
    replay = await _post(ledger_client, world, [_movement(world, "5")], key="idem-1")
    assert replay.status_code in {200, 201}, replay.text
    assert replay.json()["transaction_id"] == first.json()["transaction_id"]
    assert replay.json().get("replayed") is True
    listed = await ledger_client.get("/api/v1/inventory-transactions", headers=world["headers"], params={"item_id": str(world["item_id"])})
    assert len(listed.json()["items"]) == 1


async def test_reversal_negates_and_balance_returns_to_zero(ledger_client, world) -> None:
    posted = (await _post(ledger_client, world, [_movement(world, "7")], key="rev-post")).json()
    reversal = await ledger_client.post(
        f"/api/v1/inventory-transactions/{posted['transaction_id']}/reverse",
        json={"reason": "test reversal"}, headers=world["headers"] | {"Idempotency-Key": "rev-1"},
    )
    assert reversal.status_code == 200, reversal.text
    assert reversal.json()["transaction_id"] != posted["transaction_id"]
    txn = await ledger_client.get(f"/api/v1/inventory-transactions/{posted['transaction_id']}", headers=world["headers"])
    assert txn.status_code == 200
    balance = await ledger_client.get("/api/v1/inventory/balances", headers=world["headers"], params={"item_id": str(world["item_id"])})
    assert Decimal(balance.json()["items"][0]["qty_on_hand"]) == Decimal("0")


async def test_adjustment_document_flow(ledger_client, world) -> None:
    created = await ledger_client.post(
        "/api/v1/inventory-adjustments",
        json={"transaction_date": "2026-10-23", "memo": "test", "lines": [{
            "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
            "inventory_status_id": str(world["status_id"]), "qty_delta": "3", "unit_cost": "2",
        }]},
        headers=world["headers"] | {"Idempotency-Key": "doc-1"},
    )
    assert created.status_code == 201, created.text
    document = created.json()
    submitted = await ledger_client.post(f"/api/v1/inventory-adjustments/{document['id']}/submit", headers=world["headers"] | {"Idempotency-Key": "doc-submit"})
    assert submitted.status_code == 200
    posted = await ledger_client.post(f"/api/v1/inventory-adjustments/{document['id']}/post", headers=world["headers"] | {"Idempotency-Key": "doc-post"})
    assert posted.status_code == 200, posted.text
    assert posted.json()["transaction_id"]
    detail = await ledger_client.get(f"/api/v1/inventory-adjustments/{document['id']}", headers=world["headers"])
    assert detail.json()["status"] == "POSTED"


async def test_transfer_moves_stock_between_locations(ledger_client, world, db_session) -> None:
    from app.tests.integration.inventory_ledger.conftest import _insert_location

    destination = await _insert_location(db_session, world["tenant_id"], code=f"LED-DEST-{uuid.uuid4().hex[:5]}")
    seeded = await _post(ledger_client, world, [_movement(world, "12")], key="trf-seed")
    assert seeded.status_code == 201, seeded.text
    transfer = await ledger_client.post(
        "/api/v1/inventory-transfers",
        json={"transaction_date": "2026-10-23", "from_location_id": str(world["location_id"]), "to_location_id": str(destination),
              "lines": [{"item_id": str(world["item_id"]), "qty": "5", "inventory_status_id": str(world["status_id"])}]},
        headers=world["headers"] | {"Idempotency-Key": "trf-1"},
    )
    assert transfer.status_code == 201, transfer.text
    posted = await ledger_client.post(f"/api/v1/inventory-transfers/{transfer.json()['id']}/post", headers=world["headers"] | {"Idempotency-Key": "trf-post"})
    assert posted.status_code == 200, posted.text
    source = await ledger_client.get("/api/v1/inventory/availability", headers=world["headers"], params={"item_id": str(world["item_id"]), "location_id": str(world["location_id"])})
    dest = await ledger_client.get("/api/v1/inventory/availability", headers=world["headers"], params={"item_id": str(world["item_id"]), "location_id": str(destination)})
    assert Decimal(source.json()["on_hand"]) == Decimal("7")
    assert Decimal(dest.json()["on_hand"]) == Decimal("5")


async def test_commitment_reduces_availability_and_release_restores(ledger_client, world) -> None:
    await _post(ledger_client, world, [_movement(world, "10")], key="commit-seed")
    committed = await ledger_client.post(
        "/api/v1/inventory/commitments",
        json={"source_type": "SALES_ORDER", "source_line_ref": "SO-1", "item_id": str(world["item_id"]),
              "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]), "qty": "4"},
        headers=world["headers"] | {"Idempotency-Key": "commit-1"},
    )
    assert committed.status_code == 201, committed.text
    commitment_id = committed.json()["commitment_id"]
    availability = await ledger_client.get("/api/v1/inventory/availability", headers=world["headers"], params={"item_id": str(world["item_id"]), "location_id": str(world["location_id"])})
    assert Decimal(availability.json()["available"]) == Decimal("6")
    released = await ledger_client.post(f"/api/v1/inventory/commitments/{commitment_id}/release", json={"qty": "4"}, headers=world["headers"] | {"Idempotency-Key": "release-1"})
    assert released.status_code == 200, released.text
    availability = await ledger_client.get("/api/v1/inventory/availability", headers=world["headers"], params={"item_id": str(world["item_id"]), "location_id": str(world["location_id"])})
    assert Decimal(availability.json()["available"]) == Decimal("10")


async def test_tenant_isolation(ledger_client, world, db_session) -> None:
    other = await seed_world(db_session, code_prefix="ALT")
    posted = (await _post(ledger_client, world, [_movement(world, "1")], key="iso-1")).json()
    cross = await ledger_client.get(f"/api/v1/inventory-transactions/{posted['transaction_id']}", headers=other["headers"])
    assert cross.status_code == 404
    other_balances = await ledger_client.get("/api/v1/inventory/balances", headers=other["headers"])
    assert other_balances.status_code == 200 and other_balances.json()["items"] == []
