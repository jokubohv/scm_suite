"""Fixtures for FS-07 counts integration tests (live DB, rollback-isolated tenancy)."""

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
from app.api.routers.counts import router as counts_router
from app.core.models import platform as p
from app.modules.item_master import tables as it

REQUESTER_APP_USER_ID = UUID("e0000000-0000-7000-8000-000000000031")
APPROVER_APP_USER_ID = UUID("e0000000-0000-7000-8000-000000000032")
REQUESTER_IDENTITY_ID = UUID("e0000000-0000-7000-8000-000000000033")
APPROVER_IDENTITY_ID = UUID("e0000000-0000-7000-8000-000000000034")
TENANT_ID = UUID("e0000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("e0000000-0000-7000-8000-000000000002")
LOCATION_ID = UUID("e0000000-0000-7000-8000-000000000010")
OTHER_LOCATION_ID = UUID("e0000000-0000-7000-8000-000000000011")
STATUS_ID = UUID("e0000000-0000-7000-8000-000000000012")
OTHER_STATUS_ID = UUID("e0000000-0000-7000-8000-000000000013")
ITEM_A = UUID("e0000000-0000-7000-8000-000000000021")
ITEM_B = UUID("e0000000-0000-7000-8000-000000000022")
OTHER_ITEM = UUID("e0000000-0000-7000-8000-000000000023")

ALL_COUNT_SCOPES = frozenset(
    {
        "inventory.count.read",
        "inventory.count.create",
        "inventory.count.capture",
        "inventory.count.approve",
        "inventory.count.post",
        "inventory.count.plan.manage",
        "inventory.count.recount",
    }
)


async def _seed_tenant(db_session: Any, tenant_id: UUID, code: str) -> None:
    await db_session.execute(
        p.tenant.insert().values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"Counts {code}",
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
            negative_inventory_policy="ALLOW",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )


async def _seed_app_user(db_session: Any, tenant_id: UUID, *, app_user_id: UUID, identity_id: UUID, email: str) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO platform.user_identity (id, email, display_name, status, created_at, updated_at, version)
            VALUES (:id, :email, :display_name, 'ACTIVE', now(), now(), 1)
            """
        ),
        {"id": identity_id, "email": email, "display_name": email.split("@")[0]},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO platform.app_user
                (id, tenant_id, identity_id, email, display_name, status, membership_status,
                 token_version, is_service_account, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :identity_id, :email, :display_name, 'ACTIVE', 'ACTIVE', 0, false,
                    now(), now(), 1)
            """
        ),
        {"id": app_user_id, "tenant_id": tenant_id, "identity_id": identity_id, "email": email, "display_name": email.split("@")[0]},
    )


async def _seed_approval_definition(db_session: Any, tenant_id: UUID, *, code: str = "COUNT") -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO platform.approval_definition
                (id, tenant_id, document_type, name, condition, steps, is_enabled, priority,
                 created_at, updated_at, version)
            VALUES (gen_random_uuid(), :tenant_id, 'INVENTORY_COUNT', :name, '{}'::jsonb,
                    '[{"step": 1, "name": "Count approval", "mode": "SEQUENTIAL"}]'::jsonb,
                    true, 100, now(), now(), 1)
            """
        ),
        {"tenant_id": tenant_id, "name": f"Inventory count approval {code}"},
    )


async def _seed_item_master(db_session: Any, tenant_id: UUID, *, suffix: str, item_ids: tuple[UUID, ...]) -> None:
    type_id = uuid4()
    await db_session.execute(
        it.item_type_definition.insert().values(
            id=type_id, tenant_id=tenant_id, code=f"INV{suffix}", name="Inventory", description=None,
            can_stock=True, requires_bom=False, requires_numbering=False, is_charge_only=False,
            sort_order=10, is_system=True, version=1,
        )
    )
    uom_type_id = uuid4()
    await db_session.execute(
        it.uom_type.insert().values(
            id=uom_type_id, tenant_id=tenant_id, name="Quantity", code=f"QTY{suffix}",
            base_uom_id=None, is_active=True, version=1,
        )
    )
    uom_id = uuid4()
    await db_session.execute(
        it.uom.insert().values(
            id=uom_id, tenant_id=tenant_id, uom_type_id=uom_type_id, code=f"EA{suffix}", name="Each",
            conversion_factor=Decimal("1"), is_base=True, rounding_precision=4, is_active=True, version=1,
        )
    )
    category_id = uuid4()
    await db_session.execute(
        it.item_category.insert().values(
            id=category_id, tenant_id=tenant_id, name="Count Category", parent_id=None,
            code=f"CNT{suffix}", is_active=True, sort_order=100, version=1,
        )
    )
    for index, item_id in enumerate(item_ids):
        await db_session.execute(
            it.item.insert().values(
                id=item_id, tenant_id=tenant_id, item_number=f"COUNT-{suffix}-{index}",
                display_name=f"Count Item {suffix} {index}", description=None, item_type_id=type_id,
                is_active=True, matrix_parent_id=None, base_uom_id=uom_id, units_type_id=uom_type_id,
                item_category_id=category_id, upc_code=None, external_ref=None, created_from="MANUAL",
                version=1,
            )
        )
        await db_session.execute(
            it.item_inventory.insert().values(
                tenant_id=tenant_id, item_id=item_id, costing_method="AVERAGE", costing_method_locked=True,
                initial_cost=Decimal("4"), allow_negative_inventory=True, version=1,
            )
        )
    return None


async def _seed_location_status(db_session: Any, tenant_id: UUID, *, location_id: UUID, status_id: UUID, suffix: str) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO traceability.location
                (id, tenant_id, code, name, location_type, timezone, is_unallocated, is_inactive,
                 created_at, updated_at, version)
            VALUES (:id, :tenant_id, :code, :name, 'WAREHOUSE', 'UTC', false, false, now(), now(), 1)
            """
        ),
        {"id": location_id, "tenant_id": tenant_id, "code": f"LOC-{suffix}", "name": f"Location {suffix}"},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO traceability.inventory_status
                (id, tenant_id, code, name, is_available_for_allocation, is_committable, is_sellable,
                 is_consigned, is_default, is_system, is_active, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :code, :name, true, true, true, false, true, true, true, now(), now(), 1)
            """
        ),
        {"id": status_id, "tenant_id": tenant_id, "code": f"ST-{suffix}", "name": f"Status {suffix}"},
    )


async def seed_balance(
    db_session: Any,
    *,
    tenant_id: UUID,
    item_id: UUID,
    location_id: UUID,
    status_id: UUID,
    qty: str = "10",
    value: str = "40",
    ownership: str = "OWNED",
    serial_id: UUID | None = None,
) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO inventory_ledger.inventory_balance
                (id, tenant_id, item_id, location_id, inventory_status_id, ownership, qty_class,
                 lot_id, serial_id, qty_on_hand, qty_committed, qty_on_order, qty_backordered,
                 value_base, last_ledger_sequence, created_at, updated_at, version)
            VALUES
                (gen_random_uuid(), :tenant_id, :item_id, :location_id, :status_id, :ownership, 'ON_HAND',
                 NULL, :serial_id, :qty, 0, 0, 0, :value, 1, now(), now(), 1)
            """
        ),
        {
            "tenant_id": tenant_id, "item_id": item_id, "location_id": location_id,
            "status_id": status_id, "ownership": ownership, "qty": Decimal(qty),
            "value": Decimal(value), "serial_id": serial_id,
        },
    )


async def _enable_flag(db_session: Any, tenant_id: UUID, enabled: bool) -> None:
    await db_session.execute(
        p.feature_flag.insert().values(
            id=uuid4(), tenant_id=tenant_id, flag_key="inventory.counts.enabled",
            is_enabled=enabled, source="TEST", version=1,
        )
    )


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    await _seed_tenant(db_session, TENANT_ID, "CNT1")
    await _seed_tenant(db_session, OTHER_TENANT_ID, "CNT2")
    await _seed_item_master(db_session, TENANT_ID, suffix="1", item_ids=(ITEM_A, ITEM_B))
    await _seed_item_master(db_session, OTHER_TENANT_ID, suffix="2", item_ids=(OTHER_ITEM,))
    await _seed_location_status(db_session, TENANT_ID, location_id=LOCATION_ID, status_id=STATUS_ID, suffix="1")
    await _seed_location_status(db_session, OTHER_TENANT_ID, location_id=uuid4(), status_id=uuid4(), suffix="3")
    await _enable_flag(db_session, TENANT_ID, True)
    await _enable_flag(db_session, OTHER_TENANT_ID, True)
    await _seed_app_user(
        db_session, TENANT_ID, app_user_id=REQUESTER_APP_USER_ID,
        identity_id=REQUESTER_IDENTITY_ID, email="count-requester@test.local",
    )
    await _seed_app_user(
        db_session, TENANT_ID, app_user_id=APPROVER_APP_USER_ID,
        identity_id=APPROVER_IDENTITY_ID, email="count-approver@test.local",
    )
    await _seed_approval_definition(db_session, TENANT_ID)
    await seed_balance(db_session, tenant_id=TENANT_ID, item_id=ITEM_A, location_id=LOCATION_ID, status_id=STATUS_ID, qty="10", value="40")
    await seed_balance(db_session, tenant_id=TENANT_ID, item_id=ITEM_B, location_id=LOCATION_ID, status_id=STATUS_ID, qty="5", value="15")
    await db_session.flush()
    return {
        "tenant_id": str(TENANT_ID),
        "other_tenant_id": str(OTHER_TENANT_ID),
        "item_a": str(ITEM_A),
        "item_b": str(ITEM_B),
        "other_item": str(OTHER_ITEM),
        "location_id": str(LOCATION_ID),
        "status_id": str(STATUS_ID),
    }


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(REQUESTER_IDENTITY_ID), app_user_id=str(REQUESTER_APP_USER_ID), tenant_id=str(TENANT_ID),
        token_version=0, scopes=ALL_COUNT_SCOPES,
    )


@pytest.fixture()
def counts_app(db_session: Any, seeded: dict[str, Any], principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(counts_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(counts_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=counts_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}
