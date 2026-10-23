"""FS-15 reporting API integration fixtures: live DB session + mounted reporting router (graceful skip)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.reporting.router import router

READ_SCOPES = frozenset({"reporting.read", "reporting.cost.view", "reporting.export"})
TENANT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture()
def reporting_principal() -> Principal:
    return Principal(identity_id=str(UUID(int=1)), app_user_id=None, tenant_id=TENANT_ID,
                     token_version=0, scopes=READ_SCOPES)


@pytest.fixture()
async def reporting_client(db_session: Any, reporting_principal: Principal) -> AsyncIterator[Any]:
    import httpx
    from sqlalchemy import text

    if not await db_session.scalar(text("SELECT to_regclass('reporting.v_stock_ledger') IS NOT NULL")):
        pytest.skip("reporting views not applied yet")
    from fastapi import FastAPI

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _get_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: reporting_principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
