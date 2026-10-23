"""Fixtures for FS-00 reference/config API integration tests (task-41).

Uses the shared harness ``db_session`` fixture (outer-transaction rollback; graceful skip when no
database is configured). The test app mounts only the router under test, overrides ``get_db`` with the
shared rollback session and ``get_current_principal`` with a seeded principal — no edits to
``app/api/main.py``/deps/conftest. The router is registered at commit time by the integrator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import config as config_router
from app.core.models import platform as p

TENANT_ID = UUID("7a000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("7a000000-0000-7000-8000-000000000002")
IDENTITY_ID = UUID("7a000000-0000-7000-8000-000000000011")
APP_USER_ID = UUID("7a000000-0000-7000-8000-000000000012")
OTHER_APP_USER_ID = UUID("7a000000-0000-7000-8000-000000000013")

# Exactly the catalogued scopes required by the router (design/permissions.json).
CONFIG_SCOPES = frozenset(
    {
        "platform.reference.read",
        "platform.finance.read",
        "platform.finance.sync",
        "platform.customfield.manage",
        "platform.flag.manage",
        "platform.sequence.manage",
    }
)


async def _seed_tenant(session: Any, tenant_id: UUID, code: str, name: str) -> None:
    await session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=name,
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
        )
    )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    """Seed the primary + isolation tenant rows required by the FK constraints."""
    await _seed_tenant(db_session, TENANT_ID, "CFG1", "Config Tenant")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "CFG2", "Other Config Tenant")
    await db_session.flush()
    return {"tenant_id": str(TENANT_ID), "other_tenant_id": str(OTHER_TENANT_ID)}


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(IDENTITY_ID),
        app_user_id=str(APP_USER_ID),
        tenant_id=str(TENANT_ID),
        token_version=0,
        scopes=CONFIG_SCOPES,
    )


@pytest.fixture()
def other_principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(OTHER_APP_USER_ID),
        tenant_id=str(OTHER_TENANT_ID),
        token_version=0,
        scopes=CONFIG_SCOPES,
    )


@pytest.fixture()
def app_with_config_router(seeded: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(config_router.router)
    return app


@pytest.fixture()
def client_factory(app_with_config_router: FastAPI, db_session: Any) -> Callable[[Principal], Any]:
    """Build an HTTP client for a given principal (empty scope set = scope-denial test)."""

    @asynccontextmanager
    async def factory(principal: Principal) -> AsyncIterator[Any]:
        import httpx

        async def _get_db() -> AsyncIterator[Any]:
            yield db_session

        app_with_config_router.dependency_overrides[get_db] = _get_db
        app_with_config_router.dependency_overrides[get_current_principal] = lambda: principal
        transport = httpx.ASGITransport(app=app_with_config_router)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    return factory


@pytest.fixture()
async def app_client(client_factory: Callable[[Principal], Any], principal: Principal) -> AsyncIterator[Any]:
    async with client_factory(principal) as client:
        yield client
