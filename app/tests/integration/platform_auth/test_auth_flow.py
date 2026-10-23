"""FS-00 local auth/session endpoint integration tests (live DB; skip when the DB is unavailable)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.api.routers.platform.auth import get_password_service
from app.core.models.platform import app_user, tenant, tenant_invitation, user_identity
from app.core.security.passwords import PasswordPolicy, PasswordService
from app.tests.integration.platform_auth.support import (
    TEST_PASSWORD,
    auth_headers,
    create_identity,
    create_invitation,
    create_membership,
    create_role,
    create_sso_connection,
    create_tenant,
)

pytestmark = pytest.mark.integration


async def _login(client, email: str, password: str = TEST_PASSWORD, tenant_selector: str | None = None):
    payload: dict[str, str] = {"email": email, "password": password}
    if tenant_selector:
        payload["tenant"] = tenant_selector
    return await client.post("/api/v1/auth/login", json=payload)


async def test_login_me_refresh_logout_flow(db_session, auth_client, tokens) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-A")
    identity_id = await create_identity(db_session, email="admin@ten-a.example")
    await create_membership(
        db_session,
        tenant_id=tenant_id,
        identity_id=identity_id,
        email="admin@ten-a.example",
        scopes=("platform.admin", "items.read", "items.create"),
    )

    login = await _login(auth_client, "admin@ten-a.example")
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    assert body["tenant"]["code"] == "TEN-A"
    access_token, refresh_token = body["access_token"], body["refresh_token"]
    claims = tokens.decode_access_token(access_token)
    assert claims.sub == str(identity_id)
    assert claims.tid == str(tenant_id)
    assert claims.tv == 0
    assert "items.read" in claims.scopes

    me = await auth_client.get("/api/v1/me", headers=auth_headers(access_token))
    assert me.status_code == 200, me.text
    me_body = me.json()
    assert me_body["identity"]["email"] == "admin@ten-a.example"
    assert me_body["tenant"]["code"] == "TEN-A"
    assert me_body["membership"]["membership_status"] == "ACTIVE"
    assert me_body["permissions"]["scopes"] == ["items.create", "items.read", "platform.admin"]
    assert me_body["membership"]["roles"] == ["admin"]

    refresh = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 200, refresh.text
    refreshed = refresh.json()
    assert refreshed["access_token"] != access_token
    assert refreshed["refresh_token"] != refresh_token
    assert refreshed["token_type"] == "Bearer"

    # Replaying the already-rotated refresh token revokes the whole session.
    replay = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert replay.status_code == 401
    assert replay.json()["code"] == "SESSION_REVOKED"
    # ...so the freshly rotated token is now unusable too.
    after_replay = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": refreshed["refresh_token"]})
    assert after_replay.status_code == 401
    assert after_replay.json()["code"] == "SESSION_REVOKED"

    # Logout revokes the presented session; its refresh token can no longer be used.
    second = await _login(auth_client, "admin@ten-a.example")
    second_tokens = second.json()
    logout = await auth_client.post("/api/v1/auth/logout", json={"refresh_token": second_tokens["refresh_token"]})
    assert logout.status_code == 204
    revoked = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": second_tokens["refresh_token"]})
    assert revoked.status_code == 401
    assert revoked.json()["code"] == "SESSION_REVOKED"


async def test_wrong_password_and_unknown_email_are_generic_401(db_session, auth_client) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-B")
    identity_id = await create_identity(db_session, email="user@ten-b.example")
    await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="user@ten-b.example")

    wrong = await _login(auth_client, "user@ten-b.example", password="wrong-password-value")
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "INVALID_CREDENTIALS"
    unknown = await _login(auth_client, "nobody@ten-b.example")
    assert unknown.status_code == 401
    assert unknown.json()["code"] == "INVALID_CREDENTIALS"


async def test_inactive_membership_and_suspended_tenant_denied(db_session, auth_client) -> None:
    active_tenant = await create_tenant(db_session, code="TEN-C")
    suspended_tenant = await create_tenant(db_session, code="TEN-D", status="SUSPENDED")
    suspended_identity = await create_identity(db_session, email="suspended@ten-c.example")
    await create_membership(
        db_session,
        tenant_id=active_tenant,
        identity_id=suspended_identity,
        email="suspended@ten-c.example",
        membership_status="SUSPENDED",
    )
    tenant_identity = await create_identity(db_session, email="member@ten-d.example")
    await create_membership(
        db_session,
        tenant_id=suspended_tenant,
        identity_id=tenant_identity,
        email="member@ten-d.example",
    )
    await create_identity(db_session, email="nomembership@example.com")

    suspended = await _login(auth_client, "suspended@ten-c.example")
    assert suspended.status_code == 403
    assert suspended.json()["code"] == "MEMBERSHIP_INACTIVE"
    tenant_suspended = await _login(auth_client, "member@ten-d.example")
    assert tenant_suspended.status_code == 403
    assert tenant_suspended.json()["code"] == "TENANT_SUSPENDED"
    none = await _login(auth_client, "nomembership@example.com")
    assert none.status_code == 403
    assert none.json()["code"] == "NO_ACTIVE_MEMBERSHIP"


async def test_multi_tenant_selection_and_select_tenant(db_session, auth_client, tokens) -> None:
    first = await create_tenant(db_session, code="TEN-E")
    second = await create_tenant(db_session, code="TEN-F")
    identity_id = await create_identity(db_session, email="multi@example.com")
    await create_membership(
        db_session, tenant_id=first, identity_id=identity_id, email="multi@example.com", role_code="readonly", scopes=("items.read",)
    )
    await create_membership(
        db_session, tenant_id=second, identity_id=identity_id, email="multi@example.com", role_code="planner", scopes=("items.read", "planning.run")
    )

    login = await _login(auth_client, "multi@example.com")
    assert login.status_code == 200, login.text
    selection = login.json()
    assert selection["status"] == "TENANT_SELECTION_REQUIRED"
    assert selection["expires_in"] == 300
    assert "access_token" not in selection
    assert [choice["code"] for choice in selection["tenant_choices"]] == ["TEN-E", "TEN-F"]

    chosen = await auth_client.post(
        "/api/v1/auth/select-tenant",
        json={"selection_token": selection["selection_token"], "tenant_id": str(second)},
    )
    assert chosen.status_code == 200, chosen.text
    pair = chosen.json()
    assert pair["tenant"]["code"] == "TEN-F"
    claims = tokens.decode_access_token(pair["access_token"])
    assert claims.tid == str(second)
    assert "planning.run" in claims.scopes

    invalid = await auth_client.post(
        "/api/v1/auth/select-tenant",
        json={"selection_token": "not-a-selection-token", "tenant_id": str(second)},
    )
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "SELECTION_TOKEN_INVALID"


async def test_stale_token_version_and_post_issuance_suspension(db_session, auth_client) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-G")
    identity_id = await create_identity(db_session, email="stale@ten-g.example")
    app_user_id = await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="stale@ten-g.example")

    login = await _login(auth_client, "stale@ten-g.example")
    access_token = login.json()["access_token"]
    await db_session.execute(
        update(app_user).where(app_user.c.id == app_user_id).values(token_version=app_user.c.token_version + 1)
    )
    stale = await auth_client.get("/api/v1/me", headers=auth_headers(access_token))
    assert stale.status_code == 401
    assert stale.json()["code"] == "TOKEN_VERSION_STALE"

    fresh_login = await _login(auth_client, "stale@ten-g.example")
    fresh_token = fresh_login.json()["access_token"]
    await db_session.execute(
        update(app_user).where(app_user.c.id == app_user_id).values(membership_status="SUSPENDED")
    )
    inactive = await auth_client.get("/api/v1/me", headers=auth_headers(fresh_token))
    assert inactive.status_code == 403
    assert inactive.json()["code"] == "MEMBERSHIP_INACTIVE"

    await db_session.execute(update(app_user).where(app_user.c.id == app_user_id).values(membership_status="ACTIVE"))
    relog = await _login(auth_client, "stale@ten-g.example")
    relog_token = relog.json()["access_token"]
    await db_session.execute(update(tenant).where(tenant.c.id == tenant_id).values(status="SUSPENDED"))
    suspended = await auth_client.get("/api/v1/me", headers=auth_headers(relog_token))
    assert suspended.status_code == 403
    assert suspended.json()["code"] == "TENANT_SUSPENDED"


async def test_rehash_on_login_updates_stored_hash(db_session, auth_app, auth_client) -> None:
    weak = PasswordService(PasswordPolicy(time_cost=1, memory_cost_kib=8192, parallelism=1)).hash(TEST_PASSWORD)
    stronger = PasswordService(PasswordPolicy(time_cost=2, memory_cost_kib=8192, parallelism=1))
    auth_app.dependency_overrides[get_password_service] = lambda: stronger
    tenant_id = await create_tenant(db_session, code="TEN-H")
    identity_id = await create_identity(db_session, email="rehash@ten-h.example", password_hash=weak)
    await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="rehash@ten-h.example")

    login = await _login(auth_client, "rehash@ten-h.example")
    assert login.status_code == 200, login.text
    stored = (
        await db_session.execute(select(user_identity.c.password_hash).where(user_identity.c.id == identity_id))
    ).scalar_one()
    assert stored != weak
    assert stronger.verify(TEST_PASSWORD, stored) is True
    assert stronger.needs_rehash(stored) is False


async def test_providers_discovery_local_and_sso(db_session, auth_client) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-I")
    await create_sso_connection(db_session, tenant_id=tenant_id, email_domains=("ten-i.example",), is_enforced=False)

    discovery = await auth_client.get("/api/v1/auth/providers", params={"email_or_tenant": "member@ten-i.example"})
    assert discovery.status_code == 200, discovery.text
    body = discovery.json()
    assert body["methods"] == ["LOCAL", "SSO"]
    assert body["local_allowed"] is True
    assert len(body["connections"]) == 1
    connection = body["connections"][0]
    assert connection["tenant_code"] == "TEN-I"
    assert connection["protocol"] == "OIDC"
    assert connection["login_url"].endswith("/auth")

    local_only = await auth_client.get("/api/v1/auth/providers", params={"email_or_tenant": "someone@unknown.example"})
    assert local_only.status_code == 200
    assert local_only.json()["methods"] == ["LOCAL"]
    assert local_only.json()["connections"] == []

    await create_sso_connection(
        db_session,
        tenant_id=tenant_id,
        name="Enforced IdP",
        issuer="https://sso-enforced.auth.example.com",
        email_domains=("enforced.example",),
        is_enforced=True,
    )
    enforced = await auth_client.get("/api/v1/auth/providers", params={"email_or_tenant": "member@enforced.example"})
    assert enforced.status_code == 200
    enforced_body = enforced.json()
    assert enforced_body["local_allowed"] is False
    assert enforced_body["methods"] == ["SSO"]


async def test_self_service_memberships_and_effective_permissions(db_session, auth_client) -> None:
    first = await create_tenant(db_session, code="TEN-J")
    second = await create_tenant(db_session, code="TEN-K")
    identity_id = await create_identity(db_session, email="self@example.com")
    await create_membership(
        db_session, tenant_id=first, identity_id=identity_id, email="self@example.com", role_code="admin", scopes=("platform.admin", "items.read")
    )
    await create_membership(
        db_session,
        tenant_id=second,
        identity_id=identity_id,
        email="self@example.com",
        role_code="readonly",
        membership_status="SUSPENDED",
        scopes=("items.read",),
    )
    login = await _login(auth_client, "self@example.com", tenant_selector="TEN-J")
    assert login.status_code == 200, login.text
    access_token = login.json()["access_token"]

    memberships = await auth_client.get("/api/v1/me/tenants", headers=auth_headers(access_token))
    assert memberships.status_code == 200, memberships.text
    items = memberships.json()["items"]
    assert [item["tenant_code"] for item in items] == ["TEN-J", "TEN-K"]
    assert items[0]["membership_status"] == "ACTIVE"
    assert items[0]["roles"] == ["admin"]
    assert items[1]["membership_status"] == "SUSPENDED"

    effective = await auth_client.get("/api/v1/me/effective-permissions", headers=auth_headers(access_token))
    assert effective.status_code == 200, effective.text
    effective_body = effective.json()
    assert effective_body["app_user_id"]
    assert effective_body["roles"] == ["admin"]
    assert "items.read" in effective_body["scopes"]
    assert "platform.admin" in effective_body["scopes"]
    assert isinstance(effective_body["data_scopes"], list)


async def test_logout_all_sessions_bumps_token_version(db_session, auth_client) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-L")
    identity_id = await create_identity(db_session, email="logout@ten-l.example")
    app_user_id = await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="logout@ten-l.example")
    first = (await _login(auth_client, "logout@ten-l.example")).json()
    second = (await _login(auth_client, "logout@ten-l.example")).json()

    logout = await auth_client.post(
        "/api/v1/auth/logout",
        headers=auth_headers(first["access_token"]),
        json={"all_sessions": True},
    )
    assert logout.status_code == 204
    for pair in (first, second):
        refreshed = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert refreshed.status_code == 401
        assert refreshed.json()["code"] == "SESSION_REVOKED"
    token_version = (
        await db_session.execute(select(app_user.c.token_version).where(app_user.c.id == app_user_id))
    ).scalar_one()
    assert token_version == 1


async def test_invitation_accept_creates_identity_membership_and_tokens(db_session, auth_client, tokens) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-M")
    inviter_identity = await create_identity(db_session, email="inviter@ten-m.example")
    inviter_app_user = await create_membership(
        db_session, tenant_id=tenant_id, identity_id=inviter_identity, email="inviter@ten-m.example"
    )
    role_id = await create_role(db_session, tenant_id=tenant_id, code="invitee", scopes=("items.read",))
    invitation_id = await create_invitation(
        db_session,
        tenant_id=tenant_id,
        email="newinvitee@ten-m.example",
        invited_by=inviter_app_user,
        role_ids=(role_id,),
        raw_token="invite-token-new-user",
    )

    accepted = await auth_client.post(
        "/api/v1/admin/invitations/invite-token-new-user/accept",
        json={"password": TEST_PASSWORD, "display_name": "New Invitee"},
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["token_type"] == "Bearer"
    claims = tokens.decode_access_token(body["access_token"])
    assert claims.uid == body["app_user_id"]
    assert claims.tid == str(tenant_id)
    assert "items.read" in claims.scopes

    status = (
        await db_session.execute(select(tenant_invitation.c.status).where(tenant_invitation.c.id == invitation_id))
    ).scalar_one()
    assert status == "ACCEPTED"
    reused = await auth_client.post(
        "/api/v1/admin/invitations/invite-token-new-user/accept",
        json={"password": TEST_PASSWORD},
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "INVITATION_ALREADY_USED"

    login = await _login(auth_client, "newinvitee@ten-m.example")
    assert login.status_code == 200
    assert login.json()["tenant"]["code"] == "TEN-M"


async def test_invitation_accept_existing_identity_and_error_states(db_session, auth_client) -> None:
    tenant_id = await create_tenant(db_session, code="TEN-N")
    inviter_identity = await create_identity(db_session, email="inviter@ten-n.example")
    inviter_app_user = await create_membership(
        db_session, tenant_id=tenant_id, identity_id=inviter_identity, email="inviter@ten-n.example"
    )
    existing_identity = await create_identity(db_session, email="existing@ten-n.example")
    role_id = await create_role(db_session, tenant_id=tenant_id, code="reader", scopes=("items.read",))
    await create_invitation(
        db_session,
        tenant_id=tenant_id,
        email="existing@ten-n.example",
        invited_by=inviter_app_user,
        role_ids=(role_id,),
        raw_token="invite-token-existing",
    )
    accepted = await auth_client.post(
        "/api/v1/admin/invitations/invite-token-existing/accept",
        json={},
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["identity_id"] == str(existing_identity)
    assert len(body["access_token"]) > 20

    expired_invitation = await create_invitation(
        db_session,
        tenant_id=tenant_id,
        email="expired@ten-n.example",
        invited_by=inviter_app_user,
        role_ids=(role_id,),
        raw_token="invite-token-expired",
        expires_in_hours=-1,
    )
    expired = await auth_client.post(
        "/api/v1/admin/invitations/invite-token-expired/accept",
        json={"password": TEST_PASSWORD},
    )
    assert expired.status_code == 410
    assert expired.json()["code"] == "INVITATION_EXPIRED"
    expired_status = (
        await db_session.execute(select(tenant_invitation.c.status).where(tenant_invitation.c.id == expired_invitation))
    ).scalar_one()
    assert expired_status == "EXPIRED"

    await create_invitation(
        db_session,
        tenant_id=tenant_id,
        email="revoked@ten-n.example",
        invited_by=inviter_app_user,
        role_ids=(role_id,),
        raw_token="invite-token-revoked",
        status="REVOKED",
    )
    revoked = await auth_client.post(
        "/api/v1/admin/invitations/invite-token-revoked/accept",
        json={"password": TEST_PASSWORD},
    )
    assert revoked.status_code == 409
    assert revoked.json()["code"] == "INVITATION_REVOKED"

    invalid = await auth_client.post(
        "/api/v1/admin/invitations/unknown-token/accept",
        json={"password": TEST_PASSWORD},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVITATION_TOKEN_INVALID"
