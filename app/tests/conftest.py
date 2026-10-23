"""Top-level pytest harness (S0-05).

Provides deterministic fixtures shared by every feature-set test suite:

* ``settings`` — test ``Settings`` instance (env=test, non-secret placeholders)
* ``clock`` / ``fixed_now`` / ``uuids`` — deterministic time and UUIDv7 helpers
* ``tenant`` / ``tenant_id`` / ``system_tenant_id`` — tenant fixtures
* ``rbac_catalogue`` / ``principal`` / ``principal_readonly`` / ``principal_factory`` — RBAC principals
* ``app_instance`` / ``sync_client`` / ``client`` — FastAPI app + test clients (skip if app is missing)
* ``db_config`` / ``requires_db`` / ``db_engine`` / ``db_session`` / ``db_reset`` — async SQLAlchemy
  Postgres test database with transaction-rollback isolation and graceful skip when unavailable

Database profile selection (see ``app/tests/utils/db.py`` and ``app/tests/README.md``):
``TEST_DB_PROFILE=auto|url|testcontainers|compose|skip``; ``TEST_DATABASE_URL`` selects an existing DB.
Integration tests that need a real database should request ``db_session`` (or ``requires_db``) and are
skipped — never failed — when the environment cannot provide one.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.core.config import Settings, get_settings
from app.tests.utils.context import TenantContext, bind_context
from app.tests.utils.db import (
    DatabaseUnavailable,
    DbProfileConfig,
    check_connection,
    describe,
    ensure_database_url,
    resolve_db_profile,
)
from app.tests.utils.deterministic import FIXED_EPOCH, DeterministicUUIDs, FixedClock, uuid7_at
from app.tests.utils.factories import (
    ItemSample,
    LedgerPostingSample,
    LocationSample,
    Principal,
    Tenant,
    make_item,
    make_ledger_posting,
    make_location,
    make_principal,
    make_status,
    make_tenant,
    make_uom_pair,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_TEST_ENV_VARS = ("ENV", "APP_NAME", "LOG_LEVEL")


# ---------------------------------------------------------------------------
# Session environment
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _test_environment() -> Iterator[None]:
    """Force a test environment and reset the settings cache for all tests."""
    previous = {key: os.environ.get(key) for key in _TEST_ENV_VARS}
    os.environ["ENV"] = "test"
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture()
def settings() -> Settings:
    """Deterministic test settings (no .env file, non-secret placeholders)."""
    return Settings(
        env="test",
        app_name="inventory-platform-test",
        log_level="WARNING",
        jwt_secret="test-only-jwt-secret",
        database_url="postgresql+asyncpg://inventory:inventory@localhost:5432/inventory_test",
        redis_url="redis://localhost:6379/15",
    )


# ---------------------------------------------------------------------------
# Deterministic time / ids
# ---------------------------------------------------------------------------
@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(FIXED_EPOCH)


@pytest.fixture()
def fixed_now() -> datetime:
    return FIXED_EPOCH


@pytest.fixture()
def uuids() -> DeterministicUUIDs:
    return DeterministicUUIDs()


@pytest.fixture()
def uuid7_factory() -> Callable[[datetime, int], UUID]:
    """Expose the deterministic UUIDv7 constructor to tests that need explicit timestamps."""

    def factory(moment: datetime, sequence: int = 0) -> UUID:
        return uuid7_at(moment, sequence)

    return factory


# ---------------------------------------------------------------------------
# Tenant / principal / RBAC
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def system_tenant_id() -> UUID:
    from app.seed.constants import SYSTEM_TENANT_ID

    return SYSTEM_TENANT_ID


@pytest.fixture()
def tenant() -> Tenant:
    return make_tenant(0)


@pytest.fixture()
def tenant_id(tenant: Tenant) -> UUID:
    return tenant.id


@pytest.fixture(scope="session")
def rbac_catalogue() -> dict[str, Any]:
    import json

    return json.loads((FIXTURES_DIR / "permissions.json").read_text(encoding="utf-8"))


@pytest.fixture()
def admin_scopes(rbac_catalogue: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(row["scope"] for row in rbac_catalogue["catalogue"] if row["assignable"])


@pytest.fixture()
def principal(tenant: Tenant) -> Principal:
    return make_principal(tenant, role="admin")


@pytest.fixture()
def principal_readonly(tenant: Tenant) -> Principal:
    return make_principal(tenant, index=1, role="readonly")


@pytest.fixture()
def principal_factory(tenant: Tenant) -> Callable[..., Principal]:
    """Factory bound to the current tenant: ``principal_factory(role="buyer", index=2)``."""

    def factory(index: int = 0, **kwargs: Any) -> Principal:
        kwargs.setdefault("role", "admin")
        return make_principal(tenant, index=index, **kwargs)

    return factory


@pytest.fixture()
def tenant_context(principal: Principal, tenant: Tenant) -> Iterator[TenantContext]:
    context = TenantContext.from_principal(principal, tenant)
    with bind_context(context):
        yield context


@pytest.fixture()
def auth_headers(principal: Principal) -> dict[str, str]:
    """Placeholder bearer header until FS-00 auth middleware lands.

    FS-00 replaces this fixture with a real signed JWT/API key; tests should depend on
    ``auth_headers`` rather than constructing tokens themselves.
    """
    return {
        "Authorization": f"Bearer test-token:{principal.id}",
        "X-Test-Tenant-Id": str(principal.tenant_id),
    }


# ---------------------------------------------------------------------------
# FastAPI app + clients
# ---------------------------------------------------------------------------
@pytest.fixture()
def app_instance() -> Any:
    try:
        module = importlib.import_module("app.api.main")
    except ModuleNotFoundError as exc:  # pragma: no cover - placeholder until S0-01/FS-00 wiring
        pytest.skip(f"FastAPI app module not available yet: {exc}")
    create_app = getattr(module, "create_app", None)
    if create_app is None:  # pragma: no cover - placeholder until S0-01/FS-00 wiring
        pytest.skip("app.api.main has no create_app factory yet")
    return create_app()


@pytest.fixture()
def sync_client(app_instance: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(app_instance) as client:
        yield client


@pytest.fixture()
async def client(app_instance: Any) -> AsyncIterator[Any]:
    httpx = pytest.importorskip("httpx")
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Database (async SQLAlchemy, graceful skip, transaction rollback isolation)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def db_config() -> DbProfileConfig:
    return resolve_db_profile()


@pytest.fixture(scope="session")
def requires_db(db_config: DbProfileConfig) -> DbProfileConfig:
    if not db_config.available:
        pytest.skip(f"integration database unavailable: {db_config.reason}")
    return db_config


@pytest.fixture()
def db_profile_description(db_config: DbProfileConfig) -> dict[str, Any]:
    return describe(db_config)


@pytest.fixture()
async def db_engine(requires_db: DbProfileConfig) -> AsyncIterator[Any]:
    sqlalchemy = pytest.importorskip("sqlalchemy.ext.asyncio")
    try:
        url = ensure_database_url(requires_db)
        await check_connection(url)
    except DatabaseUnavailable as exc:
        pytest.skip(f"integration database unavailable: {exc}")
    engine = sqlalchemy.create_async_engine(url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
async def db_session(db_engine: Any) -> AsyncIterator[Any]:
    """Async session bound to an outer transaction that is always rolled back.

    Tests may commit/rollback their own work; the outer transaction rollback resets the database to
    its pre-test state (mirrors ``DATABASE_RESET=rollback`` semantics).
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()


@pytest.fixture()
def db_tables() -> list[str]:
    """Tables reset between database tests; feature-set conftests override with mapped tables."""
    return []


@pytest.fixture()
async def db_reset(db_engine: Any, db_tables: list[str]) -> AsyncIterator[None]:
    """Truncate the declared tables before a test (no-op while models/tables do not exist yet)."""
    if db_tables:
        from sqlalchemy import text

        async with db_engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {', '.join(db_tables)} RESTART IDENTITY CASCADE"))
    yield


# ---------------------------------------------------------------------------
# Convenience object fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def uom_pair_eaches():
    return make_uom_pair("eaches")


@pytest.fixture()
def item_factory() -> Callable[..., ItemSample]:
    def factory(index: int = 0, **kwargs: Any) -> ItemSample:
        return make_item(index, **kwargs)

    return factory


@pytest.fixture()
def location_factory() -> Callable[..., LocationSample]:
    def factory(index: int = 0, **kwargs: Any) -> LocationSample:
        return make_location(index, **kwargs)

    return factory


@pytest.fixture()
def status_factory() -> Callable[..., Any]:
    def factory(code: str = "AVAILABLE", **kwargs: Any) -> Any:
        return make_status(code, **kwargs)

    return factory


@pytest.fixture()
def posting_factory() -> Callable[..., LedgerPostingSample]:
    def factory(index: int = 0, **kwargs: Any) -> LedgerPostingSample:
        return make_ledger_posting(index, **kwargs)

    return factory


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _db_profile_report() -> Iterator[None]:
    """Print the DB profile once when ``TEST_DB_REPORT=1`` (CI diagnostics, no Docker probing otherwise)."""
    if os.environ.get("TEST_DB_REPORT") == "1":
        os.environ.pop("TEST_DB_REPORT", None)  # avoid re-entry in nested pytest runs
        print(f"[harness] db profile: {describe(resolve_db_profile())}")
    yield
