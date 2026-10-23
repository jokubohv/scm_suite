"""FS-09 ATB contract pass-through: `available_to_build.calculated` outbox emission (Δ residual V-21).

The FS-15 V-21 reporting projection consumes the FS-09 atb-v1 result from `platform.outbox_event`; the
manufacturing service therefore emits exactly one transactional `available_to_build.calculated` event per
calculation, with the authoritative result fields and a deterministic `input_hash`.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

TODAY = date.today().isoformat()


async def _active_bom(client, world, *, code: str = "ATB-BOM") -> dict[str, Any]:
    headers = world["headers"]
    bom = await client.post(
        "/api/v1/boms",
        json={"assembly_item_id": str(world["assembly_item_id"]), "bom_code": code, "name": code},
        headers=headers,
    )
    assert bom.status_code == 201, bom.text
    bom_id = bom.json()["bom"]["id"]
    await client.post(f"/api/v1/boms/{bom_id}/activate", headers=headers)
    revision = await client.post(
        f"/api/v1/boms/{bom_id}/revisions",
        json={"revision_code": "R1", "effective_from": "2020-01-01"},
        headers=headers,
    )
    assert revision.status_code == 201, revision.text
    revision_id = revision.json()["revision"]["id"]
    for line_number, item_id, qty_per in (
        (1, world["component_item_id"], "2"),
        (2, world["other_item_id"], "1"),
    ):
        component = await client.post(
            f"/api/v1/bom-revisions/{revision_id}/components",
            json={
                "line_number": line_number,
                "component_item_id": str(item_id),
                "qty_per": qty_per,
                "uom_id": str(world["uom_id"]),
            },
            headers=headers,
        )
        assert component.status_code == 201, component.text
    activated = await client.post(f"/api/v1/bom-revisions/{revision_id}/activate", headers=headers)
    assert activated.status_code == 200, activated.text
    return {"bom_id": bom_id, "revision_id": revision_id}


async def _outbox_events(db_session: Any, tenant_id: Any) -> list[dict[str, Any]]:
    rows = await db_session.execute(
        text(
            "SELECT id, event_type, event_version, aggregate_type, aggregate_id, payload, occurred_at "
            "FROM platform.outbox_event WHERE tenant_id = :tenant AND event_type = 'available_to_build.calculated' "
            "ORDER BY occurred_at"
        ),
        {"tenant": tenant_id},
    )
    events = []
    for row in rows.all():
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        events.append(
            {
                "id": str(row.id),
                "event_type": row.event_type,
                "event_version": row.event_version,
                "aggregate_type": row.aggregate_type,
                "aggregate_id": str(row.aggregate_id),
                "payload": payload,
                "data": payload["data"],
                "occurred_at": row.occurred_at,
            }
        )
    return events


async def test_available_to_build_emits_exactly_one_pass_through_event(client, world, db_session) -> None:
    await _active_bom(client, world)
    location = str(world["location_id"])
    world["balances"].set(str(world["component_item_id"]), location, on_hand=50, available=50)
    world["balances"].set(str(world["other_item_id"]), location, on_hand=10, available=10)
    world["balances"].set(str(world["assembly_item_master_id"]), location, on_hand=0, available=0)

    response = await client.get(
        "/api/v1/available-to-build",
        params={"item_id": str(world["assembly_item_id"]), "location_id": location},
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["buildable_qty"] == "10", body["components"]  # min(50//2, 10//1)
    assert body["policy_version"] == "atb-v1"
    assert body["input_hash"]

    events = await _outbox_events(db_session, world["tenant_id"])
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "available_to_build.calculated"
    assert event["event_version"] == "1.0.0"
    assert event["aggregate_type"] == "ASSEMBLY_ITEM"
    assert event["aggregate_id"] == str(world["assembly_item_id"])
    data = event["data"]
    assert data["assembly_item_id"] == str(world["assembly_item_id"])
    assert data["item_id"] == str(world["assembly_item_master_id"])
    assert data["location_id"] == location
    assert data["level_mode"] == "ALL_LEVELS"
    assert data["basis"] == "ON_HAND"
    assert data["as_of"] == TODAY
    assert data["buildable_qty"] == "10"
    assert data["policy_version"] == "atb-v1"
    assert data["input_hash"] == body["input_hash"]
    assert data["degraded"] is False
    assert data["per_level"] and data["per_level"][0]["level"] == 1
    assert data["limiting_component_id"] == str(world["other_item_id"])
    assert data["qty_per_component"] == "1.000000000000"
    assert data["available_component_qty"] == "10"

    # One event per calculation (no duplicate emission for a single GET).
    second = await client.get(
        "/api/v1/available-to-build",
        params={"item_id": str(world["assembly_item_id"]), "location_id": location},
        headers=world["headers"],
    )
    assert second.status_code == 200
    events = await _outbox_events(db_session, world["tenant_id"])
    assert len(events) == 2
    assert events[0]["data"]["input_hash"] == events[1]["data"]["input_hash"]  # deterministic inputs
    assert events[1]["occurred_at"] >= events[0]["occurred_at"]


async def test_emitted_event_matches_fs09_contract_schema(client, world, db_session) -> None:
    from jsonschema import validate

    from app.contracts.manufacturing.build_from_app import HERE

    await _active_bom(client, world, code="ATB-SCHEMA")
    world["balances"].set(str(world["component_item_id"]), str(world["location_id"]), on_hand=4, available=4)
    world["balances"].set(str(world["other_item_id"]), str(world["location_id"]), on_hand=4, available=4)
    response = await client.get(
        "/api/v1/available-to-build",
        params={"item_id": str(world["assembly_item_id"])},
        headers=world["headers"],
    )
    assert response.status_code == 200, response.text
    events = await _outbox_events(db_session, world["tenant_id"])
    assert len(events) == 1
    schema = json.loads((HERE / "events" / "available_to_build.calculated.schema.json").read_text())
    validate(instance=events[0]["payload"], schema=schema)


async def test_batch_calculate_emits_one_event_per_item(client, world, db_session) -> None:
    await _active_bom(client, world, code="ATB-BATCH")
    location = str(world["location_id"])
    world["balances"].set(str(world["component_item_id"]), location, on_hand=6, available=6)
    world["balances"].set(str(world["other_item_id"]), location, on_hand=6, available=6)
    batch = await client.post(
        "/api/v1/available-to-build/calculate",
        json={"items": [{"item_id": str(world["assembly_item_id"])}], "location_id": location},
        headers=world["headers"],
    )
    assert batch.status_code == 200, batch.text
    assert len(batch.json()["results"]) == 1
    events = await _outbox_events(db_session, world["tenant_id"])
    assert len(events) == 1
    assert events[0]["data"]["buildable_qty"] == "3"  # min(6//2, 6//1)
