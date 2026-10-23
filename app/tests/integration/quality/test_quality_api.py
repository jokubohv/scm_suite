"""FS-12 quality-management API integration tests (live Postgres; graceful skip when unavailable)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.modules.inventory_ledger import tables as ledger
from app.tests.integration.quality.conftest import (
    create_active_spec,
    create_inspection,
    create_plan,
    idem,
    record_and_complete,
    spec_payload,
)

pytestmark = pytest.mark.integration


async def test_spec_and_plan_lifecycle(client: Any, seeded: dict[str, Any]) -> None:
    spec = await create_active_spec(client, seeded["item_id"])
    assert spec["status"] == "ACTIVE"
    assert spec["spec_version"] == 1
    assert len(spec["lines"]) == 3

    listed = await client.get("/api/v1/quality/specs", params={"item_id": str(seeded["item_id"])})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [spec["id"]]

    plan = await create_plan(client, spec["id"], seeded["item_id"])
    assert plan["quality_spec_id"] == spec["id"]
    assert len(plan["lines"]) == 3

    duplicate_code = await client.post(
        "/api/v1/quality/specs", json=spec_payload(seeded["item_id"], code="SPEC-1"), headers=idem()
    )
    assert duplicate_code.status_code == 201
    assert duplicate_code.json()["spec"]["spec_version"] == 2

    retire = await client.post(
        f"/api/v1/quality/specs/{spec['id']}/retire", json={"reason": "superseded"}, headers=idem()
    )
    assert retire.status_code == 422  # active plan references it (C-06 default)
    assert retire.json()["code"] == "VALIDATION_FAILED"

    limit = spec_payload(seeded["item_id"], code="SPEC-LIMIT")
    limit["lines"] = [{"characteristic": f"c{i}", "result_type": "TEXT"} for i in range(36)]
    too_many = await client.post("/api/v1/quality/specs", json=limit, headers=idem())
    assert too_many.status_code == 422
    assert too_many.json()["code"] == "SPEC_LINE_LIMIT"


async def test_trigger_once_and_inspection_flow(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    spec = await create_active_spec(client, seeded["item_id"])
    plan = await create_plan(client, spec["id"], seeded["item_id"])
    trigger = await client.post(
        "/api/v1/quality/inspection-triggers",
        json={
            "code": "RCV-1",
            "name": "Receipt inspection",
            "trigger_event": "ITEM_RECEIPT",
            "inspection_plan_id": plan["id"],
            "condition": {"frequency": 1},
        },
        headers=idem(),
    )
    assert trigger.status_code == 201, trigger.text
    trigger_id = trigger.json()["trigger"]["id"]
    source = str(uuid4())
    event = {
        "source_entity_type": "ITEM_RECEIPT",
        "source_entity_id": source,
        "item_id": str(seeded["item_id"]),
        "location_id": str(seeded["location_id"]),
    }
    first = await client.post(f"/api/v1/quality/inspection-triggers/{trigger_id}/evaluate", json=event, headers=idem())
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    second = await client.post(f"/api/v1/quality/inspection-triggers/{trigger_id}/evaluate", json=event, headers=idem())
    assert second.status_code == 200
    assert second.json()["created"] is False and second.json()["deduplicated"] is True
    inspection = first.json()["inspection"]
    assert inspection["status"] == "PENDING"
    assert inspection["inspection_number"].startswith("INSP-")

    listed = await client.get("/api/v1/quality/inspections", params={"status": "PENDING"})
    assert listed.json()["items"][0]["id"] == inspection["id"]

    passed = await record_and_complete(client, seeded, inspection["id"])
    assert passed["status"] == "PASSED"
    assert passed["disposition"] == "ACCEPT"
    assert passed["result_summary"]["rows"] == 2


async def test_failed_inspection_hold_and_release(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    await db_session.execute(
        insert(ledger.inventory_balance).values(
            id=uuid4(),
            tenant_id=seeded["tenant_id"],
            item_id=seeded["item_id"],
            location_id=seeded["location_id"],
            inventory_status_id=seeded["available_status"],
            lot_id=None,
            serial_id=None,
            ownership="OWNED",
            qty_on_hand=Decimal("5"),
            qty_committed=Decimal("0"),
            qty_on_order=Decimal("0"),
            value_base=Decimal("0"),
        )
    )
    spec = await create_active_spec(client, seeded["item_id"])
    plan = await create_plan(client, spec["id"], seeded["item_id"])
    inspection = await create_inspection(client, seeded, plan["id"])
    failed = await record_and_complete(client, seeded, inspection["id"], fail=True, disposition="HOLD")
    assert failed["status"] == "FAILED" and failed["disposition"] == "HOLD"

    positions = [
        {
            "item_id": str(seeded["item_id"]),
            "location_id": str(seeded["location_id"]),
            "inventory_status_id": str(seeded["available_status"]),
            "qty": "2",
        }
    ]
    held = await client.post(
        f"/api/v1/quality/inspections/{inspection['id']}/hold", json={"positions": positions}, headers=idem()
    )
    assert held.status_code == 200, held.text
    assert held.json()["inventory_transaction_id"]

    balances = (
        (
            await db_session.execute(
                ledger.inventory_balance.select().where(ledger.inventory_balance.c.tenant_id == seeded["tenant_id"])
            )
        )
        .mappings()
        .all()
    )
    by_status = {row["inventory_status_id"]: Decimal(row["qty_on_hand"]) for row in balances}
    assert by_status[seeded["available_status"]] == Decimal("3")
    assert by_status[seeded["hold_status"]] == Decimal("2")

    waived = await client.post(
        f"/api/v1/quality/inspections/{inspection['id']}/waive",
        json={"reason": "customer concession", "approval_ref": "APPROVAL-1"},
        headers=idem(),
    )
    assert waived.status_code == 200, waived.text
    release_positions = [
        {
            "item_id": str(seeded["item_id"]),
            "location_id": str(seeded["location_id"]),
            "inventory_status_id": str(seeded["hold_status"]),
            "qty": "2",
        }
    ]
    released = await client.post(
        f"/api/v1/quality/inspections/{inspection['id']}/release-hold",
        json={"positions": release_positions},
        headers=idem(),
    )
    assert released.status_code == 200, released.text
    balances = (
        (
            await db_session.execute(
                ledger.inventory_balance.select().where(ledger.inventory_balance.c.tenant_id == seeded["tenant_id"])
            )
        )
        .mappings()
        .all()
    )
    by_status = {row["inventory_status_id"]: Decimal(row["qty_on_hand"]) for row in balances}
    assert by_status[seeded["available_status"]] == Decimal("5")


async def test_coa_lifecycle_excludes_internal_lines(client: Any, seeded: dict[str, Any]) -> None:
    spec = await create_active_spec(client, seeded["item_id"])
    plan = await create_plan(client, spec["id"], seeded["item_id"])
    inspection = await create_inspection(client, seeded, plan["id"])
    await record_and_complete(client, seeded, inspection["id"])
    created = await client.post("/api/v1/quality/coas", json={"inspection_id": inspection["id"]}, headers=idem())
    assert created.status_code == 201, created.text
    coa = created.json()["coa"]
    assert coa["status"] == "DRAFT"
    assert [line["characteristic"] for line in coa["lines"]] == ["length", "visual"]
    issued = await client.post(f"/api/v1/quality/coas/{coa['id']}/issue", json={}, headers=idem())
    assert issued.status_code == 200 and issued.json()["coa"]["status"] == "ISSUED"
    revoked = await client.post(
        f"/api/v1/quality/coas/{coa['id']}/revoke", json={"reason": "data error"}, headers=idem()
    )
    assert revoked.status_code == 200 and revoked.json()["coa"]["status"] == "REVOKED"


async def test_settings_require_non_committable_hold_status(client: Any, seeded: dict[str, Any]) -> None:
    ok = await client.patch(
        "/api/v1/quality/settings", json={"default_queue_code": "DEFAULT", "hold_status_code": "HOLD"}
    )
    assert ok.status_code == 200, ok.text
    invalid = await client.patch("/api/v1/quality/settings", json={"hold_status_code": "AVAILABLE"})
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "QUALITY_HOLD_STATUS_INVALID"
    settings = await client.get("/api/v1/quality/settings")
    assert settings.status_code == 200
    assert settings.json()["settings"]["hold_status_code"] == "HOLD"
