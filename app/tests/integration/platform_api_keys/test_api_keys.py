"""FS-00 API-key lifecycle integration tests (task-43) against the live platform schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.models import platform as p
from app.core.repositories.api_keys import derive_api_key_pepper
from app.core.security.scopes import ScopeRegistry
from app.core.security.tokens import ApiKeyHasher
from app.tests.integration.platform_api_keys.conftest import OTHER_TENANT_ID, TENANT_ID


async def _rows(db_session):
    result = await db_session.execute(select(p.api_key).where(p.api_key.c.tenant_id == TENANT_ID))
    return [dict(row) for row in result.mappings().all()]


async def test_create_list_rotate_revoke_lifecycle(app_client, db_session) -> None:
    created = await app_client.post(
        "/api/v1/admin/api-keys",
        json={"name": "ci-bot", "scopes": ["items.read", "items.update"],
              "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["name"] == "ci-bot" and payload["scopes"] == ["items.read", "items.update"]
    assert payload["key_prefix"] and payload["secret"].startswith(payload["key_prefix"] + ".")
    assert "secret_hash" not in payload and "revoked" in payload
    key_id = payload["id"]
    old_secret = payload["secret"]

    rows = await _rows(db_session)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["secret_hash"] != old_secret.split(".", 1)[1]
    assert old_secret.split(".", 1)[1] not in stored["secret_hash"]
    assert ApiKeyHasher(derive_api_key_pepper("dev-only-change-me-32-bytes-minimum!!")).verify(
        old_secret, stored["secret_hash"])

    listed = await app_client.get("/api/v1/admin/api-keys")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1 and "secret" not in items[0] and "secret_hash" not in items[0]

    rotated = await app_client.post(f"/api/v1/admin/api-keys/{key_id}/rotate")
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["secret"]
    assert new_secret != old_secret and rotated.json()["id"] == key_id
    stored_after = (await _rows(db_session))[0]
    hasher = ApiKeyHasher(derive_api_key_pepper("dev-only-change-me-32-bytes-minimum!!"))
    assert hasher.verify(new_secret, stored_after["secret_hash"]) is True
    assert hasher.verify(old_secret, stored_after["secret_hash"]) is False  # old secret invalid immediately

    revoked = await app_client.delete(f"/api/v1/admin/api-keys/{key_id}")
    assert revoked.status_code == 204
    again = await app_client.delete(f"/api/v1/admin/api-keys/{key_id}")
    assert again.status_code == 204  # idempotent
    revoked_list = await app_client.get("/api/v1/admin/api-keys", params={"include_revoked": "true"})
    assert revoked_list.json()["items"][0]["revoked"] is True
    assert (await app_client.get("/api/v1/admin/api-keys")).json()["items"] == []
    assert (await app_client.post(f"/api/v1/admin/api-keys/{key_id}/rotate")).status_code == 404
    assert (await app_client.delete("/api/v1/admin/api-keys/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_scope_validation_rejects_invalid_platform_service_sod(app_client) -> None:
    platform_only = await app_client.post(
        "/api/v1/admin/api-keys", json={"name": "bad", "scopes": ["platform.tenant.manage"]})
    assert platform_only.status_code == 422 and platform_only.json()["code"] == "SCOPE_NOT_ASSIGNABLE"

    unknown = await app_client.post("/api/v1/admin/api-keys", json={"name": "bad", "scopes": ["ghost.scope.read"]})
    assert unknown.status_code == 422 and unknown.json()["code"] == "UNKNOWN_SCOPE"

    service_only = next(s.scope for s in ScopeRegistry().catalogue.scopes if s.service_only)
    service = await app_client.post("/api/v1/admin/api-keys", json={"name": "bad", "scopes": [service_only]})
    assert service.status_code == 422 and service.json()["code"] == "SCOPE_NOT_ASSIGNABLE"

    pair = ScopeRegistry().catalogue.sod_pairs[0]
    sod = await app_client.post("/api/v1/admin/api-keys", json={"name": "bad", "scopes": [pair.a, pair.b]})
    assert sod.status_code == 409 and sod.json()["code"] == "SOD_CONFLICT"


async def test_expiry_validation_and_filters(app_client, db_session) -> None:
    past = await app_client.post(
        "/api/v1/admin/api-keys",
        json={"name": "past", "scopes": ["items.read"],
              "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    assert past.status_code == 422

    created = await app_client.post("/api/v1/admin/api-keys", json={"name": "short", "scopes": ["items.read"]})
    assert created.status_code == 201, created.text
    key_id = created.json()["id"]
    # Force-expire directly (clock is server-side) and exercise the filter.
    from sqlalchemy import update
    await db_session.execute(
        update(p.api_key).where(p.api_key.c.id == key_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.flush()
    without_expired = await app_client.get("/api/v1/admin/api-keys", params={"include_expired": "false"})
    assert without_expired.status_code == 200 and without_expired.json()["items"] == []


async def test_repository_is_tenant_scoped(app_client, db_session) -> None:
    created = await app_client.post("/api/v1/admin/api-keys", json={"name": "scoped", "scopes": ["items.read"]})
    assert created.status_code == 201
    key_id = created.json()["id"]
    rows = await db_session.execute(
        select(p.api_key).where(p.api_key.c.tenant_id == OTHER_TENANT_ID, p.api_key.c.id == key_id))
    assert rows.mappings().first() is None
