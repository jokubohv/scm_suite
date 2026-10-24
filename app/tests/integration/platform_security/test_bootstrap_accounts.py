"""Live-DB bootstrap account creation tests (operator token only)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db
from app.api.main import create_app
from app.core.config import get_settings
from app.core.models import platform as p

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_bootstrap_tenant_user_and_public_guard(db_session, monkeypatch) -> None:
    token = "bootstrap-test-token-" + uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setenv("SUPER_ADMIN_TOKEN", token)
    monkeypatch.setenv("ACCOUNT_CREATION_SUPER_ADMIN_ONLY", "true")
    get_settings.cache_clear()
    from app.api import deps
    deps._settings.cache_clear()

    app = create_app()

    async def _db_override():
        yield db_session

    app.dependency_overrides[get_db] = _db_override
    suffix = uuid.uuid4().hex[:8].upper()
    tenant_code = f"BT{suffix}"
    email = f"bootstrap-{suffix.lower()}@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing/invalid token must never create anything.
        anon = await client.post(
            "/api/v1/bootstrap/tenants",
            json={"code": tenant_code, "name": "Bootstrap Tenant", "admin_email": email,
                  "admin_password": "bootstrap-password-123"},
        )
        assert anon.status_code == 401
        wrong = await client.post(
            "/api/v1/bootstrap/tenants",
            headers={"X-Super-Admin-Token": "wrong"},
            json={"code": tenant_code, "name": "Bootstrap Tenant", "admin_email": email,
                  "admin_password": "bootstrap-password-123"},
        )
        assert wrong.status_code == 401
        who = await client.get("/api/v1/bootstrap/whoami", headers={"X-Super-Admin-Token": token})
        assert who.status_code == 200 and who.json()["enabled"] is True

        created = await client.post(
            "/api/v1/bootstrap/tenants",
            headers={"X-Super-Admin-Token": token, "Idempotency-Key": f"bootstrap-{suffix}"},
            json={"code": tenant_code, "name": "Bootstrap Tenant", "admin_email": email,
                  "admin_password": "bootstrap-password-123", "admin_display_name": "Bootstrap Admin"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["rbac"]["scopes"] > 0 and body["rbac"]["roles"] == 7
        assert body["admin"]["email"] == email
        tenant_id = uuid.UUID(body["tenant_id"])
        assert await db_session.scalar(select(p.tenant.c.id).where(p.tenant.c.id == tenant_id)) == tenant_id

        duplicate = await client.post(
            "/api/v1/bootstrap/tenants",
            headers={"X-Super-Admin-Token": token},
            json={"code": tenant_code, "name": "Bootstrap Tenant", "admin_email": email,
                  "admin_password": "bootstrap-password-123"},
        )
        assert duplicate.status_code == 409

        second_email = f"bootstrap-two-{suffix.lower()}@example.com"
        user = await client.post(
            "/api/v1/bootstrap/users",
            headers={"X-Super-Admin-Token": token},
            json={"tenant_id": str(tenant_id), "email": second_email, "display_name": "Second",
                  "password": "bootstrap-password-456", "role_codes": ["warehouse_operator"]},
        )
        assert user.status_code == 201, user.text
        assert user.json()["user"]["role_codes"] == ["warehouse_operator"]

        # Ordinary tenant-admin creation routes are closed without the operator token in hardened mode.
        admin_route = await client.post(
            "/api/v1/admin/users",
            json={"email": "blocked@example.com"},
        )
        assert admin_route.status_code in (401, 403)
