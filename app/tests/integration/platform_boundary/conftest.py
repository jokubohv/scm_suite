"""Fixtures for FS-00 boundary/finance/reference/jobs API integration tests (task-53).

Mounts only the router under test on a minimal FastAPI app, uses the shared rollback-isolated
``db_session`` fixture, overrides ``get_db``/``get_current_principal`` and injects a fresh
``StubOrderDemandAdapter`` per test. No edits to main/registry/deps/conftest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import boundary as boundary_router
from app.core.models import platform as p
from app.core.repositories.boundary import StubOrderDemandAdapter

TENANT_ID = UUID("9b000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("9b000000-0000-7000-8000-000000000002")
JOB_DEFINITION_ID = UUID("9b000000-0000-7000-8000-000000000010")
DISABLED_JOB_DEFINITION_ID = UUID("9b000000-0000-7000-8000-000000000011")

ALL_SCOPES = frozenset(
    {
        "inventory.balance.read",
        "platform.internal",
        "platform.finance.read",
        "platform.reference.read",
        "platform.finance.sync",
        "platform.job.manage",
    }
)


async def _seed_tenant(session: Any, tenant_id: UUID, code: str) -> None:
    await session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"Boundary {code}",
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await _seed_tenant(db_session, TENANT_ID, "BND1")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "BND2")
    await db_session.execute(
        p.job_definition.insert().values(
            id=JOB_DEFINITION_ID,
            tenant_id=TENANT_ID,
            name="ledger.reconcile_balances",
            queue="default",
            handler="tests.noop",
            timeout_seconds=60,
            max_attempts=3,
            singleton=True,
            is_enabled=True,
            version=1,
        )
    )
    await db_session.execute(
        p.job_definition.insert().values(
            id=DISABLED_JOB_DEFINITION_ID,
            tenant_id=TENANT_ID,
            name="disabled.job",
            queue="default",
            handler="tests.noop",
            timeout_seconds=60,
            max_attempts=3,
            singleton=True,
            is_enabled=False,
            version=1,
        )
    )
    await db_session.flush()
    return {
        "tenant_id": str(TENANT_ID),
        "other_tenant_id": str(OTHER_TENANT_ID),
        "job_definition_id": str(JOB_DEFINITION_ID),
        "disabled_job_definition_id": str(DISABLED_JOB_DEFINITION_ID),
    }


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(TENANT_ID),
        token_version=0,
        scopes=ALL_SCOPES,
    )


@pytest.fixture()
def order_adapter() -> StubOrderDemandAdapter:
    return StubOrderDemandAdapter()


@pytest.fixture()
def admin_app(db_session: Any, principal: Principal, order_adapter: StubOrderDemandAdapter) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(boundary_router.router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[boundary_router.get_adapter] = lambda: order_adapter
    return app


@pytest.fixture()
async def api_client(admin_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
def limited_principal() -> Principal:
    """A principal without any of the boundary scopes (for 403 assertions)."""
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(TENANT_ID),
        token_version=0,
        scopes=frozenset({"platform.audit.read"}),
    )


async def seed_outbox_event(
    db_session: Any,
    *,
    tenant_id: UUID,
    event_id: UUID | None = None,
    aggregate_type: str = "inventory_transaction",
    aggregate_id: UUID | None = None,
    published_at: Any = None,
) -> dict[str, Any]:
    """Insert one outbox event (partitioned table; ``occurred_at`` = now)."""
    from datetime import UTC, datetime

    event_id = event_id or uuid4()
    aggregate_id = aggregate_id or uuid4()
    row_id = uuid4()
    now = datetime.now(UTC)
    await db_session.execute(
        p.outbox_event.insert().values(
            id=row_id,
            tenant_id=tenant_id,
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=1,
            event_type="inventory.adjusted",
            event_version="v1",
            payload={"test": True},
            occurred_at=now,
            correlation_id=None,
            causation_id=None,
            published_at=published_at,
            attempts=0,
            last_error=None,
        )
    )
    await db_session.flush()
    return {"id": row_id, "event_id": event_id, "occurred_at": now}
