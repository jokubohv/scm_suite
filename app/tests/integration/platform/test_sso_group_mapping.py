"""FS-00 Δ-SSO group→role mapping bounds: exact/regex/priority, assignable-only, SoD, lifecycle."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.core.models.platform import sso_group_role_map
from app.tests.integration.platform.support import (
    SSO_SCOPES,
    activate_sso_connection,
    create_platform_scope_role,
    create_role,
    create_sso_connection,
    dex_claims,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration


async def _mapping_connection(app_client, db_session):
    tenant = await seed_admin_tenant(db_session, code="EGT-MAP-A", scopes=tuple(SSO_SCOPES))
    roles = {
        "lead": await create_role(
            db_session, tenant_id=tenant["tenant_id"], code="lead", scopes=("platform.sso.read",)
        ),
        "po_creator": await create_role(
            db_session, tenant_id=tenant["tenant_id"], code="po_creator", scopes=("procurement.po.create",)
        ),
        "po_approver": await create_role(
            db_session, tenant_id=tenant["tenant_id"], code="po_approver", scopes=("procurement.po.approve",)
        ),
        "platform_scope": await create_platform_scope_role(
            db_session, tenant_id=tenant["tenant_id"], code="platform_scope_role"
        ),
    }
    connection = await create_sso_connection(app_client, tenant["headers"], email_domains=["map.example"])
    return tenant, roles, connection


async def test_mapping_validation_bounds(app_client, db_session) -> None:
    tenant, roles, connection = await _mapping_connection(app_client, db_session)
    base = f"/api/v1/admin/sso-connections/{connection['id']}/group-mappings"

    unknown = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "EXACT", "group_value": "ghost", "role_key": "no_such_role"}]},
    )
    assert unknown.status_code == 422 and unknown.json()["code"] == "SSO_GROUP_MAPPING_INVALID"

    platform_scope = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "EXACT", "group_value": "platform", "role_key": "platform_scope_role"}]},
    )
    assert platform_scope.status_code == 422 and platform_scope.json()["code"] == "SSO_GROUP_MAPPING_INVALID"

    bad_regex = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "REGEX", "group_value": "[unterminated", "role_key": "lead"}]},
    )
    assert bad_regex.status_code == 422 and bad_regex.json()["code"] == "SSO_GROUP_MAPPING_INVALID"

    negative_priority = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "EXACT", "group_value": "leads", "role_key": "lead", "priority": -1}]},
    )
    assert negative_priority.status_code == 422

    sod = await app_client.put(
        base,
        headers=tenant["headers"],
        json={
            "mappings": [
                {"match_type": "EXACT", "group_value": "po-creators", "role_key": "po_creator"},
                {"match_type": "EXACT", "group_value": "po-approvers", "role_key": "po_approver"},
            ]
        },
    )
    assert sod.status_code == 409 and sod.json()["code"] == "SOD_CONFLICT"


async def test_mapping_exact_regex_priority_and_lifecycle(app_client, db_session) -> None:
    tenant, roles, connection = await _mapping_connection(app_client, db_session)
    base = f"/api/v1/admin/sso-connections/{connection['id']}/group-mappings"
    created = await app_client.put(
        base,
        headers=tenant["headers"],
        json={
            "mappings": [
                {"match_type": "EXACT", "group_value": "leads", "role_key": "lead", "priority": 20},
                {"match_type": "REGEX", "group_value": "^inv-.*", "role_key": "po_creator", "priority": 10},
            ]
        },
    )
    assert created.status_code == 200, created.text
    items = created.json()["items"]
    assert {item["match_type"] for item in items} == {"EXACT", "REGEX"}
    assert {item["role_key"] for item in items} == {"lead", "po_creator"}
    assert {item["priority"] for item in items} == {10, 20}
    stored = await db_session.execute(select(func.count()).select_from(sso_group_role_map))
    assert int(stored.scalar_one()) == 2

    fetched = await app_client.get(base, headers=tenant["headers"])
    assert fetched.status_code == 200
    assert len(fetched.json()["items"]) == 2

    # PUT is a replace: the previous mappings are removed, not accumulated.
    replaced = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "EXACT", "group_value": "leads", "role_key": "lead", "priority": 100000}]},
    )
    assert replaced.status_code == 200, replaced.text
    assert len(replaced.json()["items"]) == 1
    assert replaced.json()["items"][0]["priority"] == 100000
    after = await db_session.execute(select(func.count()).select_from(sso_group_role_map))
    assert int(after.scalar_one()) == 1


async def test_group_mapping_applied_during_sso_callback(app_client, platform_app, db_session) -> None:
    tenant, roles, connection = await _mapping_connection(app_client, db_session)
    # The callback evaluator resolves group mappings against the RBAC catalogue's role templates, so
    # seed a template-backed role (custom-role mappings are validated on write but not resolvable there;
    # reported separately as an app gap).
    template_role = await create_role(
        db_session,
        tenant_id=tenant["tenant_id"],
        code="readonly",
        scopes=("items.read",),
        is_template=True,
        is_system=True,
        is_editable=False,
    )
    base = f"/api/v1/admin/sso-connections/{connection['id']}/group-mappings"
    configured = await app_client.put(
        base,
        headers=tenant["headers"],
        json={"mappings": [{"match_type": "EXACT", "group_value": "leads", "role_key": "readonly"}]},
    )
    assert configured.status_code == 200, configured.text
    await activate_sso_connection(app_client, tenant["headers"], connection["id"])

    started = await app_client.post("/api/v1/auth/sso/start", json={"email_or_tenant": "user@map.example"})
    assert started.status_code == 200, started.text
    params = parse_qs(urlparse(started.json()["authorization_url"]).query)
    platform_app.state.dex_client.claims = dex_claims(
        nonce=params["nonce"][0], email="user@map.example", groups=["leads", "unmapped-group"]
    )
    callback = await app_client.get(
        "/api/v1/auth/sso/callback", params={"code": "code", "state": params["state"][0]}
    )
    assert callback.status_code == 200, callback.text
    tokens = callback.json()
    assert "items.read" in tokens["scopes"]

    effective = await app_client.get(
        "/api/v1/me/effective-permissions", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert effective.status_code == 200, effective.text
    assert "items.read" in effective.json()["scopes"]
    assert template_role is not None and roles["lead"] is not None
