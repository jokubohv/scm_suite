"""Fixtures for FS-04 inventory-ledger integration tests (live Postgres; graceful skip)."""

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
from app.api.routers.inventory_ledger import router
from app.core.models.platform import app_user, tenant, user_identity

LEDGER_SCOPES = frozenset(
    scope for scope in (
        "inventory.transaction.read", "inventory.transaction.post", "inventory.transaction.reverse",
        "inventory.adjustment.create", "inventory.adjustment.post", "inventory.worksheet.create",
        "inventory.worksheet.post", "inventory.transfer.create", "inventory.transfer.post",
        "inventory.to.create", "inventory.to.approve", "inventory.to.fulfil", "inventory.to.receive",
        "inventory.to.close", "inventory.commitment.commit", "inventory.commitment.release",
        "inventory.commitment.reallocate", "inventory.fulfillment.post", "inventory.return.post",
        "inventory.balance.read", "inventory.distribution.create", "inventory.distribution.post",
        "inventory.import.create", "inventory.import.post", "inventory.reason.manage",
    )
)


def _token(identity_id: uuid.UUID, app_user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id), app_user_id=str(app_user_id), tenant_id=str(tenant_id),
        token_version=0, scopes=LEDGER_SCOPES,
    )
    return token


async def _insert_location(session: Any, tenant_id: uuid.UUID, *, code: str, unallocated: bool = False) -> uuid.UUID:
    location_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["location"]).location).values(
            id=location_id, tenant_id=tenant_id, code=code, name=code, location_type="UNALLOCATED" if unallocated else "WAREHOUSE",
            is_bins_enabled=False, is_statuses_enabled=False, is_consignment_allowed=False,
            allow_commit_from_non_available=False, timezone="UTC", is_unallocated=unallocated, is_inactive=False,
        )
    )
    return location_id


async def _insert_status(session: Any, tenant_id: uuid.UUID, *, code: str = "AVAILABLE") -> uuid.UUID:
    status_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["inventory_status"]).inventory_status).values(
            id=status_id, tenant_id=tenant_id, code=code, name=code, is_available_for_allocation=True,
            is_committable=True, is_sellable=True, is_consigned=False, is_default=(code == "AVAILABLE"), is_system=True, is_active=True,
        )
    )
    return status_id


async def seed_world(session: Any, *, code_prefix: str = "LED") -> dict[str, Any]:
    from app.modules.item_master.service import ItemMasterService

    tenant_id = uuid.uuid4()
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(tenant).values(id=tenant_id, tenant_id=tenant_id, code=f"{code_prefix}{uuid.uuid4().hex[:6].upper()}", name="Ledger Test Tenant", status="ACTIVE", base_currency_code="USD")
    )
    await session.execute(insert(user_identity).values(id=identity_id, email=f"ledger-{uuid.uuid4().hex[:8]}@test.local", display_name="Ledger Tester", status="ACTIVE"))
    await session.execute(insert(app_user).values(id=user_id, tenant_id=tenant_id, identity_id=identity_id, email=f"ledger-{uuid.uuid4().hex[:8]}@test.local", status="ACTIVE", membership_status="ACTIVE", token_version=0))
    location_id = await _insert_location(session, tenant_id, code=f"{code_prefix}-WH-{uuid.uuid4().hex[:5]}")
    status_id = await _insert_status(session, tenant_id)
    item_service = ItemMasterService(session, tenant_id)
    item = await item_service.create_item({"item_number": f"{code_prefix}-ITEM-{uuid.uuid4().hex[:8]}", "display_name": "Ledger Item", "item_type_code": "INVENTORY"})
    token = _token(identity_id, user_id, tenant_id)
    return {
        "tenant_id": tenant_id, "identity_id": identity_id, "app_user_id": user_id,
        "item_id": uuid.UUID(item["id"]), "item_number": item["item_number"],
        "location_id": location_id, "status_id": status_id,
        "headers": {"Authorization": f"Bearer {token}"}, "scopes": LEDGER_SCOPES,
    }


@pytest.fixture()
def ledger_app(db_session: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def ledger_client(ledger_app: FastAPI, requires_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=ledger_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def world(db_session: Any) -> dict[str, Any]:
    return await seed_world(db_session)
