"""Local DB fixture for reporting tests (module-scoped tests are outside app/tests/conftest.py)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

DEFAULT_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory"


@pytest.fixture()
async def db_session() -> AsyncIterator[Any]:
    sqlalchemy = pytest.importorskip("sqlalchemy.ext.asyncio")
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_URL
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = sqlalchemy.create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = sqlalchemy.AsyncSession(bind=connection)
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"reporting integration database unavailable: {type(exc).__name__}")
    finally:
        await engine.dispose()
