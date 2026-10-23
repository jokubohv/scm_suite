"""Integration tests for the platform-admin tenant API (live Postgres; graceful skip via fixtures)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.api.deps import Principal, get_current_principal
from app.core.models import platform as p
from app.core.security.tenant_context import SYSTEM_TENANT_ID

SYSTEM_ID = str(SYSTEM_TENANT_ID)


def _body(code: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "name": f"Tenant {code}",
        "timezone": "UTC",
        "base_currency_code": "USD",
    }
    payload.update(overrides)
    return payload


def _code() -> str:
    return f"T{uuid4().hex[:10].upper()}"


async def _create(client: Any, *, code: str | None = None, key: str | None = None, **overrides: Any) -> dict[str, Any]:
    code = code or _code()
    response = await client.post(
        "/api/v1/platform/tenants",
        json=_body(code, **overrides),
        headers={"Idempotency-Key": key or uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _tenant_row(db_session: Any, tenant_id: str) -> Any:
    result = await db_session.execute(select(p.tenant).where(p.tenant.c.id == tenant_id))
    return result.mappings().first()


async def _audit_rows(db_session: Any, tenant_id: str) -> list[Any]:
    result = await db_session.execute(select(p.audit_event).where(p.audit_event.c.resource_id == str(tenant_id)))
    return list(result.mappings().all())


async def _seed_member(
    db_session: Any,
    tenant_id: str,
    *,
    membership_status: str = "ACTIVE",
    role_code: str | None = None,
    is_platform_admin: bool = False,
) -> dict[str, str]:
    identity_id = uuid4()
    app_user_id = uuid4()
    role_id = uuid4()
    await db_session.execute(
        p.user_identity.insert().values(
            id=identity_id,
            email=f"member-{identity_id}@example.test",
            display_name="Member User",
            mfa_enabled=False,
            is_platform_admin=is_platform_admin,
            status="ACTIVE",
        )
    )
    await db_session.execute(
        p.app_user.insert().values(
            id=app_user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"member-{identity_id}@example.test",
            display_name="Member User",
            status="ACTIVE",
            membership_status=membership_status,
            token_version=0,
        )
    )
    await db_session.execute(
        p.role.insert().values(
            id=role_id,
            tenant_id=tenant_id,
            code=role_code or f"role-{uuid4().hex[:8]}",
            name="Test Role",
            is_system=False,
            is_template=False,
            is_editable=True,
        )
    )
    await db_session.execute(p.user_role.insert().values(tenant_id=tenant_id, user_id=app_user_id, role_id=role_id))
    await db_session.flush()
    return {"identity_id": str(identity_id), "app_user_id": str(app_user_id), "role_id": str(role_id)}


async def _seed_session(db_session: Any, tenant_id: str, member: dict[str, str]) -> None:
    now = datetime.now(UTC)
    await db_session.execute(
        p.user_session.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            app_user_id=member["app_user_id"],
            identity_id=member["identity_id"],
            refresh_token_hash=f"hash-{uuid4().hex}",
            status="ACTIVE",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    await db_session.flush()


# --------------------------------------------------------------------------- create ------------
async def test_create_tenant_persists_row_and_system_audit(api_client: Any, db_session: Any) -> None:
    body = await _create(api_client, code="ACME", base_currency_code="USD")
    row = await _tenant_row(db_session, body["tenant_id"])
    assert row is not None
    assert row["code"] == "ACME"
    assert row["status"] == "ACTIVE"
    assert row["tenant_id"] == row["id"]
    assert row["base_currency_code"] == "USD"
    assert body["tenant"]["is_system"] is False

    audits = await _audit_rows(db_session, body["tenant_id"])
    assert len(audits) == 1
    assert audits[0]["action"] == "tenant.provisioned"
    assert str(audits[0]["tenant_id"]) == SYSTEM_ID
    assert audits[0]["resource_type"] == "tenant"


async def test_create_duplicate_code_conflicts(api_client: Any) -> None:
    await _create(api_client, code="DUP1")
    response = await api_client.post(
        "/api/v1/platform/tenants",
        json=_body("dup1"),
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TENANT_CODE_CONFLICT"


async def test_create_is_idempotent_per_key(api_client: Any, db_session: Any) -> None:
    code = _code()
    key = uuid4().hex
    first = await _create(api_client, code=code, key=key)
    second = await _create(api_client, code=code, key=key)
    assert second["tenant_id"] == first["tenant_id"]
    count = await db_session.execute(select(func.count()).select_from(p.tenant).where(p.tenant.c.code == code))
    assert int(count.scalar_one()) == 1
    audits = await _audit_rows(db_session, first["tenant_id"])
    assert len(audits) == 1


async def test_same_key_different_payload_conflicts(api_client: Any) -> None:
    key = uuid4().hex
    await _create(api_client, code=_code(), key=key)
    response = await api_client.post(
        "/api/v1/platform/tenants",
        json=_body(_code(), name="Different"),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_create_validation_and_currency_rules(api_client: Any) -> None:
    bad_timezone = await api_client.post(
        "/api/v1/platform/tenants",
        json=_body(_code(), timezone="Mars/Olympus_Mons"),
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert bad_timezone.status_code == 422
    assert bad_timezone.json()["code"] == "VALIDATION_FAILED"

    unknown_currency = await api_client.post(
        "/api/v1/platform/tenants",
        json=_body(_code(), base_currency_code="ZZZ"),
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert unknown_currency.status_code == 422

    missing_key = await api_client.post("/api/v1/platform/tenants", json=_body(_code()))
    assert missing_key.status_code == 422


# --------------------------------------------------------------------------- list / detail -----
async def test_list_excludes_system_and_filters(api_client: Any) -> None:
    first = await _create(api_client, code=_code())
    second = await _create(api_client, code=_code(), status="SUSPENDED")

    listing = await api_client.get("/api/v1/platform/tenants", params={"limit": 200})
    assert listing.status_code == 200
    ids = [item["id"] for item in listing.json()["items"]]
    assert SYSTEM_ID not in ids
    assert first["tenant_id"] in ids and second["tenant_id"] in ids

    suspended = await api_client.get("/api/v1/platform/tenants", params={"status": "SUSPENDED", "limit": 200})
    assert {item["id"] for item in suspended.json()["items"]} >= {second["tenant_id"]}
    assert first["tenant_id"] not in {item["id"] for item in suspended.json()["items"]}

    by_code = await api_client.get("/api/v1/platform/tenants", params={"code": first["code"]})
    assert [item["id"] for item in by_code.json()["items"]] == [first["tenant_id"]]

    invalid = await api_client.get("/api/v1/platform/tenants", params={"status": "BOGUS"})
    assert invalid.status_code == 422


async def test_list_pagination(api_client: Any) -> None:
    created: list[str] = []
    for _ in range(3):
        created.append((await _create(api_client))["tenant_id"])
    page = await api_client.get("/api/v1/platform/tenants", params={"limit": 2})
    body = page.json()
    assert len(body["items"]) <= 2
    assert body["next_cursor"]
    second = await api_client.get("/api/v1/platform/tenants", params={"limit": 2, "cursor": body["next_cursor"]})
    assert second.status_code == 200
    seen = {item["id"] for item in body["items"]} | {item["id"] for item in second.json()["items"]}
    assert set(created) <= seen

    bad_cursor = await api_client.get("/api/v1/platform/tenants", params={"cursor": "!!!"})
    assert bad_cursor.status_code == 422


async def test_detail_membership_count_and_404(api_client: Any, db_session: Any) -> None:
    created = await _create(api_client)
    await _seed_member(db_session, created["tenant_id"])
    detail = await api_client.get(f"/api/v1/platform/tenants/{created['tenant_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["tenant"]["id"] == created["tenant_id"]
    assert body["membership_count"] == 1
    assert body["settings_summary"]["negative_inventory_policy"] == "BLOCK"

    missing = await api_client.get(f"/api/v1/platform/tenants/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "TENANT_NOT_FOUND"


# --------------------------------------------------------------------------- patch -------------
async def test_patch_profile_and_optimistic_version(api_client: Any) -> None:
    created = await _create(api_client)
    patched = await api_client.patch(
        f"/api/v1/platform/tenants/{created['tenant_id']}",
        json={"name": "Renamed Tenant", "rounding_mode": "PER_TRANSACTION"},
        headers={"If-Match": str(created["tenant"]["version"])},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["tenant"]["name"] == "Renamed Tenant"
    assert body["tenant"]["rounding_mode"] == "PER_TRANSACTION"
    assert body["tenant"]["version"] == created["tenant"]["version"] + 1
    assert body["changed_fields"] == ["name", "rounding_mode"]

    stale = await api_client.patch(
        f"/api/v1/platform/tenants/{created['tenant_id']}",
        json={"name": "Stale Update"},
        headers={"If-Match": str(created["tenant"]["version"])},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "STALE_VERSION"


async def test_patch_rejects_immutable_and_unknown_fields(api_client: Any) -> None:
    created = await _create(api_client)
    response = await api_client.patch(f"/api/v1/platform/tenants/{created['tenant_id']}", json={"code": "NEWCODE"})
    assert response.status_code == 422
    unknown = await api_client.patch(f"/api/v1/platform/tenants/{created['tenant_id']}", json={"nope": 1})
    assert unknown.status_code == 422
    empty = await api_client.patch(f"/api/v1/platform/tenants/{created['tenant_id']}", json={})
    assert empty.status_code == 422


async def test_patch_status_transitions_and_terminal_offboarding(api_client: Any) -> None:
    created = await _create(api_client)
    suspended = await api_client.patch(f"/api/v1/platform/tenants/{created['tenant_id']}", json={"status": "SUSPENDED"})
    assert suspended.status_code == 200
    assert suspended.json()["tenant"]["status"] == "SUSPENDED"
    offboarded = await api_client.patch(
        f"/api/v1/platform/tenants/{created['tenant_id']}", json={"status": "OFFBOARDING"}
    )
    assert offboarded.status_code == 200
    assert offboarded.json()["tenant"]["status"] == "OFFBOARDING"
    invalid = await api_client.patch(f"/api/v1/platform/tenants/{created['tenant_id']}", json={"status": "ACTIVE"})
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "INVALID_STATE_TRANSITION"

    reserved = await api_client.patch(f"/api/v1/platform/tenants/{SYSTEM_ID}", json={"name": "Nope"})
    assert reserved.status_code == 409
    assert reserved.json()["code"] == "SYSTEM_TENANT_RESERVED"


# --------------------------------------------------------------------------- lifecycle --------
async def test_suspend_revokes_sessions_and_is_idempotent(api_client: Any, db_session: Any) -> None:
    created = await _create(api_client)
    tenant_id = created["tenant_id"]
    member = await _seed_member(db_session, tenant_id)
    await _seed_session(db_session, tenant_id, member)

    suspended = await api_client.post(
        f"/api/v1/platform/tenants/{tenant_id}/suspend",
        json={"reason": "customer request"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert suspended.status_code == 200
    body = suspended.json()
    assert body["status"] == "SUSPENDED"
    assert body["sessions_revoked"] == 1
    assert body["already"] is False

    row = await db_session.execute(select(p.user_session.c.status).where(p.user_session.c.tenant_id == tenant_id))
    assert row.scalar_one() == "REVOKED"
    audits = await _audit_rows(db_session, tenant_id)
    assert any(audit["action"] == "tenant.suspended" for audit in audits)
    assert all(str(audit["tenant_id"]) == SYSTEM_ID for audit in audits)

    replay = await api_client.post(
        f"/api/v1/platform/tenants/{tenant_id}/suspend",
        json={"reason": "customer request"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert replay.status_code == 200
    assert replay.json()["already"] is True

    reactivated = await api_client.post(
        f"/api/v1/platform/tenants/{tenant_id}/reactivate",
        json={"reason": "payment received"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == "ACTIVE"

    terminal = await _create(api_client)
    await api_client.patch(f"/api/v1/platform/tenants/{terminal['tenant_id']}", json={"status": "OFFBOARDING"})
    rejected = await api_client.post(
        f"/api/v1/platform/tenants/{terminal['tenant_id']}/reactivate",
        json={"reason": "too late"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert rejected.status_code == 409


# --------------------------------------------------------------------------- memberships ------
async def test_memberships_projection_has_roles_and_no_secrets(api_client: Any, db_session: Any) -> None:
    created = await _create(api_client)
    member = await _seed_member(db_session, created["tenant_id"], role_code="admin")
    await _seed_member(db_session, created["tenant_id"], membership_status="INVITED", role_code="readonly")

    response = await api_client.get(f"/api/v1/platform/tenants/{created['tenant_id']}/memberships")
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_code"] == created["code"]
    assert len(body["items"]) == 2
    first = next(item for item in body["items"] if item["app_user_id"] == member["app_user_id"])
    assert first["roles"] == ["admin"]
    assert first["membership_status"] == "ACTIVE"
    assert "password_hash" not in first
    assert "refresh_token_hash" not in first

    invited = await api_client.get(
        f"/api/v1/platform/tenants/{created['tenant_id']}/memberships",
        params={"membership_status": "INVITED"},
    )
    assert invited.status_code == 200
    assert [item["membership_status"] for item in invited.json()["items"]] == ["INVITED"]

    missing = await api_client.get(f"/api/v1/platform/tenants/{uuid4()}/memberships")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- auth guards ------
async def test_platform_admin_flag_is_required(api_client: Any, db_session: Any, platform_admin_context: Any) -> None:
    await db_session.execute(
        p.user_identity.update()
        .where(p.user_identity.c.id == platform_admin_context["identity_id"])
        .values(is_platform_admin=False)
    )
    await db_session.flush()
    response = await api_client.get("/api/v1/platform/tenants")
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_PLATFORM_ADMIN"


async def test_system_tenant_context_is_required(admin_app: Any) -> None:
    import httpx

    other = Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(uuid4()),
        token_version=0,
        scopes=frozenset({"platform.tenant.manage"}),
    )
    admin_app.dependency_overrides[get_current_principal] = lambda: other
    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/platform/tenants")
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_PLATFORM_ADMIN"


async def test_no_hard_delete_route(api_client: Any) -> None:
    created = await _create(api_client)
    response = await api_client.delete(f"/api/v1/platform/tenants/{created['tenant_id']}")
    assert response.status_code == 405
