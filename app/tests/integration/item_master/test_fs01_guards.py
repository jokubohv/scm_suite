"""FS-01 residual guards: type migration, UoM deactivation, matrix in-use, custom fields, planning idempotency."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.models import platform as p
from app.modules.traceability.tables import location as trace_location

pytestmark = pytest.mark.integration


async def _create_item(client, ctx, **overrides):
    payload = {
        "item_number": f"GUARD-{uuid.uuid4().hex[:10].upper()}",
        "display_name": "Guard Item",
        "item_type_code": "INVENTORY",
        "inventory": {"costing_method": "AVERAGE", "initial_cost": "10"},
    }
    payload.update(overrides)
    number = payload["item_number"]
    return await client.post("/api/v1/items", json=payload, headers=ctx["headers"] | {"Idempotency-Key": number})


async def test_type_migration_creates_inventory_item_and_inactivates_source(item_client, tenant_ctx, db_session) -> None:
    source = await _create_item(item_client, tenant_ctx, item_type_code="NON_INVENTORY")
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]
    migrated = await item_client.post(
        f"/api/v1/items/{source_id}/convert-to-inventory",
        json={"item_number": f"CONV-{uuid.uuid4().hex[:8].upper()}", "reason": "make stockable"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": f"conv-{source_id}"},
    )
    assert migrated.status_code == 201, migrated.text
    body = migrated.json()
    assert body["source_item_id"] == source_id and body["converted"] is False
    target = await item_client.get(f"/api/v1/items/{body['new_item_id']}", headers=tenant_ctx["headers"])
    assert target.status_code == 200 and target.json()["item_type_code"] == "INVENTORY"
    source_after = await item_client.get(f"/api/v1/items/{source_id}", headers=tenant_ctx["headers"])
    assert source_after.json()["is_active"] is False
    link = (await db_session.execute(text(
        "SELECT count(*) FROM platform.document_link WHERE tenant_id = :tenant AND link_type = 'TYPE_MIGRATION' AND from_id = :source"
    ), {"tenant": tenant_ctx["tenant_id"], "source": uuid.UUID(source_id)})).scalar_one()
    assert int(link) == 1
    retry = await item_client.post(
        f"/api/v1/items/{source_id}/convert-to-inventory",
        json={"item_number": f"CONV2-{uuid.uuid4().hex[:8].upper()}"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": f"conv-retry-{source_id}"},
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["new_item_id"] == body["new_item_id"] and retry.json()["converted"] is True


async def test_uom_deactivation_is_guarded_when_referenced(item_client, tenant_ctx) -> None:
    item = await _create_item(item_client, tenant_ctx)
    assert item.status_code == 201, item.text
    uom_id = item.json()["base_uom_id"]
    blocked = await item_client.patch(f"/api/v1/uoms/{uom_id}", json={"is_active": False}, headers=tenant_ctx["headers"])
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "UOM_IN_USE"
    created = await item_client.post(
        "/api/v1/uoms",
        json={"uom_type_id": item.json()["units_type_id"], "code": f"X{uuid.uuid4().hex[:6].upper()}", "name": "Spare"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert created.status_code == 201, created.text
    deactivated = await item_client.patch(f"/api/v1/uoms/{created.json()['id']}", json={"is_active": False}, headers=tenant_ctx["headers"])
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False


async def test_matrix_option_value_in_use_guard(item_client, tenant_ctx) -> None:
    parent = await _create_item(item_client, tenant_ctx, item_type_code="MATRIX_PARENT", inventory=None)
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    option = await item_client.post(
        f"/api/v1/items/{parent_id}/matrix-options",
        json={"code": "COLOR", "name": "Color", "values": [{"code": "RED", "value": "Red"}, {"code": "BLUE", "value": "Blue"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert option.status_code == 201, option.text
    option_id = option.json()["id"]
    generated = await item_client.post(
        f"/api/v1/items/{parent_id}/matrix-children/generate",
        json={"combinations": [{"item_number": f"CHILD-{uuid.uuid4().hex[:8].upper()}", "option_values": {"COLOR": "RED"}}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert generated.status_code == 201, generated.text
    blocked = await item_client.put(
        f"/api/v1/items/{parent_id}/matrix-options/{option_id}/values",
        json={"values": [{"code": "BLUE", "value": "Blue"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["code"] == "MATRIX_OPTION_IN_USE"
    allowed = await item_client.put(
        f"/api/v1/items/{parent_id}/matrix-options/{option_id}/values",
        json={"values": [{"code": "RED", "value": "Red"}, {"code": "GREEN", "value": "Green"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert allowed.status_code == 200, allowed.text


async def test_custom_field_eav_wiring_and_required_activation_guard(item_client, tenant_ctx, db_session) -> None:
    await db_session.execute(p.custom_field_def.insert().values(
        id=uuid.uuid4(), tenant_id=tenant_ctx["tenant_id"], entity_type="ITEM", field_code="WARRANTY_CODE",
        label="Warranty Code", data_type="TEXT", is_required=False, is_searchable=True,
    ))
    created = await _create_item(item_client, tenant_ctx, custom_fields={"WARRANTY_CODE": "W-12"})
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    fetched = await item_client.get(f"/api/v1/items/{item_id}/custom-fields", headers=tenant_ctx["headers"])
    assert fetched.status_code == 200, fetched.text
    values = {row["field_code"]: row["value"] for row in fetched.json()["items"]}
    assert values["WARRANTY_CODE"] == "W-12"
    updated = await item_client.put(
        f"/api/v1/items/{item_id}/custom-fields",
        json={"values": {"WARRANTY_CODE": "W-24"}},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert updated.status_code == 200, updated.text
    assert {row["field_code"]: row["value"] for row in updated.json()["items"]}["WARRANTY_CODE"] == "W-24"
    invalid = await item_client.put(
        f"/api/v1/items/{item_id}/custom-fields",
        json={"values": {"UNKNOWN_FIELD": "x"}},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "CUSTOM_FIELD_UNKNOWN"

    await db_session.execute(p.custom_field_def.insert().values(
        id=uuid.uuid4(), tenant_id=tenant_ctx["tenant_id"], entity_type="ITEM", field_code="SERIAL_REQUIRED",
        label="Required Field", data_type="TEXT", is_required=True,
    ))
    missing = await _create_item(item_client, tenant_ctx)
    assert missing.status_code == 422, missing.text
    assert missing.json()["code"] == "CUSTOM_FIELD_REQUIRED"


async def test_planning_values_idempotent_replay(item_client, tenant_ctx, db_session) -> None:
    created = await _create_item(item_client, tenant_ctx)
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    headers = tenant_ctx["headers"] | {"Idempotency-Key": "planning-same-key"}
    first = await item_client.post(
        f"/api/v1/items/{item_id}/planning-values",
        json={"reorder_point": "5", "safety_stock": "2", "source": "MANUAL"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    replay = await item_client.post(
        f"/api/v1/items/{item_id}/planning-values",
        json={"reorder_point": "5", "safety_stock": "2", "source": "MANUAL"},
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["version"] == first.json()["version"]
    missing_key = await item_client.post(
        f"/api/v1/items/{item_id}/planning-values",
        json={"reorder_point": "7"},
        headers=tenant_ctx["headers"],
    )
    assert missing_key.status_code == 400


async def test_guards_are_tenant_isolated(item_client, tenant_ctx, second_tenant_ctx) -> None:
    created = await _create_item(item_client, tenant_ctx)
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    other = second_tenant_ctx["headers"]
    assert (await item_client.get(f"/api/v1/items/{item_id}/custom-fields", headers=other)).status_code == 404
    converted = await item_client.post(
        f"/api/v1/items/{item_id}/convert-to-inventory",
        json={"item_number": f"OTHER-{uuid.uuid4().hex[:6]}"},
        headers=other | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert converted.status_code == 404
    patched = await item_client.patch(f"/api/v1/uoms/{created.json()['base_uom_id']}", json={"is_active": False}, headers=other)
    assert patched.status_code == 404
    type_blocked = await item_client.patch(
        f"/api/v1/uom-types/{created.json()['units_type_id']}", json={"is_active": False}, headers=tenant_ctx["headers"]
    )
    assert type_blocked.status_code == 409 and type_blocked.json()["code"] == "UOM_IN_USE"


async def test_item_location_data_scope_filtering(item_client, tenant_ctx, db_session) -> None:
    created = await _create_item(item_client, tenant_ctx)
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]
    allowed_location, other_location = uuid.uuid4(), uuid.uuid4()
    for location_uuid in (allowed_location, other_location):
        await db_session.execute(trace_location.insert().values(
            id=location_uuid, tenant_id=tenant_ctx["tenant_id"], code=f"LOC-{str(location_uuid)[:8]}",
            name="Loc", location_type="WAREHOUSE", is_bins_enabled=False, is_statuses_enabled=False,
            is_consignment_allowed=False, allow_commit_from_non_available=False, timezone="UTC",
            is_unallocated=False, is_inactive=False,
        ))
    put = await item_client.put(
        f"/api/v1/items/{item_id}/locations",
        json={"locations": [{"location_id": str(allowed_location)}, {"location_id": str(other_location)}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": uuid.uuid4().hex},
    )
    assert put.status_code == 200, put.text
    await db_session.execute(p.user_data_scope.insert().values(
        id=uuid.uuid4(), tenant_id=tenant_ctx["tenant_id"], user_id=tenant_ctx["app_user_id"],
        scope_type="LOCATION", scope_value=str(allowed_location), include_descendants=False,
    ))
    listed = await item_client.get(f"/api/v1/items/{item_id}/locations", headers=tenant_ctx["headers"])
    assert listed.status_code == 200, listed.text
    assert [row["location_id"] for row in listed.json()["items"]] == [str(allowed_location)]
    permitted = await item_client.get(
        f"/api/v1/items/{item_id}/locations", params={"location_id": str(allowed_location)}, headers=tenant_ctx["headers"]
    )
    assert permitted.status_code == 200 and len(permitted.json()["items"]) == 1
    denied = await item_client.get(
        f"/api/v1/items/{item_id}/locations", params={"location_id": str(other_location)}, headers=tenant_ctx["headers"]
    )
    assert denied.status_code == 404
