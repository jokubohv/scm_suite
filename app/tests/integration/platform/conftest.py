"""Fixtures for the FS-00 exit-gate integration/platform suites.

Builds the **canonical** app (all routers mounted), overrides only the DB dependency with the shared
rollback-isolated ``db_session`` fixture and injects the deterministic fake Dex client + in-memory SSO
state store. Each suite seeds its own tenant/identity data via ``support.py``; the whole directory skips
gracefully when no Postgres is available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_db
from app.api.main import create_app
from app.api.routers.platform.auth import get_password_service
from app.api.routers.platform.sso import InMemorySsoStateStore
from app.tests.integration.platform.support import FakeDex, cheap_password_service


@pytest.fixture()
def platform_app(db_session: Any) -> FastAPI:
    app = create_app()
    app.state.sso_state_store = InMemorySsoStateStore()
    app.state.dex_client = FakeDex()

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_password_service] = cheap_password_service
    return app


@pytest.fixture()
async def app_client(platform_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=platform_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
