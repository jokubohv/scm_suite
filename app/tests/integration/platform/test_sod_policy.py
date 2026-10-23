"""FS-00 SoD policy: pair rejection, last-admin lockout, privilege escalation, HARD/WARN setting."""

from __future__ import annotations

import pytest

from app.core.security.scopes import ScopeRegistry
from app.tests.integration.platform.support import (
    create_role,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration

ROLES_URL = "/api/v1/admin/roles"
SOD_URL = "/api/v1/admin/sod-policy"


async def test_role_with_sod_pair_rejected(app_client, db_session) -> None:
    actor = await seed_admin_tenant(db_session, code="EGT-SOD-A")
    pair = ScopeRegistry().catalogue.sod_pairs[0]
    response = await app_client.post(
        ROLES_URL,
        headers=actor["headers"],
        json={"code": "sod_conflict_role", "name": "SoD", "scope_ids": [pair.a, pair.b]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SOD_CONFLICT"


async def test_last_active_admin_cannot_lose_admin_role(app_client, db_session) -> None:
    actor = await seed_admin_tenant(db_session, code="EGT-SOD-B")
    response = await app_client.delete(
        f"/api/v1/admin/users/{actor['app_user_id']}/roles/{actor['role_id']}",
        headers=actor["headers"],
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "LAST_TENANT_ADMIN"


async def test_privilege_escalation_grant_scope_not_held_by_actor(app_client, db_session) -> None:
    actor = await seed_admin_tenant(
        db_session,
        code="EGT-SOD-C",
        scopes=("platform.role.manage",),
        role_code="limited_role_manager",
        is_platform_admin=False,
    )
    # Provision a scope the limited actor does not hold so the guard (not provisioning) is exercised.
    await create_role(db_session, tenant_id=actor["tenant_id"], code="provision_read", scopes=("items.read",))
    response = await app_client.post(
        ROLES_URL,
        headers=actor["headers"],
        json={"code": "escalate_role", "name": "Escalate", "scope_ids": ["items.read"]},
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PRIVILEGE_ESCALATION"


async def test_sod_policy_hard_and_warn_mode(app_client, db_session) -> None:
    actor = await seed_admin_tenant(db_session, code="EGT-SOD-D")
    policy = await app_client.get(SOD_URL, headers=actor["headers"])
    assert policy.status_code == 200, policy.text
    body = policy.json()
    assert body["sod_enforcement"] == "HARD"
    assert body["requester_not_approver"] is True
    assert len(body["pairs"]) == 14

    changed = await app_client.put(SOD_URL, headers=actor["headers"], json={"sod_enforcement": "WARN", "reason": "exit gate"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["sod_enforcement"] == "WARN"
    current = await app_client.get(SOD_URL, headers=actor["headers"])
    assert current.json()["sod_enforcement"] == "WARN"

    # Assignment-time SoD remains HARD under WARN (only custom-role-creation warnings may downgrade).
    pair = ScopeRegistry().catalogue.sod_pairs[0]
    rejected = await app_client.post(
        ROLES_URL,
        headers=actor["headers"],
        json={"code": "warn_sod_role", "name": "Warn SoD", "scope_ids": [pair.a, pair.b]},
    )
    assert rejected.status_code == 409 and rejected.json()["code"] == "SOD_CONFLICT"
