"""FS-00 Δ-MT auth flows: login→me→refresh→logout, wrong password, selection, stale tv, suspended."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.core.models.platform import app_user
from app.tests.integration.platform.support import (
    TEST_PASSWORD,
    auth_headers,
    create_identity,
    create_membership,
    create_tenant,
)

pytestmark = pytest.mark.integration


async def _seed_member(db_session, *, code: str, email: str, **membership_overrides):
    tenant_id = await create_tenant(db_session, code=code, status=membership_overrides.pop("tenant_status", "ACTIVE"))
    identity_id = await create_identity(db_session, email=email)
    membership = await create_membership(
        db_session,
        tenant_id=tenant_id,
        identity_id=identity_id,
        email=email,
        scopes=("platform.admin", "items.read"),
        **membership_overrides,
    )
    return tenant_id, identity_id, membership


async def _login(client, email: str, password: str = TEST_PASSWORD, **extra):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password, **extra})


async def test_login_me_refresh_logout_flow(app_client, db_session) -> None:
    tenant_id, identity_id, membership = await _seed_member(db_session, code="EGT-AUTH-A", email="admin@egt-auth-a.test")
    login = await _login(app_client, "admin@egt-auth-a.test")
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] > 0
    assert body["tenant"]["code"] == "EGT-AUTH-A"
    access, refresh = body["access_token"], body["refresh_token"]

    me = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200, me.text
    me_body = me.json()
    assert me_body["identity"]["email"] == "admin@egt-auth-a.test"
    assert me_body["tenant"]["id"] == str(tenant_id)
    assert "items.read" in me_body["permissions"]["scopes"]
    assert me_body["membership"]["app_user_id"] == str(membership["app_user_id"])

    rotated = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200, rotated.text
    new_tokens = rotated.json()
    assert new_tokens["access_token"] != access and new_tokens["refresh_token"] != refresh
    # Replay of the already-rotated refresh token revokes the session (old and new tokens unusable).
    replay = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401 and replay.json()["code"] == "SESSION_REVOKED"
    after = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]})
    assert after.status_code == 401 and after.json()["code"] == "SESSION_REVOKED"

    fresh = (await _login(app_client, "admin@egt-auth-a.test")).json()
    logout = await app_client.post("/api/v1/auth/logout", json={"refresh_token": fresh["refresh_token"]})
    assert logout.status_code == 204
    revoked = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert revoked.status_code == 401 and revoked.json()["code"] == "SESSION_REVOKED"


async def test_wrong_password_and_unknown_email_are_generic_401(app_client, db_session) -> None:
    await _seed_member(db_session, code="EGT-AUTH-B", email="user@egt-auth-b.test")
    wrong = await _login(app_client, "user@egt-auth-b.test", password="definitely-wrong")
    assert wrong.status_code == 401 and wrong.json()["code"] == "INVALID_CREDENTIALS"
    unknown = await _login(app_client, "nobody@egt-auth-b.test")
    assert unknown.status_code == 401 and unknown.json()["code"] == "INVALID_CREDENTIALS"


async def test_multi_tenant_selection_then_select(app_client, db_session) -> None:
    first = await create_tenant(db_session, code="EGT-AUTH-C1")
    second = await create_tenant(db_session, code="EGT-AUTH-C2")
    identity_id = await create_identity(db_session, email="multi@egt-auth.test")
    await create_membership(
        db_session, tenant_id=first, identity_id=identity_id, email="multi@egt-auth.test",
        role_code="readonly", scopes=("items.read",),
    )
    await create_membership(
        db_session, tenant_id=second, identity_id=identity_id, email="multi@egt-auth.test",
        role_code="planner", scopes=("items.read", "planning.run"),
    )

    login = await _login(app_client, "multi@egt-auth.test")
    assert login.status_code == 200, login.text
    selection = login.json()
    assert selection["status"] == "TENANT_SELECTION_REQUIRED"
    assert "access_token" not in selection and selection["expires_in"] == 300
    assert [choice["code"] for choice in selection["tenant_choices"]] == ["EGT-AUTH-C1", "EGT-AUTH-C2"]

    selected = await app_client.post(
        "/api/v1/auth/select-tenant",
        json={"selection_token": selection["selection_token"], "tenant_id": str(second)},
    )
    assert selected.status_code == 200, selected.text
    pair = selected.json()
    assert pair["tenant"]["id"] == str(second)

    invalid = await app_client.post(
        "/api/v1/auth/select-tenant",
        json={"selection_token": "not-a-valid-selection-token", "tenant_id": str(second)},
    )
    assert invalid.status_code == 401 and invalid.json()["code"] == "SELECTION_TOKEN_INVALID"


async def test_stale_token_version_is_rejected_on_me(app_client, db_session) -> None:
    _, _, membership = await _seed_member(db_session, code="EGT-AUTH-D", email="stale@egt-auth.test")
    login = (await _login(app_client, "stale@egt-auth.test")).json()
    await db_session.execute(
        update(app_user)
        .where(app_user.c.id == membership["app_user_id"])
        .values(token_version=app_user.c.token_version + 1)
    )
    me = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {login['access_token']}"})
    assert me.status_code == 401 and me.json()["code"] == "TOKEN_VERSION_STALE"


async def test_inactive_membership_and_suspended_tenant_denied(app_client, db_session) -> None:
    await _seed_member(db_session, code="EGT-AUTH-E", email="suspended@egt-auth.test", membership_status="SUSPENDED")
    suspended = await _login(app_client, "suspended@egt-auth.test")
    assert suspended.status_code == 403 and suspended.json()["code"] == "MEMBERSHIP_INACTIVE"

    await _seed_member(db_session, code="EGT-AUTH-F", email="tenant-status@egt-auth.test", tenant_status="SUSPENDED")
    tenant_suspended = await _login(app_client, "tenant-status@egt-auth.test")
    assert tenant_suspended.status_code == 403 and tenant_suspended.json()["code"] == "TENANT_SUSPENDED"

    identity_id = await create_identity(db_session, email="no-member@egt-auth.test")
    del identity_id
    no_member = await _login(app_client, "no-member@egt-auth.test")
    assert no_member.status_code == 403 and no_member.json()["code"] == "NO_ACTIVE_MEMBERSHIP"


async def test_me_tenants_and_effective_permissions_self_service(app_client, db_session) -> None:
    first = await create_tenant(db_session, code="EGT-AUTH-G1")
    second = await create_tenant(db_session, code="EGT-AUTH-G2")
    identity_id = await create_identity(db_session, email="self@egt-auth.test")
    membership = await create_membership(
        db_session, tenant_id=first, identity_id=identity_id, email="self@egt-auth.test", scopes=("items.read",),
    )
    await create_membership(
        db_session, tenant_id=second, identity_id=identity_id, email="self@egt-auth.test",
        membership_status="SUSPENDED", scopes=("items.read",),
    )
    headers = auth_headers(
        identity_id=identity_id, app_user_id=membership["app_user_id"], tenant_id=first, scopes={"items.read"},
    )
    tenants = await app_client.get("/api/v1/me/tenants", headers=headers)
    assert tenants.status_code == 200, tenants.text
    codes = [item["tenant_code"] for item in tenants.json()["items"]]
    assert codes == ["EGT-AUTH-G1", "EGT-AUTH-G2"]
    effective = await app_client.get("/api/v1/me/effective-permissions", headers=headers)
    assert effective.status_code == 200, effective.text
    assert "items.read" in effective.json()["scopes"]
