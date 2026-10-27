"""FS-03 traceability registry integration tests (live Postgres; graceful skip when unavailable)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_current_principal
from app.tests.integration.traceability.registry.conftest import (
    ITEM_ID,
    TENANT_ID,
    create_bin,
    create_location,
    create_zone,
    idem,
)

pytestmark = pytest.mark.integration


async def test_bootstrap_state_and_seed_catalogue(client: Any, seeded: dict[str, Any]) -> None:
    response = await client.get("/api/v1/traceability/bootstrap")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["sentinel_present"] is True
    assert state["sentinel_location_id"] == seeded["sentinel"]["id"]
    assert state["status_count"] == 7
    assert state["default_status_code"] == "AVAILABLE"
    assert state["seeded"] is True
    assert state["domain_enabled"] is True

    statuses = await client.get("/api/v1/inventory-statuses")
    assert statuses.status_code == 200
    by_code = {row["code"]: row for row in statuses.json()["items"]}
    assert set(by_code) == {"AVAILABLE", "HOLD", "DAMAGED", "RETURNED", "CONSIGNED_GOOD", "USED", "REFURB"}
    assert by_code["AVAILABLE"]["is_default"] is True
    assert by_code["AVAILABLE"]["is_system"] is True
    for code in ("USED", "REFURB"):
        assert by_code[code]["is_available_for_allocation"] is True
        assert by_code[code]["is_committable"] is False
        assert by_code[code]["is_sellable"] is False
        assert by_code[code]["is_system"] is False
    assert by_code["CONSIGNED_GOOD"]["is_consigned"] is True
    assert by_code["HOLD"]["is_committable"] is False


async def test_location_hierarchy_and_lifecycle(client: Any, db_session: Any) -> None:
    warehouse = await create_location(client, code="WH-MAIN", bins=True)
    store = await create_location(client, code="STORE-1", location_type="STORE", parent_id=warehouse["id"])
    child = await create_location(client, code="WH-CHILD", parent_id=warehouse["id"])

    children = await client.get(f"/api/v1/locations/{warehouse['id']}/children")
    assert children.status_code == 200
    assert {row["code"] for row in children.json()["items"]} == {"STORE-1", "WH-CHILD"}

    descendants = await client.get(f"/api/v1/locations/{warehouse['id']}/descendants")
    assert descendants.status_code == 200
    assert {row["code"] for row in descendants.json()["items"]} == {"STORE-1", "WH-CHILD"}
    assert descendants.json()["depth"] == 2

    cycle = await client.patch(
        f"/api/v1/locations/{warehouse['id']}",
        json={"parent_id": child["id"]},
        headers={"If-Match": str(warehouse["version"])},
    )
    assert cycle.status_code == 422
    assert cycle.json()["code"] in {"HIERARCHY_CYCLE", "INVALID_HIERARCHY"}

    stale = await client.patch(
        f"/api/v1/locations/{warehouse['id']}", json={"name": "Renamed"}, headers={"If-Match": "1"}
    )
    assert stale.status_code in {200, 412}

    duplicate = await client.post(
        "/api/v1/locations", json={"code": "WH-MAIN", "name": "Dup", "location_type": "WAREHOUSE"}, headers=idem()
    )
    assert duplicate.status_code == 409

    parent_update = await client.patch(
        f"/api/v1/locations/{store['id']}", json={"name": "Main Store"}, headers={"If-Match": str(store["version"])}
    )
    assert parent_update.status_code == 200, parent_update.text
    assert parent_update.json()["location"]["name"] == "Main Store"

    reactivate = await client.post(f"/api/v1/locations/{store['id']}/activate", json={}, headers=idem())
    assert reactivate.status_code == 409


async def test_location_update_rejects_immutable_identity_fields(client: Any) -> None:
    """PATCH no longer accepts code/location_type/is_unallocated (contract residual fix)."""
    warehouse = await create_location(client, code="WH-IMMUTABLE")
    for field, value in (("code", "WH-CHANGED"), ("location_type", "STORE"), ("is_unallocated", True)):
        response = await client.patch(
            f"/api/v1/locations/{warehouse['id']}",
            json={field: value},
            headers={"If-Match": str(warehouse["version"])},
        )
        assert response.status_code == 422, (field, response.status_code, response.text)

    renamed = await client.patch(
        f"/api/v1/locations/{warehouse['id']}",
        json={"name": "Renamed"},
        headers={"If-Match": str(warehouse["version"])},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["location"]["name"] == "Renamed"
    assert renamed.json()["location"]["code"] == "WH-IMMUTABLE"


async def test_sentinel_is_immutable(client: Any, seeded: dict[str, Any]) -> None:
    sentinel_id = seeded["sentinel"]["id"]
    patch = await client.patch(f"/api/v1/locations/{sentinel_id}", json={"name": "Renamed sentinel"})
    assert patch.status_code == 409
    assert patch.json()["code"] == "UNALLOCATED_LOCATION_IMMUTABLE"

    retype = await client.patch(f"/api/v1/locations/{sentinel_id}", json={"location_type": "WAREHOUSE"})
    assert retype.status_code in {409, 422}

    inactivate = await client.post(
        f"/api/v1/locations/{sentinel_id}/inactivate", json={"reason": "cleanup"}, headers=idem()
    )
    assert inactivate.status_code == 409
    assert inactivate.json()["code"] == "UNALLOCATED_LOCATION_IMMUTABLE"

    child = await client.post(
        "/api/v1/locations",
        json={"code": "SENTINEL-CHILD", "name": "Bad", "location_type": "STORE", "parent_id": sentinel_id},
        headers=idem(),
    )
    assert child.status_code == 422


async def test_location_inactivation_guards_and_strict_mode(client: Any, db_session: Any) -> None:
    location = await create_location(client, code="WH-GUARD", bins=True)
    zone = await create_zone(client, location_id=location["id"], code="Z-G")
    bin_row = await create_bin(client, location_id=location["id"], code="B-G", zone_id=zone["id"])

    blocked = await client.post(
        f"/api/v1/locations/{location['id']}/inactivate", json={"reason": "closed"}, headers=idem()
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "BINS_IN_USE"

    assert (
        await client.post(f"/api/v1/bins/{bin_row['id']}/inactivate", json={"reason": "closed"}, headers=idem())
    ).status_code == 200
    assert (await client.post(f"/api/v1/zones/{zone['id']}/inactivate", json={}, headers=idem())).status_code == 200
    ok = await client.post(f"/api/v1/locations/{location['id']}/inactivate", json={"reason": "closed"}, headers=idem())
    assert ok.status_code == 200, ok.text
    assert ok.json()["location"]["is_inactive"] is True
    assert "warnings" in ok.json()

    strict_location = await create_location(client, code="WH-STRICT")
    await db_session.execute(
        text(
            "INSERT INTO platform.tenant_setting (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version) "
            "VALUES (:id, :tenant, 'inventory', 'strict_inactivation', 'true'::jsonb, 'BOOLEAN', false, now(), now(), 1)"
            " ON CONFLICT (tenant_id, namespace, key) DO UPDATE SET value = 'true'::jsonb"
        ),
        {"id": uuid.uuid4(), "tenant": str(TENANT_ID)},
    )
    await db_session.flush()
    strict = await client.post(
        f"/api/v1/locations/{strict_location['id']}/inactivate", json={"reason": "strict"}, headers=idem()
    )
    assert strict.status_code == 409
    assert strict.json()["code"] == "LOCATION_HAS_STOCK_GUARD_UNAVAILABLE"

    child_blocked = await client.post(
        "/api/v1/locations", json={"code": "WH-PARENT", "name": "Parent", "location_type": "WAREHOUSE"}, headers=idem()
    )
    assert child_blocked.status_code == 201
    parent_id = child_blocked.json()["location"]["id"]
    await client.post(
        "/api/v1/locations",
        json={"code": "WH-KID", "name": "Kid", "location_type": "STORE", "parent_id": parent_id},
        headers=idem(),
    )
    has_children = await client.post(f"/api/v1/locations/{parent_id}/inactivate", json={"reason": "x"}, headers=idem())
    assert has_children.status_code == 422
    assert has_children.json()["code"] == "INVALID_HIERARCHY"


async def test_bins_require_enabled_location_and_tenant_setting(client: Any, db_session: Any) -> None:
    plain = await create_location(client, code="WH-NOBINS", bins=False)
    denied = await client.post(
        "/api/v1/bins",
        json={"location_id": plain["id"], "code": "B-X", "name": "B", "bin_type": "STORAGE"},
        headers=idem(),
    )
    assert denied.status_code == 422
    assert denied.json()["code"] == "BINS_NOT_ENABLED"

    await db_session.execute(
        text(
            "UPDATE platform.tenant_setting SET value = 'false'::jsonb WHERE tenant_id = :tenant AND namespace = 'inventory' AND key = 'bins.enabled'"
        ),
        {"tenant": str(TENANT_ID)},
    )
    await db_session.flush()
    located = await create_location(client, code="WH-GLOBAL-OFF", bins=False)
    global_off = await client.post(
        "/api/v1/bins",
        json={"location_id": located["id"], "code": "B-Y", "name": "B", "bin_type": "STORAGE"},
        headers=idem(),
    )
    assert global_off.status_code == 422
    assert global_off.json()["code"] == "BINS_NOT_ENABLED"

    bins_on = await client.post(
        "/api/v1/locations",
        json={"code": "WH-GLOBAL-ON", "name": "On", "location_type": "WAREHOUSE", "is_bins_enabled": True},
        headers=idem(),
    )
    assert bins_on.status_code == 422
    assert bins_on.json()["code"] == "BINS_NOT_ENABLED"


async def test_zone_and_bin_topology_rules(client: Any) -> None:
    location = await create_location(client, code="WH-TOPO")
    other = await create_location(client, code="WH-OTHER")
    zone = await create_zone(client, location_id=location["id"], code="Z-A", priority=5)
    bad_zone = await create_zone(client, location_id=other["id"], code="Z-B")

    mismatch = await client.post(
        "/api/v1/bins",
        json={
            "location_id": location["id"],
            "zone_id": bad_zone["id"],
            "code": "B-MIS",
            "name": "B",
            "bin_type": "STORAGE",
        },
        headers=idem(),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "ZONE_LOCATION_MISMATCH"

    bin_row = await create_bin(client, location_id=location["id"], code="B-1", zone_id=zone["id"])
    zone_block = await client.post(f"/api/v1/zones/{zone['id']}/inactivate", json={}, headers=idem())
    assert zone_block.status_code == 409
    bins = await client.get(f"/api/v1/locations/{location['id']}/bins")
    assert bins.status_code == 200
    assert [row["code"] for row in bins.json()["items"]] == ["B-1"]

    duplicate = await client.post(
        "/api/v1/bins",
        json={"location_id": location["id"], "code": "B-1", "name": "Dup", "bin_type": "STORAGE"},
        headers=idem(),
    )
    assert duplicate.status_code == 409

    inactive = await client.post(
        f"/api/v1/bins/{bin_row['id']}/inactivate", json={"reason": "cycle count"}, headers=idem()
    )
    assert inactive.status_code == 200
    active = await client.post(f"/api/v1/bins/{bin_row['id']}/activate", json={}, headers=idem())
    assert active.status_code == 200
    again = await client.post(f"/api/v1/bins/{bin_row['id']}/activate", json={}, headers=idem())
    assert again.status_code == 409
    assert again.json()["code"] == "INVALID_STATE_TRANSITION"

    uom_bin = await client.post(
        "/api/v1/bins",
        json={
            "location_id": location["id"],
            "code": "B-UOM",
            "name": "Capacity",
            "bin_type": "STORAGE",
            "capacity_qty": "10.5",
            "capacity_uom_id": "00000000-0000-7000-8000-0000000000ff",
        },
        headers=idem(),
    )
    assert uom_bin.status_code == 422


async def test_status_catalogue_lifecycle_and_flags(client: Any) -> None:
    created = await client.post(
        "/api/v1/inventory-statuses",
        json={
            "code": "QUARANTINE",
            "name": "Quarantine",
            "is_available_for_allocation": False,
            "is_committable": False,
            "is_sellable": False,
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    quarantine = created.json()["inventory_status"]
    duplicate = await client.post(
        "/api/v1/inventory-statuses", json={"code": "QUARANTINE", "name": "Dup"}, headers=idem()
    )
    assert duplicate.status_code == 409

    invalid_flags = await client.patch(
        f"/api/v1/inventory-statuses/{quarantine['id']}",
        json={"is_available_for_allocation": False, "is_committable": True},
        headers={"If-Match": str(quarantine["version"])},
    )
    assert invalid_flags.status_code == 422
    assert invalid_flags.json()["code"] in {"VALIDATION_FAILED", "STATUS_FLAGS_INVALID"}

    statuses = {row["code"]: row for row in (await client.get("/api/v1/inventory-statuses")).json()["items"]}
    available = statuses["AVAILABLE"]
    system_edit = await client.patch(
        f"/api/v1/inventory-statuses/{available['id']}",
        json={"is_committable": False},
        headers={"If-Match": str(available["version"])},
    )
    assert system_edit.status_code == 409
    assert system_edit.json()["code"] == "SYSTEM_STATUS_IMMUTABLE"
    sellable_ok = await client.patch(
        f"/api/v1/inventory-statuses/{available['id']}",
        json={"is_sellable": False},
        headers={"If-Match": str(available["version"])},
    )
    assert sellable_ok.status_code == 200

    hold = statuses["HOLD"]
    set_hold = await client.post(f"/api/v1/inventory-statuses/{hold['id']}/set-default", headers=idem())
    assert set_hold.status_code == 200, set_hold.text
    state = (await client.get("/api/v1/traceability/bootstrap")).json()
    assert state["default_status_code"] == "HOLD"
    default_inactivate = await client.post(
        f"/api/v1/inventory-statuses/{hold['id']}/inactivate", json={"reason": "x"}, headers=idem()
    )
    assert default_inactivate.status_code == 409

    set_available = await client.post(f"/api/v1/inventory-statuses/{available['id']}/set-default", headers=idem())
    assert set_available.status_code == 200

    consigned_custom = await client.post(
        "/api/v1/inventory-statuses",
        json={"code": "CONSIGNED_X", "name": "Consigned X", "is_consigned": True, "is_default": True},
        headers=idem(),
    )
    assert consigned_custom.status_code == 422
    assert consigned_custom.json()["code"] in {"DEFAULT_STATUS_CONFLICT", "VALIDATION_FAILED"}

    used = statuses["USED"]
    inactivated = await client.post(
        f"/api/v1/inventory-statuses/{used['id']}/inactivate", json={"reason": "retire"}, headers=idem()
    )
    assert inactivated.status_code == 200
    activated = await client.post(f"/api/v1/inventory-statuses/{used['id']}/activate", json={}, headers=idem())
    assert activated.status_code == 200

    system_inactivate = await client.post(
        f"/api/v1/inventory-statuses/{available['id']}/inactivate", json={"reason": "x"}, headers=idem()
    )
    assert system_inactivate.status_code == 409
    assert system_inactivate.json()["code"] == "SYSTEM_STATUS_IMMUTABLE"


async def test_posting_eligibility_and_consignment(client: Any, seeded: dict[str, Any], db_session: Any) -> None:
    location = await create_location(client, code="WH-POST", statuses=True)
    zone = await create_zone(client, location_id=location["id"], code="Z-P")
    bin_row = await create_bin(client, location_id=location["id"], code="B-P", zone_id=zone["id"])

    eligible = await client.get(
        f"/api/v1/locations/{location['id']}/posting-eligibility",
        params={"item_id": seeded["item_id"], "bin_id": bin_row["id"]},
    )
    assert eligible.status_code == 200, eligible.text
    body = eligible.json()
    assert body["eligible"] is True
    assert body["normalized"]["inventory_status_id"] == seeded["statuses"]["AVAILABLE"]["id"]
    assert body["normalized"]["location_id"] == location["id"]

    no_bin = await client.get(
        f"/api/v1/locations/{location['id']}/posting-eligibility", params={"item_id": seeded["item_id"]}
    )
    assert no_bin.status_code == 422
    assert no_bin.json()["code"] == "LOCATION_REQUIRED"

    consigned = await client.get(
        f"/api/v1/locations/{location['id']}/posting-eligibility",
        params={"item_id": seeded["item_id"], "bin_id": bin_row["id"], "ownership": "CONSIGNED"},
    )
    assert consigned.status_code == 200
    assert consigned.json()["eligible"] is False
    assert any(error["code"] == "CONSIGNMENT_NOT_ALLOWED" for error in consigned.json()["errors"])

    consigned_bin = await client.get(
        f"/api/v1/locations/{location['id']}/posting-eligibility",
        params={
            "item_id": seeded["item_id"],
            "bin_id": bin_row["id"],
            "inventory_status_id": seeded["statuses"]["CONSIGNED_GOOD"]["id"],
        },
    )
    assert consigned_bin.status_code == 200

    other_location = await create_location(client, code="WH-ELSE")
    mismatch = await client.get(
        f"/api/v1/locations/{other_location['id']}/posting-eligibility",
        params={"item_id": seeded["item_id"], "bin_id": bin_row["id"]},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "LOCATION_MISMATCH"


async def test_tenant_isolation(
    client: Any, traceability_app: Any, principal: Any, other_principal: Any, seeded: dict[str, Any]
) -> None:
    location = await create_location(client, code="WH-ISO")
    traceability_app.dependency_overrides[get_current_principal] = lambda: other_principal
    try:
        listing = await client.get("/api/v1/locations")
        assert listing.status_code == 200
        assert all(row["code"] != "WH-ISO" for row in listing.json()["items"])
        cross = await client.get(f"/api/v1/locations/{location['id']}")
        assert cross.status_code == 404
        cross_status = await client.get(f"/api/v1/inventory-statuses/{seeded['statuses']['AVAILABLE']['id']}")
        assert cross_status.status_code == 404
    finally:
        traceability_app.dependency_overrides[get_current_principal] = lambda: principal


async def test_idempotent_replay_and_version_guard(client: Any) -> None:
    body = {"code": "WH-IDEM", "name": "Idem", "location_type": "WAREHOUSE", "is_bins_enabled": True}
    headers = {"Idempotency-Key": "same-key-location"}
    first = await client.post("/api/v1/locations", json=body, headers=headers)
    assert first.status_code == 201
    replay = await client.post("/api/v1/locations", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["location"]["id"] == first.json()["location"]["id"]
    conflict = await client.post("/api/v1/locations", json={**body, "name": "Different"}, headers=headers)
    assert conflict.status_code == 409


async def test_deferred_item_master_fks_closed(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    location = await create_location(client, code="WH-FK", statuses=True)
    zone = await create_zone(client, location_id=location["id"], code="Z-FK")
    bin_row = await create_bin(client, location_id=location["id"], code="B-FK", zone_id=zone["id"])
    available_id = seeded["statuses"]["AVAILABLE"]["id"]

    await db_session.execute(
        text(
            "INSERT INTO item_master.item_location (tenant_id, item_id, location_id, default_bin_id, default_status_id, version) "
            "VALUES (:tenant, :item, :location, :bin, :status, 1)"
        ),
        {
            "tenant": str(TENANT_ID),
            "item": str(ITEM_ID),
            "location": location["id"],
            "bin": bin_row["id"],
            "status": available_id,
        },
    )
    await db_session.execute(
        text(
            "INSERT INTO item_master.item_bin (tenant_id, item_id, location_id, bin_id, version) "
            "VALUES (:tenant, :item, :location, :bin, 1)"
        ),
        {"tenant": str(TENANT_ID), "item": str(ITEM_ID), "location": location["id"], "bin": bin_row["id"]},
    )
    await db_session.execute(
        text(
            "UPDATE item_master.item_inventory SET default_status_id = :status WHERE tenant_id = :tenant AND item_id = :item"
        ),
        {"status": available_id, "tenant": str(TENANT_ID), "item": str(ITEM_ID)},
    )
    await db_session.flush()

    # orphan references are rejected by the FS-03-closed composite FKs
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO item_master.item_bin (tenant_id, item_id, location_id, bin_id, version) "
                "VALUES (:tenant, :item, :location, :bin, 1)"
            ),
            {"tenant": str(TENANT_ID), "item": str(ITEM_ID), "location": location["id"], "bin": str(uuid.uuid4())},
        )
    await db_session.rollback()

    constraints = await db_session.execute(
        text(
            "SELECT conname FROM pg_constraint WHERE conname IN ('fk_item_location_location_id','fk_item_bin_bin_id','fk_item_inventory_default_status_id')"
        )
    )
    assert len([row[0] for row in constraints.all()]) == 3


async def test_events_and_audit_written(client: Any, db_session: Any) -> None:
    location = await create_location(client, code="WH-EVT")
    await db_session.flush()
    outbox = await db_session.execute(
        text(
            "SELECT aggregate_id FROM platform.outbox_event "
            "WHERE tenant_id = :tenant AND event_type = 'traceability.location.created' AND aggregate_id = :location"
        ),
        {"tenant": str(TENANT_ID), "location": location["id"]},
    )
    assert outbox.first() is not None
    audit = await db_session.execute(
        text(
            "SELECT count(*) FROM platform.audit_event WHERE tenant_id = :tenant AND action = 'traceability.location.created'"
        ),
        {"tenant": str(TENANT_ID)},
    )
    assert int(audit.scalar_one()) >= 1


async def test_domain_flag_gate(client: Any, db_session: Any) -> None:
    await db_session.execute(
        text(
            "UPDATE platform.feature_flag SET is_enabled = false WHERE tenant_id = :tenant AND flag_key = 'traceability.registry.enabled'"
        ),
        {"tenant": str(TENANT_ID)},
    )
    await db_session.flush()
    disabled = await client.get("/api/v1/locations")
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "DOMAIN_DISABLED"
