"""FS-01 item-master integration tests (live DB): taxonomy, CRUD/extensions, UoM, matrix, codes, isolation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.item_master.taxonomy import ITEM_TYPE_SEEDS

pytestmark = pytest.mark.integration


async def _create_item(client, ctx, **overrides):
    payload = {
        "item_number": f"ITEM-{overrides.pop('suffix', 'A')}",
        "display_name": "Test Item",
        "item_type_code": "INVENTORY",
        "inventory": {"costing_method": "AVERAGE", "initial_cost": "12.5"},
    }
    payload.update(overrides)
    return await client.post("/api/v1/items", json=payload, headers=ctx["headers"] | {"Idempotency-Key": payload["item_number"]})


async def test_item_type_taxonomy_seeded_and_filterable(item_client, tenant_ctx) -> None:
    response = await item_client.get("/api/v1/item-types", headers=tenant_ctx["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 24
    assert {item["code"] for item in items} == {seed.code for seed in ITEM_TYPE_SEEDS}
    inventory = next(item for item in items if item["code"] == "INVENTORY")
    assert inventory["can_stock"] is True
    assert inventory["posting_eligibility"]["POST"]["eligible"] is True
    service = next(item for item in items if item["code"] == "SERVICE")
    assert service["posting_eligibility"]["POST"]["eligible"] is False
    filtered = await item_client.get("/api/v1/item-types", params={"can_stock": "true"}, headers=tenant_ctx["headers"])
    assert filtered.status_code == 200 and len(filtered.json()["items"]) < 24


async def test_create_stockable_item_with_extension_and_stock_uom(item_client, tenant_ctx, db_session) -> None:
    response = await _create_item(item_client, tenant_ctx, item_number="FS01-ITEM-0001", suffix="0001")
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["item_number"] == "FS01-ITEM-0001"
    assert item["item_type_code"] == "INVENTORY"
    assert item["inventory"]["costing_method"] == "AVERAGE"
    assert item["inventory"]["initial_cost"] == "12.5"
    assert item["inventory"]["stock_uom_id"] == item["base_uom_id"]
    assert any(entry["role"] == "STOCK" and entry["is_base"] for entry in item["uoms"])

    detail = await item_client.get(f"/api/v1/items/{item['id']}", headers=tenant_ctx["headers"])
    assert detail.status_code == 200 and detail.json()["item_number"] == "FS01-ITEM-0001"
    inventory = await item_client.get(f"/api/v1/items/{item['id']}/inventory", headers=tenant_ctx["headers"])
    assert inventory.status_code == 200 and inventory.json()["stock_uom_id"] == item["base_uom_id"]
    availability = await item_client.get(f"/api/v1/items/{item['id']}/availability-settings", headers=tenant_ctx["headers"])
    assert availability.status_code == 200 and "planning" in availability.json()

    # item.created + item.uom.changed events were emitted in the caller transaction (outbox rows).
    from sqlalchemy import select

    from app.core.models.platform import outbox_event

    events = (
        await db_session.execute(
            select(outbox_event.c.event_type).where(outbox_event.c.aggregate_id == __import__("uuid").UUID(item["id"]))
        )
    ).scalars().all()
    assert {"item.created", "item.uom.changed"} <= set(events)


async def test_duplicate_item_number_and_idempotent_replay(item_client, tenant_ctx) -> None:
    first = await _create_item(item_client, tenant_ctx, item_number="FS01-DUP-0001", suffix="DUP-0001")
    assert first.status_code == 201, first.text
    replay = await item_client.post(
        "/api/v1/items",
        json={"item_number": "FS01-DUP-0001", "display_name": "Test Item", "item_type_code": "INVENTORY", "inventory": {"costing_method": "AVERAGE", "initial_cost": "12.5"}},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "FS01-DUP-0001"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    duplicate = await item_client.post(
        "/api/v1/items",
        json={"item_number": "FS01-DUP-0001", "display_name": "Again", "item_type_code": "INVENTORY"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "another-key"},
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "ITEM_NUMBER_EXISTS"


async def test_uom_assignment_and_conversion(item_client, tenant_ctx) -> None:
    created = (await _create_item(item_client, tenant_ctx, item_number="FS01-UOM-0001", suffix="UOM-0001")).json()
    uom_types = (await item_client.get("/api/v1/uom-types", headers=tenant_ctx["headers"])).json()["items"]
    ea_type = next(entry for entry in uom_types if entry["code"] == "EA")
    kg_type = next(entry for entry in uom_types if entry["code"] == "WEIGHT")
    ea = next(unit for unit in ea_type["units"] if unit["code"] == "EA")
    case = next((unit for unit in ea_type["units"] if unit["code"] == "CASE"), None)
    assert ea["is_base"] and case is not None
    replaced = await item_client.put(
        f"/api/v1/items/{created['id']}/uoms",
        json={"uoms": [
            {"uom_id": ea["id"], "role": "STOCK", "is_default_for_role": True},
            {"uom_id": case["id"], "role": "PURCHASE", "is_default_for_role": True},
        ]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "uoms-1"},
    )
    assert replaced.status_code == 200, replaced.text
    assert {entry["role"] for entry in replaced.json()["items"]} == {"STOCK", "PURCHASE"}
    converted = await item_client.post(
        "/api/v1/uom-conversions/resolve",
        json={"quantity": "2", "uom_type_id": ea_type["id"], "from_uom_id": case["id"]},
        headers=tenant_ctx["headers"],
    )
    assert converted.status_code == 200, converted.text
    assert Decimal(converted.json()["quantity_base"]) == Decimal("24")
    mismatch = await item_client.put(
        f"/api/v1/items/{created['id']}/uoms",
        json={"uoms": [{"uom_id": next(unit for unit in kg_type["units"] if unit["code"] == "KG")["id"], "role": "PURCHASE"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "uoms-2"},
    )
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "UOM_ASSIGNMENT_INVALID"


async def test_barcode_alias_and_resolution(item_client, tenant_ctx) -> None:
    created = (await _create_item(item_client, tenant_ctx, item_number="FS01-BC-0001", suffix="BC-0001")).json()
    # Valid UPC-A check digit: 036000291452.
    barcode = await item_client.post(
        f"/api/v1/items/{created['id']}/barcodes",
        json={"barcode": "036000291452", "barcode_type": "UPC_A", "is_primary": True},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "barcode-1"},
    )
    assert barcode.status_code == 201, barcode.text
    resolved = await item_client.get("/api/v1/items/resolve/barcode", params={"barcode": "036000291452"}, headers=tenant_ctx["headers"])
    assert resolved.status_code == 200 and resolved.json()["item_id"] == created["id"]
    alias = await item_client.post(
        f"/api/v1/items/{created['id']}/aliases",
        json={"alias": "WMS-ALIAS-1", "alias_type": "WMS"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "alias-1"},
    )
    assert alias.status_code == 201, alias.text
    resolved_alias = await item_client.get("/api/v1/items/resolve/alias", params={"alias": "WMS-ALIAS-1", "alias_type": "WMS"}, headers=tenant_ctx["headers"])
    assert resolved_alias.status_code == 200 and resolved_alias.json()["item_id"] == created["id"]
    invalid = await item_client.post(
        f"/api/v1/items/{created['id']}/barcodes",
        json={"barcode": "036000291453", "barcode_type": "UPC_A"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "barcode-bad"},
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "BARCODE_INVALID"


async def test_matrix_options_generate_and_kit_members(item_client, tenant_ctx) -> None:
    parent = await _create_item(item_client, tenant_ctx, item_number="FS01-MX-PARENT", item_type_code="MATRIX_PARENT", suffix="MX-PARENT")
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    first = await item_client.post(
        f"/api/v1/items/{parent_id}/matrix-options",
        json={"code": "COLOR", "name": "Colour", "values": [{"code": "RED", "value": "Red"}, {"code": "BLUE", "value": "Blue"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "mx-opt-1"},
    )
    assert first.status_code == 201, first.text
    second = await item_client.post(
        f"/api/v1/items/{parent_id}/matrix-options",
        json={"code": "SIZE", "name": "Size", "values": [{"code": "S", "value": "Small"}, {"code": "M", "value": "Medium"}]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "mx-opt-2"},
    )
    assert second.status_code == 201, second.text
    generated = await item_client.post(
        f"/api/v1/items/{parent_id}/matrix-children/generate",
        json={"combinations": [
            {"item_number": "FS01-MX-RED-S", "option_values": {"COLOR": "RED", "SIZE": "S"}},
            {"item_number": "FS01-MX-RED-M", "option_values": {"COLOR": "RED", "SIZE": "M"}},
        ]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "mx-gen-1"},
    )
    assert generated.status_code == 201, generated.text
    assert len(generated.json()["created"]) == 2
    children = await item_client.get(f"/api/v1/items/{parent_id}/matrix-children", headers=tenant_ctx["headers"])
    assert len(children.json()["items"]) == 2

    component = (await _create_item(item_client, tenant_ctx, item_number="FS01-COMP-0001", suffix="COMP-0001")).json()
    kit = await _create_item(item_client, tenant_ctx, item_number="FS01-KIT-0001", item_type_code="KIT", suffix="KIT-0001")
    # KIT creation still needs a base UoM for the parent; use the component's base UoM.
    assert kit.status_code == 201, kit.text
    member = await item_client.post(
        f"/api/v1/items/{kit.json()['id']}/kit-members",
        json={"member_item_id": component["id"], "qty": "2", "uom_id": component["base_uom_id"]},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "kit-member-1"},
    )
    assert member.status_code == 201, member.text
    assert member.json()["members"][0]["member_item_number"] == "FS01-COMP-0001"


async def test_extensions_purchasing_sales_accounting(item_client, tenant_ctx) -> None:
    created = (await _create_item(item_client, tenant_ctx, item_number="FS01-EXT-0001", item_type_code="NON_INVENTORY", suffix="EXT-0001")).json()
    purchasing = await item_client.put(
        f"/api/v1/items/{created['id']}/purchasing",
        json={"purchase_price": "9.99", "purchase_price_currency": "USD", "lead_time_days": 5},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "purch-1"},
    )
    assert purchasing.status_code == 200, purchasing.text
    assert purchasing.json()["purchase_price"] == "9.99"
    sales = await item_client.put(
        f"/api/v1/items/{created['id']}/sales",
        json={"is_sellable": True, "weight": "1.25", "weight_uom": "KG"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "sales-1"},
    )
    assert sales.status_code == 200 and sales.json()["weight"] == "1.25"
    accounting = await item_client.put(
        f"/api/v1/items/{created['id']}/accounting",
        json={"income_account_ref": "REV-1000"},
        headers=tenant_ctx["headers"] | {"Idempotency-Key": "acct-1"},
    )
    assert accounting.status_code == 200 and accounting.json()["income_account_ref"] == "REV-1000"


async def test_tenant_isolation_and_immutability_trigger(item_client, tenant_ctx, second_tenant_ctx, db_session) -> None:
    created = (await _create_item(item_client, tenant_ctx, item_number="FS01-ISO-0001", suffix="ISO-0001")).json()
    cross = await item_client.get(f"/api/v1/items/{created['id']}", headers=second_tenant_ctx["headers"])
    assert cross.status_code == 404 and cross.json()["code"] == "ITEM_NOT_FOUND"
    # Raw SQL immutability trigger (D-22) survives bypassing the service layer.
    connection = await db_session.connection()
    savepoint = await connection.begin_nested()
    with pytest.raises(IntegrityError):
        await connection.execute(
            text("UPDATE item_master.item SET item_type_id = :new_type WHERE tenant_id = :tenant AND id = :id"),
            {"new_type": __import__("uuid").UUID(created["id"]), "tenant": tenant_ctx["tenant_id"], "id": __import__("uuid").UUID(created["id"])},
        )
    await savepoint.rollback()
