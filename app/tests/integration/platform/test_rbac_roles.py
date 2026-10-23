"""FS-00 Δ-RBAC role lifecycle: catalogue, CRUD, template immutability, assignment/effective scopes."""

from __future__ import annotations

import pytest

from app.tests.integration.platform.support import (
    create_identity,
    create_membership,
    create_role,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration

CATALOGUE_URL = "/api/v1/admin/permissions"
ROLES_URL = "/api/v1/admin/roles"


async def _seed_actor(db_session, code: str):
    actor = await seed_admin_tenant(db_session, code=code)
    # Provision the ordinary catalogue rows used by role CRUD.
    await create_role(
        db_session,
        tenant_id=actor["tenant_id"],
        code="provision_seed",
        scopes=("items.read", "items.update", "inventory.adjustment.post", "inventory.adjustment.approve"),
    )
    target_identity = await create_identity(db_session, email=f"target@{code.lower()}.example")
    target = await create_membership(
        db_session,
        tenant_id=actor["tenant_id"],
        identity_id=target_identity,
        email=f"target@{code.lower()}.example",
        scopes=(),
    )
    return actor, target


async def test_permissions_catalogue_counts(app_client, db_session) -> None:
    actor, _ = await _seed_actor(db_session, "EGT-RBAC-CAT")
    response = await app_client.get(CATALOGUE_URL, headers=actor["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {
        "scopes": 255,
        "assignable": 241,
        "service_only": 13,
        "platform_only": 1,
        "role_templates": 7,
        "sod_pairs": 14,
    }
    assert body["version"] == "1.5.0"
    assert body["aliases"]["platform.user.manage"] == "platform.users.manage"
    assert len(body["items"]) == 255


async def test_role_crud_lifecycle(app_client, db_session) -> None:
    actor, _ = await _seed_actor(db_session, "EGT-RBAC-CRUD")
    created = await app_client.post(
        ROLES_URL,
        headers=actor["headers"],
        json={"code": "exit_gate_role", "name": "Exit Gate Role", "description": "test", "scope_ids": ["items.read"]},
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert role["is_template"] is False and role["is_editable"] is True
    assert role["scopes"] == ["items.read"]
    role_id = role["id"]

    listed = await app_client.get(ROLES_URL, headers=actor["headers"])
    assert listed.status_code == 200
    assert any(item["code"] == "exit_gate_role" for item in listed.json()["items"])
    detail = await app_client.get(f"{ROLES_URL}/{role_id}", headers=actor["headers"])
    assert detail.status_code == 200 and detail.json()["role"]["code"] == "exit_gate_role"

    replaced = await app_client.put(
        f"{ROLES_URL}/{role_id}/permissions",
        headers=actor["headers"],
        json={"scope_ids": ["items.read", "items.update"]},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["diff"]["added"] == ["items.update"]
    patched = await app_client.patch(f"{ROLES_URL}/{role_id}", headers=actor["headers"], json={"name": "Exit Gate Role 2"})
    assert patched.status_code == 200 and patched.json()["name"] == "Exit Gate Role 2"
    deleted = await app_client.request(
        "DELETE", f"{ROLES_URL}/{role_id}", headers=actor["headers"], json={"reason": "test cleanup"}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "INACTIVE"


async def test_reserved_code_and_platform_only_scope_rejected(app_client, db_session) -> None:
    actor, _ = await _seed_actor(db_session, "EGT-RBAC-BOUNDS")
    reserved = await app_client.post(
        ROLES_URL, headers=actor["headers"], json={"code": "admin", "name": "Reserved"}
    )
    assert reserved.status_code == 422 and reserved.json()["code"] == "ROLE_CODE_RESERVED"
    platform_only = await app_client.post(
        ROLES_URL,
        headers=actor["headers"],
        json={"code": "platform_scope_role", "name": "Bad", "scope_ids": ["platform.tenant.manage"]},
    )
    assert platform_only.status_code == 422 and platform_only.json()["code"] == "SCOPE_NOT_ASSIGNABLE"


async def test_system_template_is_clone_only(app_client, db_session) -> None:
    actor, _ = await _seed_actor(db_session, "EGT-RBAC-TPL")
    template_id = await create_role(
        db_session,
        tenant_id=actor["tenant_id"],
        code="planner",
        scopes=(),
        is_template=True,
        is_system=True,
        is_editable=False,
    )
    patched = await app_client.patch(
        f"{ROLES_URL}/{template_id}", headers=actor["headers"], json={"name": "Renamed template"}
    )
    assert patched.status_code == 409 and patched.json()["code"] == "TEMPLATE_IMMUTABLE"
    deleted = await app_client.request("DELETE", f"{ROLES_URL}/{template_id}", headers=actor["headers"])
    assert deleted.status_code == 409 and deleted.json()["code"] == "TEMPLATE_IMMUTABLE"


async def test_assign_unassign_and_effective_permissions(app_client, db_session) -> None:
    actor, target = await _seed_actor(db_session, "EGT-RBAC-ASSIGN")
    readonly_role = await create_role(
        db_session, tenant_id=actor["tenant_id"], code="exit_readonly", scopes=("items.read",),
    )
    assigned = await app_client.post(
        f"/api/v1/admin/users/{target['app_user_id']}/roles",
        headers=actor["headers"],
        json={"role_id": str(readonly_role)},
    )
    assert assigned.status_code == 200, assigned.text
    assert "exit_readonly" in assigned.json()["roles"]

    effective = await app_client.get(
        f"/api/v1/admin/users/{target['app_user_id']}/effective-permissions", headers=actor["headers"]
    )
    assert effective.status_code == 200, effective.text
    assert "items.read" in effective.json()["scopes"]

    unassigned = await app_client.delete(
        f"/api/v1/admin/users/{target['app_user_id']}/roles/{readonly_role}", headers=actor["headers"]
    )
    assert unassigned.status_code == 204
    after = await app_client.get(
        f"/api/v1/admin/users/{target['app_user_id']}/effective-permissions", headers=actor["headers"]
    )
    assert after.status_code == 200 and "items.read" not in after.json()["scopes"]
