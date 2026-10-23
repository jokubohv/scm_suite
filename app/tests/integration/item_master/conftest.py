"""Fixtures for FS-01 item-master integration tests (live Postgres via root db_session; skip if absent)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from app.api.deps import get_db, token_service
from app.api.errors import install_error_handlers
from app.api.routers.item_master import router
from app.core.models.platform import app_user, tenant, user_identity

ITEM_SCOPES = frozenset(
    {
        "items.read", "items.create", "items.update", "items.inactivate", "items.uom.manage",
        "items.location.manage", "items.matrix.manage", "items.barcode.manage",
        "items.category.manage", "items.planning.manage",
    }
)


async def create_tenant(session: Any, *, code: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id, tenant_id=tenant_id, code=code, name=f"Tenant {code}", status="ACTIVE", base_currency_code="USD"
        )
    )
    return tenant_id


async def create_member(session: Any, *, tenant_id: uuid.UUID, email: str) -> dict[str, Any]:
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(user_identity).values(id=identity_id, email=email.lower(), display_name="Item Admin", status="ACTIVE")
    )
    await session.execute(
        insert(app_user).values(
            id=user_id, tenant_id=tenant_id, identity_id=identity_id, email=email.lower(),
            status="ACTIVE", membership_status="ACTIVE", token_version=0,
        )
    )
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id), app_user_id=str(user_id), tenant_id=str(tenant_id),
        token_version=0, scopes=ITEM_SCOPES,
    )
    return {"tenant_id": tenant_id, "identity_id": identity_id, "app_user_id": user_id, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture()
def item_app(db_session: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def item_client(item_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=item_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def tenant_ctx(db_session: Any) -> dict[str, Any]:
    tenant_id = await create_tenant(db_session, code=f"FS01-{uuid.uuid4().hex[:8].upper()}")
    return await create_member(db_session, tenant_id=tenant_id, email=f"admin@{uuid.uuid4().hex[:8]}.fs01.test")


@pytest.fixture()
async def second_tenant_ctx(db_session: Any) -> dict[str, Any]:
    tenant_id = await create_tenant(db_session, code=f"FS01B-{uuid.uuid4().hex[:8].upper()}")
    return await create_member(db_session, tenant_id=tenant_id, email=f"admin@{uuid.uuid4().hex[:8]}.fs01b.test")
