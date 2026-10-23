"""Fixtures for FS-13 consigned-inventory integration tests (live Postgres, graceful skip)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_db
from app.api.errors import install_error_handlers
from app.api.routers.consigned import router
from app.modules.consigned.testing import seed_consigned_world


@pytest.fixture()
def consigned_app(db_session: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def client(consigned_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=consigned_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture()
async def world(db_session: Any) -> dict[str, Any]:
    return await seed_consigned_world(db_session)
