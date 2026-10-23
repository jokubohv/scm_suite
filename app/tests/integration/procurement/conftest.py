"""Fixtures for FS-08 procurement integration tests (live Postgres, graceful skip)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from app.api.deps import get_db, token_service
from app.api.errors import install_error_handlers
from app.api.routers.procurement import router
from app.modules.procurement.testing import seed_procurement_world

PROCUREMENT_SCOPES = frozenset(
    {
        "procurement.vendor.read", "procurement.vendor.write", "procurement.vendor.merge",
        "procurement.contract.read", "procurement.contract.write", "procurement.contract.approve",
        "procurement.requisition.read", "procurement.requisition.write", "procurement.requisition.approve",
        "procurement.requisition.order", "procurement.request.read", "procurement.request.write",
        "procurement.request.approve", "procurement.rfq.read", "procurement.rfq.write",
        "procurement.rfq.award", "procurement.po.read", "procurement.po.write", "procurement.po.approve",
        "procurement.po.close", "procurement.receipt.read", "procurement.receipt.write",
        "procurement.receipt.post", "procurement.receipt.reverse", "procurement.bill.read",
        "procurement.bill.write", "procurement.bill.match", "procurement.bill.approve",
        "procurement.bill.post", "procurement.credit.read", "procurement.credit.write",
        "procurement.credit.apply", "procurement.credit.post", "procurement.return.read",
        "procurement.return.write", "procurement.return.approve", "procurement.return.ship",
        "procurement.return.credit", "procurement.return.close", "procurement.dropship.write",
        "procurement.match.override",
    }
)
FEATURE_FLAGS = (
    "procurement.p2p.enabled",
    "procurement.purchase_contracts_enabled",
    "procurement.blanket_orders_enabled",
    "procurement.requisitions_enabled",
    "procurement.purchase_requests_enabled",
    "procurement.rfq_enabled",
    "procurement.drop_ship_enabled",
    "procurement.multiple_vendors_enabled",
)


def _token(identity_id: uuid.UUID, app_user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id), app_user_id=str(app_user_id), tenant_id=str(tenant_id),
        token_version=0, scopes=PROCUREMENT_SCOPES,
    )
    return token


async def _insert_location(session: Any, tenant_id: uuid.UUID, *, code: str) -> uuid.UUID:
    location_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["location"]).location).values(
            id=location_id, tenant_id=tenant_id, code=code, name=code, location_type="WAREHOUSE",
            is_bins_enabled=False, is_statuses_enabled=False, is_consignment_allowed=False,
            allow_commit_from_non_available=False, timezone="UTC", is_unallocated=False, is_inactive=False,
        )
    )
    return location_id


async def _insert_status(session: Any, tenant_id: uuid.UUID, *, code: str = "AVAILABLE") -> uuid.UUID:
    status_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["inventory_status"]).inventory_status).values(
            id=status_id, tenant_id=tenant_id, code=code, name=code, is_available_for_allocation=True,
            is_committable=True, is_sellable=True, is_consigned=False, is_default=True, is_system=True, is_active=True,
        )
    )
    return status_id


async def seed_world(session: Any, *, code_prefix: str = "PROC") -> dict[str, Any]:
    return await seed_procurement_world(session, code_prefix=code_prefix)


@pytest.fixture()
def procurement_app(db_session: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def client(procurement_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=procurement_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture()
async def world(db_session: Any) -> dict[str, Any]:
    return await seed_procurement_world(db_session)
