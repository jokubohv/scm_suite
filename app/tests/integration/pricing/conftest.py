"""Fixtures for FS-02 pricing integration tests (live DB, graceful skip).

Mounts only the pricing router (integration-owned registration), uses the shared rollback-isolated
``db_session`` fixture, seeds a tenant + minimal FS-01 item-master rows (item type/UoM/category/items +
item_sales pointers) and enables the ``pricing.engine.enabled`` tenant flag. Cross-module seeding uses
the frozen FS-01 Core table metadata read/write only in the test harness (production code uses the
item-master port).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.pricing import router as pricing_router
from app.core.models import platform as p
from app.modules.item_master import tables as it

TENANT_ID = UUID("c0000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("c0000000-0000-7000-8000-000000000002")
ITEM_TYPE_ID = UUID("c0000000-0000-7000-8000-000000000010")
UOM_TYPE_ID = UUID("c0000000-0000-7000-8000-000000000011")
UOM_ID = UUID("c0000000-0000-7000-8000-000000000012")
CATEGORY_ID = UUID("c0000000-0000-7000-8000-000000000013")
ITEM_A = UUID("c0000000-0000-7000-8000-000000000021")
ITEM_B = UUID("c0000000-0000-7000-8000-000000000022")
OTHER_ITEM = UUID("c0000000-0000-7000-8000-000000000023")

ALL_PRICING_SCOPES = frozenset(
    {
        "pricing.read",
        "pricing.level.manage",
        "pricing.price.manage",
        "pricing.schedule.manage",
        "pricing.rule.manage",
        "pricing.collection.manage",
        "pricing.coupon.manage",
        "pricing.update.manage",
        "pricing.massupdate",
        "pricing.resolve",
    }
)


async def _seed_tenant(db_session: Any, tenant_id: UUID, code: str) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"Pricing {code}",
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
            name="Pricing Category",
            parent_id=None,
            code=f"PRICE{suffix}",
            is_active=True,
            sort_order=100,
            version=1,
        )
    )
    item_ids = (ITEM_A, ITEM_B) if tenant_id == TENANT_ID else (uuid4(),)
    for index, item_id in enumerate(item_ids):
        await db_session.execute(
            it.item.insert().values(
                id=item_id,
                tenant_id=tenant_id,
                item_number=f"ITEM-{suffix}-{index}",
                display_name=f"Pricing Item {suffix} {index}",
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
            it.item_sales.insert().values(
                tenant_id=tenant_id,
                item_id=item_id,
                sale_uom_id=uom_id,
                price_level_id=None,
                is_taxable=True,
                is_sellable=True,
                is_shippable=True,
                quantity_pricing_schedule_id=None,
                pricing_group_id=None,
                matrix_parent_qty_behavior="LINE",
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
                version=1,
            )
        )
        await db_session.execute(
            it.item_purchasing.insert().values(
                tenant_id=tenant_id,
                item_id=item_id,
                purchase_price=Decimal("5"),
                version=1,
            )
        )


async def _enable_flag(db_session: Any, tenant_id: UUID, enabled: bool = True) -> None:
    await db_session.execute(
        p.feature_flag.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            flag_key="pricing.engine.enabled",
            is_enabled=enabled,
            source="TEST",
            version=1,
        )
    )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await _seed_tenant(db_session, TENANT_ID, "PRC1")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "PRC2")
    await _seed_item_master(db_session, TENANT_ID, suffix="1")
    await _seed_item_master(db_session, OTHER_TENANT_ID, suffix="2")
    await _enable_flag(db_session, TENANT_ID)
    await _enable_flag(db_session, OTHER_TENANT_ID)
    await db_session.flush()
    return {
        "tenant_id": str(TENANT_ID),
        "other_tenant_id": str(OTHER_TENANT_ID),
        "items": [str(ITEM_A), str(ITEM_B)],
        "item_a": str(ITEM_A),
        "other_item": str(OTHER_ITEM),
    }


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(TENANT_ID),
        token_version=0,
        scopes=ALL_PRICING_SCOPES,
    )


@pytest.fixture()
def other_principal() -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(OTHER_TENANT_ID),
        token_version=0,
        scopes=ALL_PRICING_SCOPES,
    )


@pytest.fixture()
def pricing_app(db_session: Any, seeded: dict[str, Any], principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(pricing_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(pricing_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=pricing_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


async def create_level(
    client: Any, *, code: str = "DEFAULT", currency: str = "USD", is_default: bool = True
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/price-levels",
        json={"code": code, "name": code, "currency_code": currency, "is_default": is_default},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["price_level"]


async def create_price(
    client: Any,
    *,
    item_id: str | None = None,
    level_id: str,
    unit_price: str = "10",
    qty_min: str = "0",
    qty_max: str | None = None,
    effective_from: date | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/item-prices",
        json={
            "item_id": item_id or str(ITEM_A),
            "price_level_id": level_id,
            "currency_code": "USD",
            "qty_min": qty_min,
            "qty_max": qty_max,
            "unit_price": unit_price,
            "effective_from": (effective_from or date(2026, 1, 1)).isoformat(),
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["item_price"]


async def resolve(client: Any, *, quantity: str = "2", item_id: str | None = None, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/prices/resolve",
        json={
            "lines": [{"line_ref": "1", "item_id": item_id or str(ITEM_A), "quantity": quantity, "uom": "EA"}],
            "currency_code": "USD",
            "document_date": "2026-10-21",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
