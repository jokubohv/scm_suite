"""Residual contract routes: pre-post document PATCH + distribution approve (FS-04 §6 rows)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


def _headers(world: dict, key: str | None = None) -> dict:
    headers = dict(world["headers"])
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def _post(client, world, movements, *, key: str, posting_type: str = "T-07", **extra):
    response = await client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": posting_type, "transaction_date": "2026-10-23", "movements": movements, **extra},
        headers=_headers(world, key),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _movement(world: dict, qty: str, **extra) -> dict:
    return {
        "item_id": str(world["item_id"]),
        "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]),
        "qty": qty,
        "movement_type": "ADJUSTMENT",
        **extra,
    }


async def _create_adjustment(client, world, *, key: str, memo: str = "initial") -> dict:
    response = await client.post(
        "/api/v1/inventory-adjustments",
        json={
            "transaction_date": "2026-10-23",
            "memo": memo,
            "lines": [
                {
                    "item_id": str(world["item_id"]),
                    "location_id": str(world["location_id"]),
                    "inventory_status_id": str(world["status_id"]),
                    "qty_delta": "4",
                    "unit_cost": "2.5",
                }
            ],
        },
        headers=_headers(world, key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_patch_adjustment_header_lines_and_guards(ledger_client, world) -> None:
    document = await _create_adjustment(ledger_client, world, key="patch-adj-1")
    assert document["status"] == "DRAFT"

    patched = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{document['id']}",
        json={
            "memo": "edited during draft",
            "transaction_date": "2026-10-24",
            "lines": [
                {
                    "item_id": str(world["item_id"]),
                    "location_id": str(world["location_id"]),
                    "inventory_status_id": str(world["status_id"]),
                    "qty_delta": "6",
                    "unit_cost": "3",
                }
            ],
        },
        headers=world["headers"] | {"If-Match": str(document["version"])},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["memo"] == "edited during draft"
    assert Decimal(body["lines"][0]["qty_delta"]) == Decimal("6")
    assert body["version"] == document["version"] + 1

    stale = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{document['id']}",
        json={"memo": "stale edit"},
        headers=world["headers"] | {"If-Match": str(document["version"])},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "STALE_VERSION"

    invalid = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{document['id']}",
        json={"memo": "bad field", "status": "POSTED"},
        headers=world["headers"],
    )
    assert invalid.status_code == 200  # unknown fields are ignored, whitelist-only patching
    assert invalid.json()["status"] == "DRAFT"

    submitted = await ledger_client.post(
        f"/api/v1/inventory-adjustments/{document['id']}/submit", headers=_headers(world, "patch-adj-submit")
    )
    assert submitted.status_code == 200
    lines_locked = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{document['id']}",
        json={
            "lines": [{"item_id": str(world["item_id"]), "location_id": str(world["location_id"]), "qty_delta": "1"}]
        },
        headers=world["headers"],
    )
    assert lines_locked.status_code == 409

    posted = await ledger_client.post(
        f"/api/v1/inventory-adjustments/{document['id']}/post", headers=_headers(world, "patch-adj-post")
    )
    assert posted.status_code == 200, posted.text
    locked = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{document['id']}", json={"memo": "too late"}, headers=world["headers"]
    )
    assert locked.status_code == 409
    assert locked.json()["code"] == "INVALID_STATE_TRANSITION"


async def test_patch_worksheets_item_and_collection(ledger_client, world) -> None:
    created = []
    for index in range(2):
        response = await ledger_client.post(
            "/api/v1/inventory-worksheets",
            json={
                "transaction_date": "2026-10-23",
                "lines": [
                    {
                        "item_id": str(world["item_id"]),
                        "location_id": str(world["location_id"]),
                        "inventory_status_id": str(world["status_id"]),
                        "new_qty": "5",
                    }
                ],
            },
            headers=_headers(world, f"patch-ws-{index}"),
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    patched = await ledger_client.patch(
        f"/api/v1/inventory-worksheets/{created[0]['id']}",
        json={"memo": "single edit", "count_date": "2026-10-22"},
        headers=world["headers"] | {"If-Match": str(created[0]["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["memo"] == "single edit"

    bulk = await ledger_client.patch(
        "/api/v1/inventory-worksheets",
        json={"ids": [created[1]["id"]], "patch": {"memo": "bulk edit"}},
        headers=world["headers"],
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["updated"] == 1
    assert bulk.json()["items"][0]["memo"] == "bulk edit"

    empty = await ledger_client.patch(
        "/api/v1/inventory-worksheets", json={"ids": [], "patch": {"memo": "x"}}, headers=world["headers"]
    )
    assert empty.status_code == 422


async def test_patch_transfers_item_and_collection(ledger_client, world, db_session) -> None:
    from app.tests.integration.inventory_ledger.conftest import _insert_location

    destination = await _insert_location(db_session, world["tenant_id"], code=f"LED-PATCH-{uuid.uuid4().hex[:5]}")
    created = []
    for index in range(2):
        response = await ledger_client.post(
            "/api/v1/inventory-transfers",
            json={
                "transaction_date": "2026-10-23",
                "from_location_id": str(world["location_id"]),
                "to_location_id": str(destination),
                "lines": [
                    {"item_id": str(world["item_id"]), "qty": "2", "inventory_status_id": str(world["status_id"])}
                ],
            },
            headers=_headers(world, f"patch-trf-{index}"),
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    patched = await ledger_client.patch(
        f"/api/v1/inventory-transfers/{created[0]['id']}",
        json={"memo": "header edit"},
        headers=world["headers"] | {"If-Match": str(created[0]["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["memo"] == "header edit"

    bulk = await ledger_client.patch(
        "/api/v1/inventory-transfers",
        json={"ids": [created[1]["id"]], "patch": {"memo": "bulk transfer edit"}},
        headers=world["headers"],
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["updated"] == 1


async def test_patch_transfer_orders_draft_only(ledger_client, world, db_session) -> None:
    from app.tests.integration.inventory_ledger.conftest import _insert_location

    destination = await _insert_location(db_session, world["tenant_id"], code=f"LED-TO-{uuid.uuid4().hex[:5]}")
    created = await ledger_client.post(
        "/api/v1/transfer-orders",
        json={
            "transaction_date": "2026-10-23",
            "from_location_id": str(world["location_id"]),
            "to_location_id": str(destination),
            "in_transit_ownership": "SOURCE",
            "lines": [{"item_id": str(world["item_id"]), "qty_requested": "3"}],
        },
        headers=_headers(world, "patch-to-1"),
    )
    assert created.status_code == 201, created.text
    order = created.json()

    patched = await ledger_client.patch(
        f"/api/v1/transfer-orders/{order['id']}",
        json={"memo": "TO draft edit", "transfer_price": "7.25"},
        headers=world["headers"] | {"If-Match": str(order["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["memo"] == "TO draft edit"
    assert Decimal(patched.json()["transfer_price"]) == Decimal("7.25")

    bulk = await ledger_client.patch(
        "/api/v1/transfer-orders",
        json={"ids": [order["id"]], "patch": {"memo": "bulk TO edit"}},
        headers=world["headers"],
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["items"][0]["memo"] == "bulk TO edit"

    submitted = await ledger_client.post(
        f"/api/v1/transfer-orders/{order['id']}/submit", headers=_headers(world, "patch-to-submit")
    )
    assert submitted.status_code == 200
    locked = await ledger_client.patch(
        f"/api/v1/transfer-orders/{order['id']}", json={"memo": "after submit"}, headers=world["headers"]
    )
    assert locked.status_code == 409


async def test_approve_distribution_then_post(ledger_client, world, db_session) -> None:
    from sqlalchemy import text

    sentinel_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO traceability.location (id, tenant_id, code, name, location_type, is_bins_enabled, "
            "is_statuses_enabled, is_consignment_allowed, allow_commit_from_non_available, timezone, "
            "is_unallocated, is_inactive, version) "
            "VALUES (:id, :tenant, 'UNALLOCATED', 'Unallocated', 'UNALLOCATED', false, true, false, false, "
            "'UTC', true, false, 1)"
        ),
        {"id": sentinel_id, "tenant": world["tenant_id"]},
    )
    await db_session.flush()
    seeded = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={
            "posting_type": "T-07",
            "transaction_date": "2026-10-23",
            "allow_unallocated": True,
            "movements": [
                {
                    "item_id": str(world["item_id"]),
                    "location_id": str(sentinel_id),
                    "inventory_status_id": str(world["status_id"]),
                    "qty": "5",
                    "movement_type": "ADJUSTMENT",
                }
            ],
        },
        headers=_headers(world, "dist-seed-sentinel"),
    )
    assert seeded.status_code == 201, seeded.text
    created = await ledger_client.post(
        "/api/v1/inventory/distributions",
        json={
            "transaction_date": "2026-10-23",
            "distribution_mode": "SIMPLE",
            "lines": [
                {
                    "item_id": str(world["item_id"]),
                    "to_location_id": str(world["location_id"]),
                    "inventory_status_id": str(world["status_id"]),
                    "qty": "5",
                }
            ],
        },
        headers=_headers(world, "dist-res-1"),
    )
    assert created.status_code == 201, created.text
    distribution = created.json()
    assert distribution["status"] == "DRAFT"

    approved = await ledger_client.post(
        f"/api/v1/inventory/distributions/{distribution['id']}/approve", headers=_headers(world, "dist-approve-1")
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    approved_version = approved.json()["version"]

    idempotent = await ledger_client.post(
        f"/api/v1/inventory/distributions/{distribution['id']}/approve", headers=_headers(world, "dist-approve-2")
    )
    assert idempotent.status_code == 200
    assert idempotent.json()["status"] == "APPROVED"
    assert idempotent.json()["version"] == approved_version

    posted = await ledger_client.post(
        f"/api/v1/inventory/distributions/{distribution['id']}/post", headers=_headers(world, "dist-post-1")
    )
    assert posted.status_code == 200, posted.text
    late = await ledger_client.post(
        f"/api/v1/inventory/distributions/{distribution['id']}/approve", headers=_headers(world, "dist-approve-3")
    )
    assert late.status_code == 409
    assert late.json()["code"] == "INVALID_STATE_TRANSITION"


async def test_patch_and_approve_scope_and_tenant_isolation(ledger_client, world, db_session) -> None:
    from app.api.deps import token_service
    from app.tests.integration.inventory_ledger.conftest import seed_world

    adjustment = await _create_adjustment(ledger_client, world, key="scope-adj-1")
    read_only, _ = token_service().issue_access_token(
        identity_id=str(world["identity_id"]),
        app_user_id=str(world["app_user_id"]),
        tenant_id=str(world["tenant_id"]),
        token_version=0,
        scopes=frozenset({"inventory.transaction.read"}),
    )
    forbidden = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{adjustment['id']}",
        json={"memo": "should not apply"},
        headers={"Authorization": f"Bearer {read_only}"},
    )
    assert forbidden.status_code == 403

    other = await seed_world(db_session, code_prefix="ALT")
    cross_tenant = await ledger_client.patch(
        f"/api/v1/inventory-adjustments/{adjustment['id']}",
        json={"memo": "cross tenant edit"},
        headers=other["headers"],
    )
    assert cross_tenant.status_code == 404
