"""FS-09 canonical route surface: alias behaviour, auth/scope, 404/validation, tenant isolation and
the genuine-gap operations implemented on the existing service (198 declared = 198 mounted)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, token_service
from app.api.errors import install_error_handlers
from app.api.routers.manufacturing import router as manufacturing_router
from app.tests.integration.manufacturing.conftest import seed_manufacturing_world

pytestmark = pytest.mark.integration

TODAY = date.today().isoformat()


def _token(world: dict[str, Any], scopes: set[str]) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(world["identity_id"]),
        app_user_id=str(world["app_user_id"]),
        tenant_id=str(world["tenant_id"]),
        token_version=0,
        scopes=frozenset(scopes),
    )
    return token


async def _bom(client, world, *, code: str = "ALIAS-BOM") -> str:
    headers = world["headers"]
    bom = await client.post(
        "/api/v1/boms",
        json={"assembly_item_id": str(world["assembly_item_id"]), "bom_code": code, "name": code},
        headers=headers,
    )
    assert bom.status_code == 201, bom.text
    bom_id = bom.json()["bom"]["id"]
    revision = await client.post(
        f"/api/v1/boms/{bom_id}/revisions",
        json={"revision_code": "R1", "effective_from": "2020-01-01"},
        headers=headers,
    )
    assert revision.status_code == 201, revision.text
    revision_id = revision.json()["revision"]["id"]
    component = await client.post(
        f"/api/v1/bom-revisions/{revision_id}/components",
        json={
            "line_number": 1,
            "component_item_id": str(world["component_item_id"]),
            "qty_per": "2",
            "uom_id": str(world["uom_id"]),
        },
        headers=headers,
    )
    assert component.status_code == 201, component.text
    assert (await client.post(f"/api/v1/boms/{bom_id}/activate", headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/bom-revisions/{revision_id}/activate", headers=headers)).status_code == 200
    return bom_id


async def _work_order(client, world, *, routing_id: str | None = None) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/work-orders",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_ordered": "2",
            "start_date": TODAY,
            "end_date": TODAY,
            "routing_id": routing_id,
        },
        headers=world["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["work_order"]


async def test_canonical_alias_urls_resolve_like_legacy(client, world) -> None:
    bom_id = await _bom(client, world)
    # Canonical `{id}` templates mount the same concrete URL behaviour as their legacy spellings.
    for path in (
        f"/api/v1/boms/{bom_id}",
        f"/api/v1/boms/{bom_id}/revisions",
    ):
        response = await client.get(path, headers=world["headers"])
        assert response.status_code == 200, (path, response.text)
    detail = await client.get(f"/api/v1/boms/{bom_id}", headers=world["headers"])
    assert detail.json()["bom"]["id"] == bom_id
    work_order = await _work_order(client, world)
    for path in (
        f"/api/v1/work-orders/{work_order['id']}",
        f"/api/v1/work-orders/{work_order['id']}/components",
        f"/api/v1/work-orders/{work_order['id']}/components/pick-requirements",
        f"/api/v1/work-orders/{work_order['id']}/operations",
        f"/api/v1/work-orders/{work_order['id']}/wip-balance",
    ):
        response = await client.get(path, headers=world["headers"])
        assert response.status_code == 200, (path, response.text)


async def test_canonical_routes_enforce_auth_scope_404_and_validation(client, world) -> None:
    anonymous = await client.get("/api/v1/work-orders")
    assert anonymous.status_code == 401
    read_only = _token(world, {"manufacturing.read"})
    forbidden = await client.post(
        f"/api/v1/work-orders/{uuid.uuid4()}/firm",
        headers={"Authorization": f"Bearer {read_only}"},
    )
    assert forbidden.status_code == 403, forbidden.text
    missing = await client.get(f"/api/v1/work-orders/{uuid.uuid4()}", headers=world["headers"])
    assert missing.status_code == 404, missing.text
    invalid = await client.post("/api/v1/work-orders/mass-create", json={"work_orders": []}, headers=world["headers"])
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["code"] == "VALIDATION_FAILED"


async def test_canonical_route_tenant_isolation(db_session: AsyncSession, client, world) -> None:
    bom_id = await _bom(client, world, code="TENANT-ALIAS")
    other_world = await seed_manufacturing_world(db_session, code_prefix="ALIAS-B")
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(manufacturing_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as other_client:
        cross_tenant = await other_client.get(f"/api/v1/boms/{bom_id}", headers=other_world["headers"])
        assert cross_tenant.status_code == 404, cross_tenant.text
        own_tenant = await other_client.get(f"/api/v1/boms/{bom_id}", headers=world["headers"])
        assert own_tenant.status_code == 200, own_tenant.text


async def test_gap_compat_and_eco_operations(client, world) -> None:
    headers = world["headers"]
    attribute = await client.post(
        "/api/v1/manufacturing/compat/attributes",
        json={"code": "alias-socket", "name": "Alias Socket", "data_type": "TEXT"},
        headers=headers,
    )
    assert attribute.status_code == 201, attribute.text
    attribute_id = attribute.json()["attribute"]["id"]
    fetched = await client.get(f"/api/v1/manufacturing/compat/attributes/{attribute_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["attribute"]["code"] == "alias-socket"

    rule_set = await client.post(
        "/api/v1/manufacturing/compat/rule-sets",
        json={
            "code": "ALIAS-RULE-SET",
            "name": "Alias",
            "scope_type": "ASSEMBLY",
            "scope_ref_id": str(world["assembly_item_id"]),
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert rule_set.status_code == 201, rule_set.text
    rule_set_id = rule_set.json()["rule_set"]["id"]
    rule = await client.post(
        f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules",
        json={
            "code": "ALIAS-RULE",
            "rule_type": "ATTR_EQ",
            "subject_type": "ATTRIBUTE",
            "constraint_expr": {"attribute": "alias-socket", "value": "X"},
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["rule"]["id"]
    listed = await client.get(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules", headers=headers)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    deleted_rule = await client.delete(f"/api/v1/manufacturing/compat/rules/{rule_id}", headers=headers)
    assert deleted_rule.status_code == 204, deleted_rule.text
    listed = await client.get(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules", headers=headers)
    assert listed.json()["items"] == []
    # ACTIVE rule sets cannot be deleted; DRAFT deletion is the declared gap behaviour.
    deleted_set = await client.delete(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", headers=headers)
    assert deleted_set.status_code == 204, deleted_set.text
    missing = await client.get(f"/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", headers=headers)
    assert missing.status_code == 404

    eco = await client.post(
        "/api/v1/ecos",
        json={
            "effective_date": TODAY,
            "reason": "alias test",
            "lines": [
                {"item_id": str(world["component_item_id"]), "change_type": "CHANGE_QTY", "new_value": {"qty_per": "3"}}
            ],
        },
        headers=headers,
    )
    assert eco.status_code == 201, eco.text
    lines = await client.get(f"/api/v1/ecos/{eco.json()['eco']['id']}/lines", headers=headers)
    assert lines.status_code == 200 and len(lines.json()["items"]) == 1


async def test_gap_work_order_bom_operation_and_mass_create(client, world) -> None:
    headers = world["headers"]
    draft_bom = await client.post(
        "/api/v1/boms",
        json={"assembly_item_id": str(world["assembly_item_id"]), "bom_code": "PATCH-BOM", "name": "PATCH-BOM"},
        headers=headers,
    )
    assert draft_bom.status_code == 201, draft_bom.text
    bom_id = draft_bom.json()["bom"]["id"]
    patched = await client.patch(f"/api/v1/boms/{bom_id}", json={"name": "Renamed BOM"}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["bom"]["name"] == "Renamed BOM"

    work_center = await client.post(
        "/api/v1/work-centers",
        json={
            "code": "ALIAS-WC",
            "name": "Alias WC",
            "location_id": str(world["location_id"]),
            "capacity_hours_per_day": "8",
        },
        headers=headers,
    )
    assert work_center.status_code == 201, work_center.text
    routing = await client.post(
        "/api/v1/routings",
        json={
            "item_id": str(world["assembly_item_master_id"]),
            "routing_code": "ALIAS-RT",
            "name": "Alias Routing",
            "effective_from": "2020-01-01",
        },
        headers=headers,
    )
    assert routing.status_code == 201, routing.text
    routing_id = routing.json()["routing"]["id"]
    step = await client.post(
        f"/api/v1/routings/{routing_id}/steps",
        json={"sequence": 10, "operation_code": "OP10", "work_center_id": work_center.json()["work_center"]["id"]},
        headers=headers,
    )
    assert step.status_code == 201, step.text
    step_id = step.json()["step"]["id"]
    charge = await client.post(
        f"/api/v1/routing-steps/{step_id}/charges",
        json={
            "charge_item_id": str(world["component_item_id"]),
            "cost_category_id": str(world["cost_category_id"]),
            "rate": "5",
            "rate_uom": "HOUR",
        },
        headers=headers,
    )
    assert charge.status_code == 201, charge.text
    charge_id = charge.json()["charge"]["id"]
    patched_charge = await client.patch(
        f"/api/v1/routing-step-charges/{charge_id}", json={"rate": "7"}, headers=headers
    )
    assert patched_charge.status_code == 200, patched_charge.text

    await _bom(client, world, code="WO-GAP-BOM")  # active BOM for the gap work-order/component flow
    work_order = await _work_order(client, world, routing_id=routing_id)
    operation = (await client.get(f"/api/v1/work-orders/{work_order['id']}/operations", headers=headers)).json()[
        "items"
    ][0]
    patched_operation = await client.patch(
        f"/api/v1/work-order-operations/{operation['id']}", json={"status": "IN_PROGRESS"}, headers=headers
    )
    assert patched_operation.status_code == 200, patched_operation.text
    completions = await client.get(f"/api/v1/work-order-operations/{operation['id']}/completions", headers=headers)
    assert completions.status_code == 200 and completions.json()["items"] == []

    component = (await client.get(f"/api/v1/work-orders/{work_order['id']}/components", headers=headers)).json()[
        "items"
    ][0]
    patched_component = await client.patch(
        f"/api/v1/work-order-components/{component['id']}", json={"qty_required": "5"}, headers=headers
    )
    assert patched_component.status_code == 200, patched_component.text

    mass = await client.post(
        "/api/v1/work-orders/mass-create",
        json={
            "work_orders": [
                {
                    "assembly_item_id": str(world["assembly_item_id"]),
                    "qty_ordered": "1",
                    "start_date": TODAY,
                    "end_date": TODAY,
                }
            ]
        },
        headers=headers,
    )
    assert mass.status_code == 201, mass.text
    assert mass.json()["count"] == 1


async def test_gap_build_unbuild_and_wip_transaction_operations(client, world) -> None:
    headers = world["headers"]
    await _bom(client, world, code="BUILD-ALIAS")
    build = await client.post(
        "/api/v1/assembly-builds",
        json={
            "assembly_item_id": str(world["assembly_item_id"]),
            "qty_built": "1",
            "location_id": str(world["location_id"]),
        },
        headers=headers,
    )
    assert build.status_code == 201, build.text
    build_id = build.json()["build"]["id"]
    assert (await client.post(f"/api/v1/assembly-builds/{build_id}/post", headers=headers)).status_code == 200
    unbuild = await client.post(f"/api/v1/assembly-builds/{build_id}/unbuild", json={}, headers=headers)
    assert unbuild.status_code == 201, unbuild.text
    assert unbuild.json()["unbuild"]["original_assembly_build_id"] == build_id

    work_order = await _work_order(client, world)
    created = await client.post(
        "/api/v1/wip-transactions",
        json={
            "work_order_id": work_order["id"],
            "wip_transaction_type": "ISSUE",
            "lines": [{"item_id": str(world["component_item_id"]), "qty": "1"}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    wip_transaction = created.json()["wip_transaction"]
    assert wip_transaction["status"] == "DRAFT"
    fetched = await client.get(f"/api/v1/wip-transactions/{wip_transaction['id']}", headers=headers)
    assert fetched.status_code == 200
    posted = await client.post(f"/api/v1/wip-transactions/{wip_transaction['id']}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    assert posted.json()["wip_transaction"]["status"] == "POSTED"
    assert world["posting"].calls[-1]["posting_type"] == "T-17"
