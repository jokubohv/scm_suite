"""FS-00 Δ-MT invitation accept + session lifecycle: rotation/replay, logout-all, switch-tenant."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.models.platform import tenant_invitation
from app.tests.integration.platform.support import (
    TEST_PASSWORD,
    create_identity,
    create_invitation,
    create_membership,
    create_role,
    create_tenant,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration


async def _login(client, email: str, *, tenant: str | None = None):
    payload = {"email": email, "password": TEST_PASSWORD}
    if tenant:
        payload["tenant"] = tenant
    return await client.post("/api/v1/auth/login", json=payload)


async def test_invitation_accept_new_identity_creates_membership_and_tokens(app_client, db_session) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-INV-A")
    invitee_role = await create_role(
        db_session, tenant_id=admin["tenant_id"], code="invitee", scopes=("items.read",),
    )
    invitation_id = await create_invitation(
        db_session,
        tenant_id=admin["tenant_id"],
        email="new-invitee@egt-inv.test",
        invited_by=admin["app_user_id"],
        role_ids=(invitee_role,),
        raw_token="exit-gate-invitation-new",
    )
    accepted = await app_client.post(
        "/api/v1/admin/invitations/exit-gate-invitation-new/accept",
        json={"password": TEST_PASSWORD, "display_name": "New Invitee"},
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["tenant_id"] == str(admin["tenant_id"])
    assert body["token_type"] == "Bearer" and body["access_token"]

    # The invitation is single-use.
    reused = await app_client.post(
        "/api/v1/admin/invitations/exit-gate-invitation-new/accept", json={"password": TEST_PASSWORD}
    )
    assert reused.status_code == 409 and reused.json()["code"] == "INVITATION_ALREADY_USED"
    status = (
        await db_session.execute(select(tenant_invitation.c.status).where(tenant_invitation.c.id == invitation_id))
    ).scalar_one()
    assert status == "ACCEPTED"

    # The created identity can log in and sees the intended role's scopes on /me.
    login = await _login(app_client, "new-invitee@egt-inv.test")
    assert login.status_code == 200, login.text
    access = login.json()["access_token"]
    me = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200, me.text
    assert "items.read" in me.json()["permissions"]["scopes"]
    assert "invitee" in me.json()["membership"]["roles"]


async def test_invitation_accept_existing_identity_without_password(app_client, db_session) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-INV-B")
    existing = await create_identity(db_session, email="existing-invitee@egt-inv.test")
    role_id = await create_role(db_session, tenant_id=admin["tenant_id"], code="reader", scopes=("items.read",))
    await create_invitation(
        db_session,
        tenant_id=admin["tenant_id"],
        email="existing-invitee@egt-inv.test",
        invited_by=admin["app_user_id"],
        role_ids=(role_id,),
        raw_token="exit-gate-invitation-existing",
    )
    accepted = await app_client.post(
        "/api/v1/admin/invitations/exit-gate-invitation-existing/accept", json={}
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["identity_id"] == str(existing)


async def test_refresh_rotation_and_replay_revokes_session(app_client, db_session) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-SESS-A")
    login = await _login(app_client, admin["email"])
    assert login.status_code == 200, login.text
    tokens = login.json()
    rotated = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]

    replay = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401 and replay.json()["code"] == "SESSION_REVOKED"
    after_replay = await app_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert after_replay.status_code == 401 and after_replay.json()["code"] == "SESSION_REVOKED"


async def test_logout_all_revokes_sessions_and_bumps_token_version(app_client, db_session) -> None:
    from sqlalchemy import select

    from app.core.models.platform import app_user

    admin = await seed_admin_tenant(db_session, code="EGT-SESS-B")
    first = (await _login(app_client, admin["email"])).json()
    second = (await _login(app_client, admin["email"])).json()
    logout = await app_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {first['access_token']}"},
        json={"all_sessions": True},
    )
    assert logout.status_code == 204
    for tokens in (first, second):
        refreshed = await app_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert refreshed.status_code == 401 and refreshed.json()["code"] == "SESSION_REVOKED"
    token_version = (
        await db_session.execute(
            select(app_user.c.token_version).where(app_user.c.id == admin["app_user_id"])
        )
    ).scalar_one()
    assert token_version == 1


async def test_switch_tenant_issues_pair_for_other_active_membership(app_client, db_session) -> None:
    first = await create_tenant(db_session, code="EGT-SESS-C1")
    second = await create_tenant(db_session, code="EGT-SESS-C2")
    identity_id = await create_identity(db_session, email="switcher@egt-sess.test")
    first_membership = await create_membership(
        db_session, tenant_id=first, identity_id=identity_id, email="switcher@egt-sess.test",
        role_code="readonly", scopes=("items.read",),
    )
    await create_membership(
        db_session, tenant_id=second, identity_id=identity_id, email="switcher@egt-sess.test",
        role_code="planner", scopes=("items.read", "planning.run"),
    )
    login = await _login(app_client, "switcher@egt-sess.test", tenant="EGT-SESS-C1")
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert tokens["tenant"]["id"] == str(first)
    switched = await app_client.post(
        "/api/v1/auth/switch-tenant",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"tenant_id": str(second)},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["tenant"]["id"] == str(second)
    del first_membership
