"""FS-00 tenant lifecycle extras: offboard (export marker + sessions + audit) and bootstrap-admin."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.models import platform as p
from app.tests.integration.platform_extras.conftest import (
    PLATFORM_ADMIN_SCOPES,
    TENANT_ADMIN_SCOPES,
    ensure_system_tenant,
    principal,
    seed_membership,
    seed_role,
    seed_session,
    seed_tenant,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
async def operator_client(extras_client, monkeypatch):
    """Use the real account-creation guard with an isolated synthetic credential."""
    from app.api import deps
    from app.core.config import get_settings

    token = "synthetic-platform-bootstrap-" + uuid4().hex
    with monkeypatch.context() as patch:
        patch.setenv("SUPER_ADMIN_TOKEN", token)
        patch.setenv("ACCOUNT_CREATION_SUPER_ADMIN_ONLY", "true")
        get_settings.cache_clear()
        deps._settings.cache_clear()
        extras_client.headers["X-Super-Admin-Token"] = token
        try:
            yield extras_client
        finally:
            extras_client.headers.pop("X-Super-Admin-Token", None)
    get_settings.cache_clear()
    deps._settings.cache_clear()


async def test_offboard_transition_export_marker_sessions_and_replay(
    extras_client, db_session, principal_holder
) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "OFFB")
    membership = await seed_membership(db_session, tenant_id)
    await seed_session(db_session, tenant_id, membership)
    from app.tests.integration.platform_extras.conftest import SYSTEM_ID

    platform_admin = principal(SYSTEM_ID, scopes=PLATFORM_ADMIN_SCOPES)
    principal_holder.set(platform_admin)
    headers = {"Idempotency-Key": "offboard-1"}
    body = {"reason": "contract ended", "export": True}
    path = f"/api/v1/platform/tenants/{tenant_id}/offboard"

    first = await extras_client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["state"] == "OFFBOARDING" and payload["status"] == "OFFBOARDING"
    assert payload["sessions_revoked"] == 1
    assert payload["export_job_id"]
    row = (await db_session.execute(select(p.tenant).where(p.tenant.c.id == tenant_id))).mappings().one()
    assert row["status"] == "OFFBOARDING"
    assert row["settings"]["offboarding_export"]["export_job_id"] == payload["export_job_id"]
    assert row["settings"]["offboarding_export"]["export"] is True

    replay = await extras_client.post(path, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["export_job_id"] == payload["export_job_id"]

    # A new key against an already-offboarded tenant is an idempotent no-op.
    again = await extras_client.post(path, json=body, headers={"Idempotency-Key": "offboard-2"})
    assert again.status_code == 200
    assert again.json()["already"] is True


async def test_offboard_guards_and_validation(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "GARD")
    from app.tests.integration.platform_extras.conftest import SYSTEM_ID

    platform_admin = principal(SYSTEM_ID, scopes=PLATFORM_ADMIN_SCOPES)
    principal_holder.set(platform_admin)
    path = f"/api/v1/platform/tenants/{tenant_id}/offboard"
    missing_key = await extras_client.post(path, json={"reason": "contract ended", "export": True})
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    too_short = await extras_client.post(
        path, json={"reason": "x", "export": True}, headers={"Idempotency-Key": "bad-reason"}
    )
    assert too_short.status_code == 422
    missing = await extras_client.post(
        f"/api/v1/platform/tenants/{uuid4()}/offboard",
        json={"reason": "contract ended", "export": True},
        headers={"Idempotency-Key": "missing-tenant"},
    )
    assert missing.status_code == 404 and missing.json()["code"] == "TENANT_NOT_FOUND"
    from app.tests.integration.platform_extras.conftest import SYSTEM_ID

    reserved = await extras_client.post(
        f"/api/v1/platform/tenants/{SYSTEM_ID}/offboard",
        json={"reason": "contract ended", "export": True},
        headers={"Idempotency-Key": "system-tenant"},
    )
    assert reserved.status_code == 409 and reserved.json()["code"] == "RESERVED_TENANT"
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))
    forbidden = await extras_client.post(
        path, json={"reason": "contract ended", "export": True}, headers={"Idempotency-Key": "forbidden"}
    )
    assert forbidden.status_code == 403


async def test_bootstrap_admin_invitation_without_plaintext_token(operator_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "BOOT")
    await seed_membership(db_session, tenant_id, email="owner@boot.example")
    admin_role = await seed_role(db_session, tenant_id, "admin")
    from app.tests.integration.platform_extras.conftest import SYSTEM_ID

    principal_holder.set(principal(SYSTEM_ID, scopes=PLATFORM_ADMIN_SCOPES))
    path = f"/api/v1/platform/tenants/{tenant_id}/bootstrap-admin"
    denied = await operator_client.post(
        path, json={"email": "New.Admin@Boot.Example"},
        headers={"X-Super-Admin-Token": "invalid", "Idempotency-Key": "bootstrap-denied"},
    )
    assert denied.status_code == 401
    assert denied.json()["code"] == "SUPER_ADMIN_TOKEN_INVALID"
    headers = {"Idempotency-Key": "bootstrap-1"}
    response = await operator_client.post(path, json={"email": "New.Admin@Boot.Example"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invitation_id"] and body["expires_at"]
    assert "token" not in response.text.lower()
    invitation = (
        (
            await db_session.execute(
                select(p.tenant_invitation).where(p.tenant_invitation.c.id == UUID(body["invitation_id"]))
            )
        )
        .mappings()
        .one()
    )
    assert invitation["status"] == "PENDING"
    assert invitation["email"] == "new.admin@boot.example"
    assert list(invitation["intended_role_ids"]) == [admin_role]
    assert len(invitation["token_hash"]) == 64

    replay = await operator_client.post(path, json={"email": "New.Admin@Boot.Example"}, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["invitation_id"] == body["invitation_id"]

    invalid_email = await operator_client.post(
        path, json={"email": "not-an-email"}, headers={"Idempotency-Key": "bootstrap-bad-email"}
    )
    assert invalid_email.status_code == 422

    no_inviter_tenant = await seed_tenant(db_session, "NOINV")
    await seed_role(db_session, no_inviter_tenant, "admin")
    conflict = await operator_client.post(
        f"/api/v1/platform/tenants/{no_inviter_tenant}/bootstrap-admin",
        json={"email": "admin@noinv.example"},
        headers={"Idempotency-Key": "bootstrap-no-inviter"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "BOOTSTRAP_ADMIN_INVITER_REQUIRED"


async def test_bootstrap_admin_requires_active_tenant_and_admin_role(
    operator_client, db_session, principal_holder
) -> None:
    await ensure_system_tenant(db_session)
    suspended = await seed_tenant(db_session, "SUSP", status="SUSPENDED")
    await seed_membership(db_session, suspended)
    from app.tests.integration.platform_extras.conftest import SYSTEM_ID

    principal_holder.set(principal(SYSTEM_ID, scopes=PLATFORM_ADMIN_SCOPES))
    response = await operator_client.post(
        f"/api/v1/platform/tenants/{suspended}/bootstrap-admin",
        json={"email": "admin@susp.example"},
        headers={"Idempotency-Key": "bootstrap-suspended"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TENANT_INVALID_STATE"

    active_no_role = await seed_tenant(db_session, "NOROLE")
    await seed_membership(db_session, active_no_role)
    response = await operator_client.post(
        f"/api/v1/platform/tenants/{active_no_role}/bootstrap-admin",
        json={"email": "admin@norole.example"},
        headers={"Idempotency-Key": "bootstrap-no-role"},
    )
    assert response.status_code == 422
