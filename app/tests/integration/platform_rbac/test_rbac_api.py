"""FS-00 RBAC/admin API integration tests (task-39) against the live platform schema."""

from __future__ import annotations

from app.core.security.scopes import ScopeRegistry
from app.tests.integration.platform_rbac.conftest import (
    ADMIN_ROLE_ID,
    APP_USER_ID,
    OTHER_USER_ID,
    READONLY_ROLE_ID,
)


async def test_permissions_catalogue_counts(app_client) -> None:
    response = await app_client.get("/api/v1/admin/permissions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {
        "scopes": 255, "assignable": 241, "service_only": 13, "platform_only": 1,
        "role_templates": 7, "sod_pairs": 14,
    }
    assert body["version"] == "1.5.0"
    assert body["aliases"]["platform.user.manage"] == "platform.users.manage"
    assert len(body["items"]) == 255
    assignable = await app_client.get("/api/v1/admin/permissions", params={"assignable": "true"})
    assert len(assignable.json()["items"]) == 241


async def test_role_crud_lifecycle(app_client) -> None:
    created = await app_client.post(
        "/api/v1/admin/roles",
        json={"code": "custom_a", "name": "Custom A", "description": "test", "scope_ids": ["items.read"]},
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert role["is_template"] is False and role["is_editable"] is True
    role_id = role["id"]
    assert role["scopes"] == ["items.read"]

    listed = await app_client.get("/api/v1/admin/roles")
    assert listed.status_code == 200
    assert any(item["code"] == "custom_a" for item in listed.json()["items"])

    detail = await app_client.get(f"/api/v1/admin/roles/{role_id}")
    assert detail.status_code == 200 and detail.json()["role"]["code"] == "custom_a"

    replaced = await app_client.put(
        f"/api/v1/admin/roles/{role_id}/permissions", json={"scope_ids": ["items.read", "items.update"]}
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["diff"]["added"] == ["items.update"]

    patched = await app_client.patch(f"/api/v1/admin/roles/{role_id}", json={"name": "Custom A2"})
    assert patched.status_code == 200 and patched.json()["name"] == "Custom A2"

    deleted = await app_client.request("DELETE", f"/api/v1/admin/roles/{role_id}", json={"reason": "test cleanup"})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "INACTIVE"


async def test_role_rejects_platform_only_scope(app_client) -> None:
    response = await app_client.post(
        "/api/v1/admin/roles",
        json={"code": "bad_scope", "name": "Bad", "scope_ids": ["platform.tenant.manage"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SCOPE_NOT_ASSIGNABLE"


async def test_role_rejects_sod_pair(app_client) -> None:
    registry = ScopeRegistry()
    pair = registry.catalogue.sod_pairs[0]
    response = await app_client.post(
        "/api/v1/admin/roles",
        json={"code": "sod_role", "name": "SoD", "scope_ids": [pair.a, pair.b]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SOD_CONFLICT"


async def test_last_admin_guard_on_assignment_removal(app_client) -> None:
    response = await app_client.delete(f"/api/v1/admin/users/{APP_USER_ID}/roles/{ADMIN_ROLE_ID}")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "LAST_TENANT_ADMIN"


async def test_user_list_detail_assign_and_effective_permissions(app_client) -> None:
    users = await app_client.get("/api/v1/admin/users")
    assert users.status_code == 200, users.text
    assert len(users.json()["items"]) == 2

    detail = await app_client.get(f"/api/v1/admin/users/{OTHER_USER_ID}")
    assert detail.status_code == 200 and detail.json()["email"] == "user@acme.test"

    assigned = await app_client.post(
        f"/api/v1/admin/users/{OTHER_USER_ID}/roles", json={"role_id": str(READONLY_ROLE_ID)}
    )
    assert assigned.status_code == 200, assigned.text
    assert "readonly" in assigned.json()["roles"]

    effective = await app_client.get(f"/api/v1/admin/users/{OTHER_USER_ID}/effective-permissions")
    assert effective.status_code == 200
    assert "items.read" in effective.json()["scopes"]

    revoked = await app_client.post(
        f"/api/v1/admin/users/{OTHER_USER_ID}/revoke-sessions", json={"reason": "test"}
    )
    assert revoked.status_code == 200 and revoked.json()["token_version"] == 1


async def test_invitations_create_list_revoke(app_client) -> None:
    created = await app_client.post(
        "/api/v1/admin/invitations",
        json={"email": "invitee@acme.test", "intended_role_ids": [str(READONLY_ROLE_ID)]},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["status"] == "PENDING" and payload["token"]
    invitation_id = payload["invitation_id"]

    listed = await app_client.get("/api/v1/admin/invitations")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1 and items[0]["email"] == "invitee@acme.test"
    serialized = str(items)
    assert "token_hash" not in serialized and payload["token"] not in serialized

    revoked = await app_client.post(
        f"/api/v1/admin/invitations/{invitation_id}/revoke", json={"reason": "test"}
    )
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED"
    resend = await app_client.post(f"/api/v1/admin/invitations/{invitation_id}/resend")
    assert resend.status_code == 404


async def test_tenant_settings_and_sod_policy(app_client) -> None:
    settings = await app_client.get("/api/v1/admin/tenant-settings")
    assert settings.status_code == 200 and "items" in settings.json()

    put = await app_client.put(
        "/api/v1/admin/tenant-settings/inventory/default_lead_time_days",
        json={"value": 7, "value_type": "NUMBER"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["value"] == 7

    bad = await app_client.put(
        "/api/v1/admin/tenant-settings/not_a_namespace/key", json={"value": 1, "value_type": "NUMBER"}
    )
    assert bad.status_code == 422 and bad.json()["code"] == "UNKNOWN_SETTING_NAMESPACE"

    policy = await app_client.get("/api/v1/admin/sod-policy")
    assert policy.status_code == 200
    assert policy.json()["sod_enforcement"] == "HARD"
    assert len(policy.json()["pairs"]) == 14

    changed = await app_client.put("/api/v1/admin/sod-policy", json={"sod_enforcement": "WARN", "reason": "test"})
    assert changed.status_code == 200 and changed.json()["sod_enforcement"] == "WARN"
    current = await app_client.get("/api/v1/admin/sod-policy")
    assert current.json()["sod_enforcement"] == "WARN"

    bulk = await app_client.patch(
        "/api/v1/admin/tenant-settings",
        json={"settings": [{"namespace": "wms", "key": "pick_sequence", "value": "FIFO", "value_type": "STRING"}]},
    )
    assert bulk.status_code == 200 and bulk.json()["items"][0]["value"] == "FIFO"


