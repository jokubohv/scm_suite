"""Fixtures for FS-14 extension-registry integration tests (live Postgres, graceful skip)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_db
from app.api.errors import install_error_handlers
from app.api.routers.extensions import router
from app.modules.extensions.testing import seed_extensions_world


@pytest.fixture()
def extensions_app(db_session: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def client(extensions_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=extensions_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture()
async def world(db_session: Any) -> dict[str, Any]:
    return await seed_extensions_world(db_session)
