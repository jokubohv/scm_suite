"""Fixtures for platform-admin tenant API integration tests (B1 / FS-00).

Mounts only the router under test on a minimal FastAPI app, overrides ``get_db`` with the shared
rollback-isolated ``db_session`` fixture and ``get_current_principal`` with a seeded global platform
administrator (SYSTEM tenant context + ``platform.tenant.manage``). No edits to conftest/main/registry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import platform_admin as platform_admin_router
from app.core.models import platform as p
from app.core.security.tenant_context import SYSTEM_TENANT_ID

PLATFORM_TENANT_ADMIN_SCOPE = "platform.tenant.manage"
SYSTEM_ID = str(SYSTEM_TENANT_ID)


@pytest.fixture()
async def platform_admin_context(db_session: Any) -> dict[str, Any]:
    """Seed (or reuse) the reserved SYSTEM tenant and a global platform-admin identity."""
    system = await db_session.execute(select(p.tenant.c.id).where(p.tenant.c.id == SYSTEM_TENANT_ID))
    if system.scalar_one_or_none() is None:
        await db_session.execute(
            p.tenant.insert().values(
                id=SYSTEM_TENANT_ID,
                tenant_id=SYSTEM_TENANT_ID,
                code="SYSTEM",
                name="System",
                base_currency_code="USD",
                timezone="UTC",
                status="ACTIVE",
                negative_inventory_policy="BLOCK",
                rounding_mode="PER_MOVEMENT",
                retention_ledger_years=7,
                settings={},
                version=1,
            )
        )
    identity_id = uuid4()
    await db_session.execute(
        p.user_identity.insert().values(
            id=identity_id,
            email=f"platform-admin-{identity_id}@example.test",
            display_name="Platform Admin",
            mfa_enabled=False,
            is_platform_admin=True,
            status="ACTIVE",
        )
    )
    await db_session.flush()
    return {"identity_id": identity_id}


@pytest.fixture()
def principal(platform_admin_context: dict[str, Any]) -> Principal:
    return Principal(
        identity_id=str(platform_admin_context["identity_id"]),
        app_user_id=str(uuid4()),
        tenant_id=SYSTEM_ID,
        token_version=0,
        scopes=frozenset({PLATFORM_TENANT_ADMIN_SCOPE}),
    )


@pytest.fixture()
def admin_app(db_session: Any, principal: Principal) -> FastAPI:
    """Minimal app mounting only the platform-admin router under test."""
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(platform_admin_router.router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def api_client(admin_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
