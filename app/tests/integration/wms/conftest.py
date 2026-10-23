"""Fixtures for FS-11 WMS integration tests (live DB, rollback-isolated tenancy)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import insert, text

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.wms import router as wms_router
from app.core.models.platform import app_user, tenant, user_identity
from app.modules.inventory_ledger.facade import PostingPort
from app.modules.traceability import tables as tr
from app.modules.wms import tables as wt

TENANT_ID = UUID("f0000000-0000-7000-8000-000000000001")
LOCATION_ID = UUID("f0000000-0000-7000-8000-000000000010")
ZONE_ID = UUID("f0000000-0000-7000-8000-000000000011")
RECEIVING_BIN = UUID("f0000000-0000-7000-8000-000000000012")
STAGING_BIN = UUID("f0000000-0000-7000-8000-000000000013")
STORAGE_BIN = UUID("f0000000-0000-7000-8000-000000000014")
PICK_BIN = UUID("f0000000-0000-7000-8000-000000000015")
STATUS_AVAILABLE = UUID("f0000000-0000-7000-8000-000000000021")
STATUS_HOLD = UUID("f0000000-0000-7000-8000-000000000022")
VENDOR_ID = UUID("f0000000-0000-7000-8000-000000000031")

WMS_SCOPES = frozenset(
    {
        "wms.admin",
        "wms.inventory",
        "wms.pack",
        "wms.pick",
        "wms.putaway",
        "wms.read",
        "wms.receive",
        "wms.replenish",
        "wms.setup.write",
        "wms.ship",
        "wms.wave",
    }
)


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


async def seed_tenant(session: Any, tenant_id: UUID, code: str) -> None:
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=code,
            name=f"WMS {code}",
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


async def seed_identity(session: Any, tenant_id: UUID) -> tuple[UUID, UUID]:
    identity_id, user_id = uuid4(), uuid4()
    email = f"wms-{uuid4().hex[:8]}@test.local"
    await session.execute(
        insert(user_identity).values(id=identity_id, email=email, display_name="WMS Tester", status="ACTIVE")
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=email,
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    return identity_id, user_id


async def seed_flag(session: Any, tenant_id: UUID, key: str, enabled: bool = True) -> None:
    await session.execute(
        insert(__import__("app.core.models.platform", fromlist=["feature_flag"]).feature_flag).values(
            id=uuid4(), tenant_id=tenant_id, flag_key=key, is_enabled=enabled, source="TEST", version=1,
        )
    )


async def seed_wms_world(session: Any, *, prefix: str = "WMS") -> dict[str, Any]:
    """Minimal FS-11 world: tenant, bin-enabled location, zones/bins, statuses, warehouse, items, flags."""
    from app.modules.item_master.service import ItemMasterService

    await seed_tenant(session, TENANT_ID, f"{prefix}{uuid4().hex[:5].upper()}")
    identity_id, user_id = await seed_identity(session, TENANT_ID)
    await session.execute(
        insert(tr.location).values(
            id=LOCATION_ID,
            tenant_id=TENANT_ID,
            code=f"{prefix}-WH",
            name="WMS Warehouse",
            location_type="WAREHOUSE",
            is_bins_enabled=True,
            is_statuses_enabled=True,
            is_consignment_allowed=False,
            allow_commit_from_non_available=False,
            timezone="UTC",
            is_unallocated=False,
            is_inactive=False,
            version=1,
        )
    )
    await session.execute(
        insert(tr.zone).values(
            id=ZONE_ID, tenant_id=TENANT_ID, location_id=LOCATION_ID, code="Z1", name="Zone 1",
            zone_type="STORAGE", priority=1, is_active=True, version=1,
        )
    )
    for bin_id, code, bin_type, pickable in (
        (RECEIVING_BIN, f"{prefix}-RCV", "RECEIVING", False),
        (STAGING_BIN, f"{prefix}-STG", "STAGING", False),
        (STORAGE_BIN, f"{prefix}-STO", "STORAGE", False),
        (PICK_BIN, f"{prefix}-PICK", "PICKING", True),
    ):
        await session.execute(
            insert(tr.bin).values(
                id=bin_id, tenant_id=TENANT_ID, location_id=LOCATION_ID, zone_id=ZONE_ID, code=code,
                name=code, bin_type=bin_type, is_pickable=pickable, is_active=True,
                capacity_qty=None, coordinates={}, version=1,
            )
        )
    for status_id, code, is_default in ((STATUS_AVAILABLE, "AVAILABLE", True), (STATUS_HOLD, "HOLD", False)):
        await session.execute(
            insert(tr.inventory_status).values(
                id=status_id, tenant_id=TENANT_ID, code=code, name=code,
                is_available_for_allocation=code == "AVAILABLE", is_committable=code == "AVAILABLE",
                is_sellable=code == "AVAILABLE", is_consigned=False, is_default=is_default,
                is_system=True, is_active=True, version=1,
            )
        )
    warehouse_id = uuid4()
    await session.execute(
        insert(wt.warehouse).values(
            id=warehouse_id, tenant_id=TENANT_ID, location_id=LOCATION_ID, code=f"{prefix}-W1", name="WMS W1",
            is_wms_enabled=True, receiving_bin_id=RECEIVING_BIN, staging_bin_id=STAGING_BIN,
            shipping_bin_id=STAGING_BIN, timezone="UTC", is_active=True, version=1,
        )
    )
    pick_strategy_id, putaway_strategy_id = uuid4(), uuid4()
    await session.execute(
        insert(wt.pick_strategy).values(
            id=pick_strategy_id, tenant_id=TENANT_ID, code="FEFO", name="FEFO pick", priority=1,
            applies_to_location_id=LOCATION_ID, is_active=True, version=1,
        )
    )
    await session.execute(
        insert(wt.pick_strategy_rule).values(
            id=uuid4(), tenant_id=TENANT_ID, pick_strategy_id=pick_strategy_id, sequence=1,
            zone_id=ZONE_ID, sort_direction="FEFO", is_active=True,
        )
    )
    await session.execute(
        insert(wt.putaway_strategy).values(
            id=putaway_strategy_id, tenant_id=TENANT_ID, code="DIRECTED", name="Directed putaway", priority=1,
            applies_to_location_id=LOCATION_ID, staging_bin_id=STAGING_BIN, is_active=True, version=1,
        )
    )
    await session.execute(
        insert(wt.putaway_strategy_rule).values(
            id=uuid4(), tenant_id=TENANT_ID, putaway_strategy_id=putaway_strategy_id, sequence=1,
            zone_id=ZONE_ID, bin_type="STORAGE", max_qty_per_bin=None, is_active=True,
        )
    )
    item_service = ItemMasterService(session, TENANT_ID)
    items = {
        "item": await item_service.create_item(
            {"item_number": f"{prefix}-ITEM-A", "display_name": "WMS Item A", "item_type_code": "INVENTORY"}
        ),
        "item_b": await item_service.create_item(
            {"item_number": f"{prefix}-ITEM-B", "display_name": "WMS Item B", "item_type_code": "INVENTORY"}
        ),
    }
    await session.execute(
        insert(__import__("app.modules.procurement.tables", fromlist=["vendor"]).vendor).values(
            id=VENDOR_ID,
            tenant_id=TENANT_ID,
            vendor_number=f"{prefix}-V1",
            name=f"WMS Vendor {uuid4().hex[:6]}",
            currency_code="USD",
            addresses=[],
            primary_contact={},
            is_inactive=False,
            version=1,
        )
    )
    for feature_flag in ("wms.execution.enabled", "procurement.p2p.enabled"):
        await seed_flag(session, TENANT_ID, feature_flag, True)
    await session.flush()
    return {
        "tenant_id": str(TENANT_ID),
        "identity_id": str(identity_id),
        "app_user_id": str(user_id),
        "location_id": str(LOCATION_ID),
        "zone_id": str(ZONE_ID),
        "receiving_bin": str(RECEIVING_BIN),
        "staging_bin": str(STAGING_BIN),
        "storage_bin": str(STORAGE_BIN),
        "pick_bin": str(PICK_BIN),
        "status_available": str(STATUS_AVAILABLE),
        "status_hold": str(STATUS_HOLD),
        "warehouse_id": str(warehouse_id),
        "pick_strategy_id": str(pick_strategy_id),
        "putaway_strategy_id": str(putaway_strategy_id),
        "item_id": items["item"]["id"],
        "item_number": items["item"]["item_number"],
        "item_b_id": items["item_b"]["id"],
        "item_b_number": items["item_b"]["item_number"],
        "vendor_id": str(VENDOR_ID),
    }


async def seed_stock(
    session: Any,
    *,
    item_id: Any,
    bin_id: Any,
    qty: str = "10",
    unit_cost: str = "5",
    status_id: Any = STATUS_AVAILABLE,
    location_id: Any = LOCATION_ID,
) -> dict[str, Any]:
    """Create on-hand stock through the frozen T-01 posting path (never direct ledger writes)."""
    movement = {
        "item_id": str(item_id),
        "location_id": str(location_id),
        "inventory_status_id": str(status_id),
        "qty": str(qty),
        "bin_id": str(bin_id),
        "unit_cost": str(unit_cost),
    }
    result = await PostingPort(session, TENANT_ID).post_receipt(
        transaction_date=__import__("datetime").date.today(),
        source_doc_type="TEST_SEED",
        source_doc_id=uuid4(),
        movements=[movement],
        idempotency_key=f"seed-{uuid4().hex}",
    )
    return result


async def bin_on_hand(session: Any, bin_id: Any) -> str:
    from decimal import Decimal

    value = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance_bin "
                "WHERE tenant_id = :tenant AND bin_id = :bin"
            ),
            {"tenant": TENANT_ID, "bin": bin_id},
        )
    ).scalar_one()
    return format(Decimal(value), ".12f")


async def balance_on_hand(session: Any, item_id: Any) -> str:
    from decimal import Decimal

    value = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance "
                "WHERE tenant_id = :tenant AND item_id = :item"
            ),
            {"tenant": TENANT_ID, "item": item_id},
        )
    ).scalar_one()
    return format(Decimal(value), ".12f")


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    return await seed_wms_world(db_session)


@pytest.fixture()
def principal(seeded: dict[str, Any]) -> Principal:
    return Principal(
        identity_id=seeded["identity_id"],
        app_user_id=seeded["app_user_id"],
        tenant_id=seeded["tenant_id"],
        token_version=0,
        scopes=WMS_SCOPES,
    )


@pytest.fixture()
def wms_app(db_session: Any, seeded: dict[str, Any], principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(wms_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(wms_app: FastAPI) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=wms_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
