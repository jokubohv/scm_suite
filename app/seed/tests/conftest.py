"""Fixtures for the live-DB seed repository tests (task-48).

Integration tests are skipped — never failed — when Postgres is unavailable (Docker/stack recovery).
Each test gets a throw-away tenant so tenant-scoped rows cannot collide with other suites; the fixture
deletes all seeded rows for that tenant plus the tenant row afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory"


def resolve_live_database_url() -> str:
    for env_var in ("TEST_DATABASE_URL", "SEED_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(env_var)
        if value:
            return value
    return DEFAULT_URL


async def _probe(url: str) -> None:
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    return resolve_live_database_url()


@pytest.fixture(scope="session")
def db_available(database_url: str) -> bool:
    try:
        asyncio.run(_probe(database_url))
    except Exception as exc:  # noqa: BLE001 - environment boundary
        pytest.skip(f"live database unavailable at {database_url.rsplit('@', 1)[-1]}: {exc}")
    return True


@pytest.fixture()
async def live_tenant(database_url: str, db_available: bool) -> AsyncIterator[dict[str, object]]:
    """Create a fresh tenant row; yield ``{tenant_id, code}``; delete seeded rows + tenant afterwards."""
    tenant_id = uuid4()
    code = "SEED" + uuid4().hex[:8].upper()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO platform.tenant (id, tenant_id, code, name, base_currency_code, timezone, status)
                VALUES (:id, :id, :code, :name, 'USD', 'UTC', 'ACTIVE')
                """
            ),
            {"id": tenant_id, "code": code, "name": f"Seed Test {code}"},
        )
    try:
        yield {"tenant_id": tenant_id, "code": code}
    finally:
        async with engine.begin() as connection:
            for table in (
                "platform.tenant_setting",
                "platform.feature_flag",
                "platform.number_sequence",
                "platform.role_permission",
                "platform.permission_scope",
                "platform.role",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant"), {"tenant": tenant_id}
                )
            await connection.execute(text("DELETE FROM platform.tenant WHERE id = :tenant"), {"tenant": tenant_id})
        await engine.dispose()


@pytest.fixture()
async def db_counter(database_url: str) -> AsyncIterator[object]:
    """Return an async helper ``count(table, tenant_id=None)`` for live assertions."""
    engine = create_async_engine(database_url, pool_pre_ping=True)

    async def count(table: str, tenant_id: UUID | None = None) -> int:
        sql = f"SELECT count(*) FROM {table}"
        params: dict[str, object] = {}
        if tenant_id is not None:
            sql += " WHERE tenant_id = :tenant"
            params["tenant"] = tenant_id
        async with engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return int(result.scalar_one())

    try:
        yield count
    finally:
        await engine.dispose()
