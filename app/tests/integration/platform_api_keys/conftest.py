"""Fixtures for FS-00 API-key API integration tests (task-43).

Shared DB harness (outer-transaction rollback, graceful skip); test app mounts only the router under
test and overrides deps — no edits to conftest/main/registry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import api_keys as api_keys_router
from app.core.models import platform as p

TENANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_TENANT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
IDENTITY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
APP_USER_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

API_KEY_SCOPES = frozenset({"platform.admin"})


@pytest.fixture()
async def seeded_api_keys(db_session: Any) -> dict[str, Any]:
    for tenant_id, code, name in ((TENANT_ID, "ACME", "Acme"), (OTHER_TENANT_ID, "OTHER", "Other")):
        await db_session.execute(
            p.tenant.insert().values(
                id=tenant_id, tenant_id=tenant_id, code=code, name=name, base_currency_code="USD",
                timezone="UTC", status="ACTIVE", negative_inventory_policy="BLOCK", rounding_mode="PER_MOVEMENT",
            )
        )
    await db_session.execute(
        p.user_identity.insert().values(
            id=IDENTITY_ID, email="admin@acme.test", display_name="Acme Admin", status="ACTIVE", is_platform_admin=True,
        )
    )
    await db_session.execute(
        p.app_user.insert().values(
            id=APP_USER_ID, tenant_id=TENANT_ID, identity_id=IDENTITY_ID, email="admin@acme.test",
            display_name="Acme Admin", status="ACTIVE", membership_status="ACTIVE", token_version=0,
        )
    )
    await db_session.flush()
    return {"tenant_id": TENANT_ID, "other_tenant_id": OTHER_TENANT_ID}


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(IDENTITY_ID), app_user_id=str(APP_USER_ID), tenant_id=str(TENANT_ID),
        token_version=0, scopes=API_KEY_SCOPES,
    )


@pytest.fixture()
async def app_client(db_session: Any, seeded_api_keys: dict[str, Any], principal: Principal) -> AsyncIterator[Any]:
    import httpx

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(api_keys_router.router)

    async def _get_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
