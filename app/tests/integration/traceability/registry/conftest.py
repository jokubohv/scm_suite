"""Fixtures for FS-03 traceability integration tests (live DB, graceful skip).

Mounts only the traceability router, uses the shared rollback-isolated ``db_session`` fixture, seeds a
tenant (+ inventory settings and the deploy-dark flag), minimal FS-01 item-master rows, then runs the
lazy per-tenant bootstrap (sentinel + status catalogue) through the service under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.traceability import router as traceability_router
from app.core.models import platform as p
from app.modules.item_master import tables as it
from app.modules.traceability.service import TraceabilityService

TENANT_ID = UUID("a0000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("a0000000-0000-7000-8000-000000000002")
ITEM_TYPE_ID = UUID("a0000000-0000-7000-8000-000000000010")
UOM_TYPE_ID = UUID("a0000000-0000-7000-8000-000000000011")
UOM_ID = UUID("a0000000-0000-7000-8000-000000000012")
CATEGORY_ID = UUID("a0000000-0000-7000-8000-000000000013")
ITEM_ID = UUID("a0000000-0000-7000-8000-000000000021")
ITEM_ID_2 = UUID("a0000000-0000-7000-8000-000000000022")
ALL_SCOPES = frozenset(
    {
        "inventory.location.read",
        "inventory.location.manage",
        "inventory.bin.read",
        "inventory.bin.manage",
        "inventory.status.read",
        "inventory.status.manage",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _seed_tenant(db_session: Any, tenant_id: UUID, code: str) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"Trace {code}",
            base_currency_code="USD",
            timezone="America/New_York",
            status="ACTIVE",
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )
    for key, value in (("bins.enabled", True), ("statuses.enabled", True)):
        await db_session.execute(
            p.tenant_settings.insert().values(
                id=uuid4(),
                tenant_id=tenant_id,
                namespace="inventory",
                key=key,
                value=value,
                value_type="BOOLEAN",
                is_default=False,
                version=1,
            )
        )
    await db_session.execute(
        p.feature_flag.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            flag_key="traceability.registry.enabled",
            is_enabled=True,
            source="TEST",
            version=1,
        )
    )


async def _seed_item_master(db_session: Any, tenant_id: UUID, *, suffix: str) -> None:
    await db_session.execute(
        it.item_type_definition.insert().values(
            id=ITEM_TYPE_ID if tenant_id == TENANT_ID else uuid4(),
            tenant_id=tenant_id,
            code=f"INVENTORY{suffix}",
            name="Inventory",
            description=None,
            can_stock=True,
            requires_bom=False,
            requires_numbering=False,
            is_charge_only=False,
            sort_order=10,
            is_system=True,
            version=1,
        )
    )
    type_id = (
        (
            await db_session.execute(
                it.item_type_definition.select().where(it.item_type_definition.c.tenant_id == tenant_id)
            )
        )
        .mappings()
        .first()["id"]
    )
    uom_type_id = UOM_TYPE_ID if tenant_id == TENANT_ID else uuid4()
    await db_session.execute(
        it.uom_type.insert().values(
            id=uom_type_id,
            tenant_id=tenant_id,
            name="Quantity",
            code=f"QTY{suffix}",
            base_uom_id=None,
            is_active=True,
            version=1,
        )
    )
    uom_id = UOM_ID if tenant_id == TENANT_ID else uuid4()
    await db_session.execute(
        it.uom.insert().values(
            id=uom_id,
            tenant_id=tenant_id,
            uom_type_id=uom_type_id,
            code=f"EA{suffix}",
            name="Each",
            conversion_factor=Decimal("1"),
            is_base=True,
            rounding_precision=4,
            is_active=True,
            version=1,
        )
    )
    category_id = CATEGORY_ID if tenant_id == TENANT_ID else uuid4()
    await db_session.execute(
        it.item_category.insert().values(
            id=category_id,
            tenant_id=tenant_id,
            name=f"Trace{suffix}",
            parent_id=None,
            code=f"TR{suffix}",
            is_active=True,
            sort_order=100,
            version=1,
        )
    )
    for index, item_id in enumerate((ITEM_ID, ITEM_ID_2) if tenant_id == TENANT_ID else (uuid4(),)):
        await db_session.execute(
            it.item.insert().values(
                id=item_id,
                tenant_id=tenant_id,
                item_number=f"TRACE-{suffix}-{index}",
                display_name=f"Trace Item {index}",
                description=None,
                item_type_id=type_id,
                is_active=True,
                matrix_parent_id=None,
                base_uom_id=uom_id,
                units_type_id=uom_type_id,
                item_category_id=category_id,
                upc_code=None,
                external_ref=None,
                created_from="MANUAL",
                version=1,
            )
        )
        await db_session.execute(
            it.item_inventory.insert().values(
                tenant_id=tenant_id,
                item_id=item_id,
                costing_method="AVERAGE",
                costing_method_locked=True,
                use_bins=True,
                bin_required_on_txn=True,
                use_statuses=True,
                version=1,
            )
        )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await _seed_tenant(db_session, TENANT_ID, "TRC1")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "TRC2")
    await _seed_item_master(db_session, TENANT_ID, suffix="1")
    await _seed_item_master(db_session, OTHER_TENANT_ID, suffix="2")
    await db_session.flush()
    service = TraceabilityService(db_session)
    sentinel = await service.ensure_bootstrap(TENANT_ID)
    await service.ensure_bootstrap(OTHER_TENANT_ID)
    await db_session.flush()
    statuses = {row["code"]: row for row in await service.list_statuses(TENANT_ID)}
    return {
        "tenant_id": str(TENANT_ID),
        "other_tenant_id": str(OTHER_TENANT_ID),
        "sentinel": sentinel,
        "statuses": statuses,
        "item_id": str(ITEM_ID),
        "item_id_2": str(ITEM_ID_2),
        "uom_id": str(UOM_ID),
    }


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()), app_user_id=str(uuid4()), tenant_id=str(TENANT_ID), token_version=0, scopes=ALL_SCOPES
    )


@pytest.fixture()
def other_principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(OTHER_TENANT_ID),
        token_version=0,
        scopes=ALL_SCOPES,
    )


@pytest.fixture()
def traceability_app(db_session: Any, seeded: dict[str, Any], principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(traceability_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(traceability_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=traceability_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


async def create_location(
    client: Any,
    *,
    code: str = "WH1",
    location_type: str = "WAREHOUSE",
    parent_id: str | None = None,
    bins: bool = True,
    statuses: bool = True,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/locations",
        json={
            "code": code,
            "name": code,
            "location_type": location_type,
            "parent_id": parent_id,
            "is_bins_enabled": bins,
            "is_statuses_enabled": statuses,
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["location"]


async def create_zone(client: Any, *, location_id: str, code: str = "Z1", priority: int = 0) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/zones",
        json={"location_id": location_id, "code": code, "name": code, "zone_type": "STORAGE", "priority": priority},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["zone"]


async def create_bin(
    client: Any, *, location_id: str, code: str = "B1", zone_id: str | None = None, item_ids: list[str] | None = None
) -> dict[str, Any]:
    del item_ids
    response = await client.post(
        "/api/v1/bins",
        json={
            "location_id": location_id,
            "code": code,
            "name": code,
            "bin_type": "STORAGE",
            "zone_id": zone_id,
            "is_pickable": True,
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["bin"]
