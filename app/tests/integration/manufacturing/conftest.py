"""Fixtures for FS-09 manufacturing integration tests.

Real Postgres schema (0018–0020 applied, graceful skip when unavailable) with **recording** FS-04
posting/balance ports: manufacturing document/state/read-model behaviour is asserted here; the frozen
ledger/costing engines are covered by their own exit suites. No live network.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.api.deps import get_db, token_service
from app.api.errors import install_error_handlers
from app.api.routers.manufacturing import router as manufacturing_api_router
from app.api.routers.manufacturing.router import _svc as manufacturing_service_dependency
from app.modules.item_master.service import ItemMasterService
from app.modules.manufacturing import tables as mfg_tables
from app.modules.manufacturing.service import ManufacturingService
from app.modules.manufacturing.testing import FakeBalancePort, FakePostingPort
from app.modules.traceability import tables as trace_tables


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


MANUFACTURING_SCOPES = frozenset(
    {
        "manufacturing.read",
        "manufacturing.write",
        "manufacturing.release",
        "manufacturing.post",
        "manufacturing.approve",
        "manufacturing.cost.view",
        "manufacturing.admin",
        "manufacturing.compat.manage",
        "manufacturing.compat.validate",
        "manufacturing.compat.override",
        "manufacturing.unbuild.dispose",
    }
)


def _token(identity_id: uuid.UUID, app_user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=MANUFACTURING_SCOPES,
    )
    return token


async def _insert_location(session: Any, tenant_id: uuid.UUID, code: str) -> uuid.UUID:
    location_id = uuid.uuid4()
    await session.execute(
        trace_tables.location.insert().values(
            id=location_id,
            tenant_id=tenant_id,
            code=code,
            name=code,
            location_type="WAREHOUSE",
            is_bins_enabled=False,
            is_statuses_enabled=False,
            is_consignment_allowed=False,
            allow_commit_from_non_available=False,
            timezone="UTC",
            is_unallocated=False,
            is_inactive=False,
        )
    )
    return location_id


async def _insert_status(session: Any, tenant_id: uuid.UUID, code: str, *, default: bool = False) -> uuid.UUID:
    status_id = uuid.uuid4()
    await session.execute(
        trace_tables.inventory_status.insert().values(
            id=status_id,
            tenant_id=tenant_id,
            code=code,
            name=code,
            is_available_for_allocation=code == "AVAILABLE",
            is_committable=code == "AVAILABLE",
            is_sellable=code == "AVAILABLE",
            is_consigned=False,
            is_default=default,
            is_system=code == "AVAILABLE",
            is_active=True,
        )
    )
    return status_id


async def seed_manufacturing_world(session: Any, *, code_prefix: str = "MFG") -> dict[str, Any]:
    tenant_id = uuid.uuid4()
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    from sqlalchemy import insert

    from app.core.models.platform import app_user, feature_flag, tenant, user_identity

    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"{code_prefix}{uuid.uuid4().hex[:6].upper()}",
            name="Manufacturing Test Tenant",
            status="ACTIVE",
            base_currency_code="USD",
        )
    )
    await session.execute(
        insert(user_identity).values(
            id=identity_id, email=f"mfg-{uuid.uuid4().hex[:8]}@test.local", display_name="MFG", status="ACTIVE"
        )
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"mfg-{uuid.uuid4().hex[:8]}@test.local",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    for flag in ("manufacturing.enabled", "manufacturing.wip.enabled"):
        await session.execute(
            insert(feature_flag).values(
                id=uuid.uuid4(), tenant_id=tenant_id, flag_key=flag, is_enabled=True, source="TEST"
            )
        )
    from app.core.models.platform import number_sequence

    for doc_type, prefix in (
        ("WO", "WO-"),
        ("WIP", "WIP-"),
        ("AB", "AB-"),
        ("AU", "AU-"),
        ("ECN", "ECN-"),
        ("MFGSCH", "SCH-"),
    ):
        await session.execute(
            insert(number_sequence).values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                doc_type=doc_type,
                scope_key="TENANT",
                prefix=prefix,
                padding=6,
                next_value=1,
                reset_policy="NEVER",
                gap_policy="NON_GAPLESS",
                period_key=None,
            )
        )
    location_id = await _insert_location(session, tenant_id, f"{code_prefix}-WH")
    status_id = await _insert_status(session, tenant_id, "AVAILABLE", default=True)
    used_status = await _insert_status(session, tenant_id, "USED")
    hold_status = await _insert_status(session, tenant_id, "HOLD")
    for code, name, is_used, sort_order in (
        ("NEW", "New", False, 10),
        ("USED_A", "Used A", True, 20),
        ("REFURB", "Refurbished", True, 30),
        ("SCRAP", "Scrap", True, 90),
    ):
        await session.execute(
            mfg_tables.condition_code.insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                code=code,
                name=name,
                is_used=is_used,
                is_sellable_default=not is_used,
                is_committable_default=not is_used,
                sort_order=sort_order,
                is_active=True,
                created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                version=1,
            )
        )
    item_service = ItemMasterService(session, tenant_id)
    assembly_item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-ASM-{uuid.uuid4().hex[:6]}",
            "display_name": "Test Assembly",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "10"},
        }
    )
    component_item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-CMP-{uuid.uuid4().hex[:6]}",
            "display_name": "Test Component",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "2"},
        }
    )
    phantom_item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-PHM-{uuid.uuid4().hex[:6]}",
            "display_name": "Test Phantom",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "1"},
        }
    )
    other_item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-OTH-{uuid.uuid4().hex[:6]}",
            "display_name": "Other Component",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "3"},
        }
    )
    assembly_rows = {}
    for key, item, assembly_type in (
        ("assembly", assembly_item, "STANDARD"),
        ("component", component_item, "STANDARD"),
        ("phantom", phantom_item, "PHANTOM"),
        ("other", other_item, "STANDARD"),
    ):
        row = await session.execute(
            mfg_tables.assembly_item.insert()
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                item_id=_as_uuid(item["id"]),
                assembly_type=assembly_type,
                build_cost=0,
                revision_control="NONE",
                backflush_default=False,
                round_up_component_qty=False,
                is_outsourced=False,
                created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                version=1,
            )
            .returning(mfg_tables.assembly_item)
        )
        assembly_rows[key] = dict(row.mappings().first())
    posting = FakePostingPort()
    balances = FakeBalancePort()
    service = ManufacturingService(session, tenant_id, actor_id=user_id, posting=posting, balances=balances)
    token = _token(identity_id, user_id, tenant_id)
    uom = (
        await session.execute(
            text("SELECT base_uom_id FROM item_master.item WHERE tenant_id = :tenant AND id = :id"),
            {"tenant": tenant_id, "id": _as_uuid(assembly_item["id"])},
        )
    ).scalar_one()
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": user_id,
        "location_id": location_id,
        "status_id": status_id,
        "used_status_id": used_status,
        "hold_status_id": hold_status,
        "assembly_item_id": _as_uuid(assembly_rows["assembly"]["id"]),
        "assembly_item_master_id": _as_uuid(assembly_item["id"]),
        "component_item_id": _as_uuid(component_item["id"]),
        "components_assembly_id": _as_uuid(assembly_rows["component"]["id"]),
        "phantom_item_id": _as_uuid(phantom_item["id"]),
        "phantom_assembly_id": _as_uuid(assembly_rows["phantom"]["id"]),
        "other_item_id": _as_uuid(other_item["id"]),
        "other_assembly_id": _as_uuid(assembly_rows["other"]["id"]),
        "assembly_extension_id": _as_uuid(assembly_rows["assembly"]["id"]),
        "uom_id": uom,
        "service": service,
        "posting": posting,
        "balances": balances,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture()
async def world(db_session: Any) -> dict[str, Any]:
    return await seed_manufacturing_world(db_session)


@pytest.fixture()
def app(db_session: Any, world: dict[str, Any]) -> FastAPI:
    application = FastAPI()
    install_error_handlers(application)
    application.include_router(manufacturing_api_router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    def _override_service() -> ManufacturingService:
        return world["service"]

    application.dependency_overrides[get_db] = _override_db
    application.dependency_overrides[manufacturing_service_dependency] = _override_service
    return application


@pytest.fixture()
async def client(app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
