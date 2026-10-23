"""Δ-SSO connection administration: lifecycle, scopes, cross-tenant isolation, group mappings."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.models.platform import sso_group_role_map
from app.tests.integration.platform_sso.conftest import create_connection, seed_tenant

pytestmark = pytest.mark.integration


async def test_connection_lifecycle_scopes_and_cross_tenant(sso_client, db_session) -> None:
    tenant_a = await seed_tenant(db_session, code="alpha")
    tenant_b = await seed_tenant(db_session, code="bravo")

    created = await create_connection(sso_client, tenant_a["headers"])
    connection_id = created["id"]
    assert created["status"] == "DRAFT"
    assert created["client_secret_configured"] is True
    assert "client_secret_ref" not in created

    listed = await sso_client.get("/api/v1/admin/sso-connections", headers=tenant_a["headers"])
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [connection_id]

    # Cross-tenant probe is indistinguishable from missing (404, no existence leak).
    cross = await sso_client.get(f"/api/v1/admin/sso-connections/{connection_id}", headers=tenant_b["headers"])
    assert cross.status_code == 404
    assert cross.headers["content-type"].startswith("application/problem+json")
    assert cross.json()["code"] == "SSO_CONNECTION_NOT_FOUND"
    other_list = await sso_client.get("/api/v1/admin/sso-connections", headers=tenant_b["headers"])
    assert other_list.json()["items"] == []

    # Test action performs the Dex health check and promotes DRAFT -> ACTIVE.
    tested = await sso_client.post(f"/api/v1/admin/sso-connections/{connection_id}/test", headers=tenant_a["headers"])
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "ACTIVE"
    assert tested.json()["last_tested_at"]

    patched = await sso_client.patch(
        f"/api/v1/admin/sso-connections/{connection_id}",
        json={"name": "Acme IdP v2", "jit_provisioning": False},
        headers=tenant_a["headers"],
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Acme IdP v2"

    rotated = await sso_client.post(
        f"/api/v1/admin/sso-connections/{connection_id}/rotate-secret",
        json={"secret_ref": "env:DEX_CLIENT_SECRET_V2"},
        headers=tenant_a["headers"],
    )
    assert rotated.status_code == 200
    assert rotated.json()["secret_ref_updated"] is True
    assert "env:DEX_CLIENT_SECRET_V2" not in rotated.text

    deleted = await sso_client.delete(f"/api/v1/admin/sso-connections/{connection_id}", headers=tenant_a["headers"])
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    missing = await sso_client.get(f"/api/v1/admin/sso-connections/{connection_id}", headers=tenant_a["headers"])
    assert missing.status_code == 404


async def test_admin_endpoints_require_sso_scope_and_bearer(sso_client, db_session) -> None:
    tenant = await seed_tenant(db_session, code="gamma", scopes={"items.read"})
    denied = await sso_client.get("/api/v1/admin/sso-connections", headers=tenant["headers"])
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_SCOPE"
    unauthenticated = await sso_client.get("/api/v1/admin/sso-connections")
    assert unauthenticated.status_code == 401


async def test_group_mapping_bounds_and_sod_rejection(sso_client, db_session) -> None:
    tenant = await seed_tenant(
        db_session,
        code="delta",
        roles=[
            {"code": "lead", "scopes": ["platform.sso.read"]},
            {"code": "po_creator", "scopes": ["procurement.po.create"]},
            {"code": "po_approver", "scopes": ["procurement.po.approve"]},
        ],
    )
    connection = await create_connection(sso_client, tenant["headers"])
    base = f"/api/v1/admin/sso-connections/{connection['id']}/group-mappings"

    unknown = await sso_client.put(
        base,
        json={"mappings": [{"match_type": "EXACT", "group_value": "ghost", "role_key": "nope"}]},
        headers=tenant["headers"],
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "SSO_GROUP_MAPPING_INVALID"

    sod = await sso_client.put(
        base,
        json={
            "mappings": [
                {"match_type": "EXACT", "group_value": "po-creators", "role_key": "po_creator"},
                {"match_type": "EXACT", "group_value": "po-approvers", "role_key": "po_approver"},
            ]
        },
        headers=tenant["headers"],
    )
    assert sod.status_code == 409
    assert sod.json()["code"] == "SOD_CONFLICT"

    valid = await sso_client.put(
        base,
        json={"mappings": [{"match_type": "EXACT", "group_value": "leads", "role_key": "lead", "priority": 10}]},
        headers=tenant["headers"],
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["items"][0]["role_key"] == "lead"
    stored = await db_session.execute(select(func.count()).select_from(sso_group_role_map))
    assert int(stored.scalar_one()) == 1
