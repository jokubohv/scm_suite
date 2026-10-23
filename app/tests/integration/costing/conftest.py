"""Fixtures for FS-05 costing integration tests (live DB, graceful skip).

Mounts only the costing router, uses the shared rollback-isolated ``db_session`` fixture, seeds a
tenant + minimal FS-01 item rows + FS-03 locations + currency and the ``costing.engine.enabled`` flag.
Cross-module seeding uses the frozen Core table metadata in the test harness only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.costing import router as costing_router
from app.core.models import platform as p
from app.modules.item_master import tables as it

TENANT_ID = UUID("d0000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("d0000000-0000-7000-8000-000000000002")
LOCATION_ID = UUID("d0000000-0000-7000-8000-000000000010")
OTHER_LOCATION_ID = UUID("d0000000-0000-7000-8000-000000000011")
CATEGORY_ID = UUID("d0000000-0000-7000-8000-000000000013")
ITEM_A = UUID("d0000000-0000-7000-8000-000000000021")
ITEM_B = UUID("d0000000-0000-7000-8000-000000000022")
OTHER_ITEM = UUID("d0000000-0000-7000-8000-000000000023")

ALL_COSTING_SCOPES = frozenset(
    {
        "costing.cost.read",
        "costing.cost.manage",
        "costing.cost.view",
        "costing.recalc.run",
        "costing.recalculate",
        "costing.standard.activate",
        "costing.standard.rollup",
        "costing.rollup",
        "costing.revaluation.create",
        "costing.revaluation.approve",
        "costing.revaluation.post",
        "costing.landed_cost.create",
        "costing.landed_cost.post",
        "costing.estimated_landed_cost.manage",
        "costing.cogs_adjustment.create",
        "costing.cogs_adjustment.post",
        "costing.variance.view",
        "costing.group.manage",
        "costing.method.manage",
        "costing.landedcost.manage",
        "costing.read",
    }
)


async def _seed_tenant(db_session: Any, tenant_id: UUID, code: str) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"Costing {code}",
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


async def _seed_item_master(db_session: Any, tenant_id: UUID, *, suffix: str, item_ids: tuple[UUID, ...]) -> None:
    type_id = uuid4()
    await db_session.execute(
        it.item_type_definition.insert().values(
            id=type_id,
            tenant_id=tenant_id,
            code=f"INV{suffix}",
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
    uom_type_id = uuid4()
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
    uom_id = uuid4()
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
    category_id = uuid4()
    await db_session.execute(
        it.item_category.insert().values(
            id=category_id,
            tenant_id=tenant_id,
            name="Costing Category",
            parent_id=None,
            code=f"COST{suffix}",
            is_active=True,
            sort_order=100,
            version=1,
        )
    )
    for index, item_id in enumerate(item_ids):
        await db_session.execute(
            it.item.insert().values(
                id=item_id,
                tenant_id=tenant_id,
                item_number=f"COST-{suffix}-{index}",
                display_name=f"Costing Item {suffix} {index}",
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
                initial_cost=Decimal("4"),
                asset_account_ref="ASSET",
                cogs_account_ref="COGS",
                adjustment_account_ref="ADJUSTMENT",
                variance_account_ref="VARIANCE",
                version=1,
            )
        )


async def _seed_locations(db_session: Any, tenant_id: UUID, location_ids: tuple[UUID, ...]) -> None:
    for index, location_id in enumerate(location_ids):
        await db_session.execute(
            text(
                """
                INSERT INTO traceability.location
                    (id, tenant_id, code, name, location_type, timezone, is_unallocated,
                     is_inactive, created_at, updated_at, version)
                VALUES
                    (:id, :tenant_id, :code, :name, 'WAREHOUSE', 'UTC', false, false, now(), now(), 1)
                """
            ),
            {"id": location_id, "tenant_id": tenant_id, "code": f"COST-LOC-{index}", "name": f"Cost Loc {index}"},
        )


async def _enable_flag(db_session: Any, tenant_id: UUID, enabled: bool = True) -> None:
    await db_session.execute(
        p.feature_flag.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            flag_key="costing.engine.enabled",
            is_enabled=enabled,
            source="TEST",
            version=1,
        )
    )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await _seed_tenant(db_session, TENANT_ID, "COST1")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "COST2")
    await _seed_item_master(db_session, TENANT_ID, suffix="1", item_ids=(ITEM_A, ITEM_B))
    await _seed_item_master(db_session, OTHER_TENANT_ID, suffix="2", item_ids=(OTHER_ITEM,))
    await _seed_locations(db_session, TENANT_ID, (LOCATION_ID, OTHER_LOCATION_ID))
    await _seed_locations(db_session, OTHER_TENANT_ID, (uuid4(),))
    await _enable_flag(db_session, TENANT_ID)
    await _enable_flag(db_session, OTHER_TENANT_ID)
    await db_session.flush()
    return {
        "tenant_id": str(TENANT_ID),
        "other_tenant_id": str(OTHER_TENANT_ID),
        "items": [str(ITEM_A), str(ITEM_B)],
        "item_a": str(ITEM_A),
        "item_b": str(ITEM_B),
        "other_item": str(OTHER_ITEM),
        "location_id": str(LOCATION_ID),
        "other_location_id": str(OTHER_LOCATION_ID),
    }


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(TENANT_ID),
        token_version=0,
        scopes=ALL_COSTING_SCOPES,
    )


@pytest.fixture()
def costing_app(db_session: Any, seeded: dict[str, Any], principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(costing_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(costing_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=costing_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


async def create_category(client: Any, *, code: str = "MATERIAL", category_type: str = "MATERIAL") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/cost-categories",
        json={"code": code, "name": code, "category_type": category_type, "is_rollup_input": True},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()
