"""FS-06 numbered-inventory integration fixtures (live Postgres; graceful skip without DB).

Seeds tenants/item-master/registry rows directly through the shared Core metadata (no other context's
service dependency), mounts only the FS-06 router on a throwaway FastAPI app and overrides the DB +
principal dependencies. Serial projection/history rows are produced by the 0013 posting triggers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.traceability_numbered import router
from app.core.models import platform as platform_models
from app.modules.item_master import tables as it
from app.modules.traceability import tables as reg

ALL_SCOPES = frozenset(
    {
        "inventory.lot.read",
        "inventory.lot.write",
        "inventory.lot.inactivate",
        "inventory.serial.read",
        "inventory.serial.register",
        "inventory.numbered.validate",
        "inventory.numbered.reconcile",
    }
)

SERIAL_ITEM = uuid4()
LOT_ITEM = uuid4()
PLAIN_ITEM = uuid4()
LOCATION_ID = uuid4()
STATUS_ID = uuid4()


async def _tenant(session: Any, tenant_id: Any, code: str) -> None:
    await session.execute(
        insert(platform_models.tenant).values(
            id=tenant_id, tenant_id=tenant_id, code=code, name=code, timezone="UTC", status="ACTIVE"
        )
    )


async def _enable_domain(session: Any, tenant_id: Any, enabled: bool = True) -> None:
    await session.execute(
        insert(platform_models.feature_flag).values(
            id=uuid4(),
            tenant_id=tenant_id,
            flag_key="inventory.numbered.enabled",
            is_enabled=enabled,
            source="MIGRATION",
        )
    )


async def _item_types(session: Any, tenant_id: Any) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    for code, requires_numbering in (("SERIALIZED", True), ("LOT_NUMBERED", True), ("INVENTORY", False)):
        type_id = uuid4()
        await session.execute(
            insert(it.item_type_definition).values(
                id=type_id, tenant_id=tenant_id, code=code, name=code, requires_numbering=requires_numbering
            )
        )
        ids[code] = type_id
    return ids


async def _item(session: Any, tenant_id: Any, item_id: Any, number: str, type_id: Any) -> None:
    await session.execute(
        insert(it.item).values(
            id=item_id,
            tenant_id=tenant_id,
            item_number=number,
            display_name=number,
            item_type_id=type_id,
            is_active=True,
            created_from="MANUAL",
        )
    )
    await session.execute(
        insert(it.item_inventory).values(tenant_id=tenant_id, item_id=item_id, costing_method="AVERAGE")
    )


async def _registry(session: Any, tenant_id: Any) -> None:
    await session.execute(
        insert(reg.location).values(
            id=LOCATION_ID, tenant_id=tenant_id, code="WH1", name="WH1", location_type="WAREHOUSE"
        )
    )
    await session.execute(
        insert(reg.inventory_status).values(
            id=STATUS_ID,
            tenant_id=tenant_id,
            code="AVAILABLE",
            name="Available",
            is_available_for_allocation=True,
            is_committable=True,
            is_sellable=True,
            is_default=True,
            is_system=True,
        )
    )


async def seed_tenant(session: Any, tenant_id: Any, suffix: str) -> dict[str, Any]:
    await _tenant(session, tenant_id, f"NUM{suffix}")
    await _enable_domain(session, tenant_id)
    types = await _item_types(session, tenant_id)
    await _item(session, tenant_id, SERIAL_ITEM, f"SER-{suffix}", types["SERIALIZED"])
    await _item(session, tenant_id, LOT_ITEM, f"LOT-{suffix}", types["LOT_NUMBERED"])
    await _item(session, tenant_id, PLAIN_ITEM, f"PLN-{suffix}", types["INVENTORY"])
    await _registry(session, tenant_id)
    return {"tenant_id": tenant_id, "types": types}


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    tenant_id = uuid4()
    await seed_tenant(db_session, tenant_id, tenant_id.hex[:6].upper())
    await db_session.flush()
    return {
        "tenant_id": tenant_id,
        "serial_item": SERIAL_ITEM,
        "lot_item": LOT_ITEM,
        "plain_item": PLAIN_ITEM,
        "location_id": LOCATION_ID,
        "status_id": STATUS_ID,
    }


@pytest.fixture()
def principal(seeded: dict[str, Any]) -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(seeded["tenant_id"]),
        token_version=0,
        scopes=ALL_SCOPES,
    )


@pytest.fixture()
def other_principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(uuid4()),
        token_version=0,
        scopes=ALL_SCOPES,
    )


@pytest.fixture()
def numbered_app(db_session: Any, principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(numbered_app: FastAPI) -> AsyncIterator[Any]:
    transport = httpx.ASGITransport(app=numbered_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}
