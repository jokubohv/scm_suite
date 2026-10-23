"""Test/developer helpers for FS-08 procurement (seeding a minimal tenant world)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, text

from app.api.deps import token_service
from app.core.models.platform import app_user, tenant, user_identity
from app.modules.procurement.service import ProcurementService

PROCUREMENT_SCOPES = frozenset(
    {
        "procurement.vendor.read",
        "procurement.vendor.write",
        "procurement.vendor.merge",
        "procurement.contract.read",
        "procurement.contract.write",
        "procurement.contract.approve",
        "procurement.requisition.read",
        "procurement.requisition.write",
        "procurement.requisition.approve",
        "procurement.requisition.order",
        "procurement.request.read",
        "procurement.request.write",
        "procurement.request.approve",
        "procurement.rfq.read",
        "procurement.rfq.write",
        "procurement.rfq.award",
        "procurement.po.read",
        "procurement.po.write",
        "procurement.po.approve",
        "procurement.po.close",
        "procurement.receipt.read",
        "procurement.receipt.write",
        "procurement.receipt.post",
        "procurement.receipt.reverse",
        "procurement.bill.read",
        "procurement.bill.write",
        "procurement.bill.match",
        "procurement.bill.approve",
        "procurement.bill.post",
        "procurement.credit.read",
        "procurement.credit.write",
        "procurement.credit.apply",
        "procurement.credit.post",
        "procurement.return.read",
        "procurement.return.write",
        "procurement.return.approve",
        "procurement.return.ship",
        "procurement.return.credit",
        "procurement.return.close",
        "procurement.dropship.write",
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
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=PROCUREMENT_SCOPES,
    )
    return token


async def _insert_location(session: Any, tenant_id: uuid.UUID, *, code: str) -> uuid.UUID:
    location_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["location"]).location).values(
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


async def _insert_status(session: Any, tenant_id: uuid.UUID, *, code: str = "AVAILABLE") -> uuid.UUID:
    status_id = uuid.uuid4()
    await session.execute(
        insert(__import__("app.modules.traceability.tables", fromlist=["inventory_status"]).inventory_status).values(
            id=status_id,
            tenant_id=tenant_id,
            code=code,
            name=code,
            is_available_for_allocation=True,
            is_committable=True,
            is_sellable=True,
            is_consigned=False,
            is_default=True,
            is_system=True,
            is_active=True,
        )
    )
    return status_id


async def seed_procurement_world(session: Any, *, code_prefix: str = "PROC") -> dict[str, Any]:
    from app.modules.item_master.service import ItemMasterService

    tenant_id = uuid.uuid4()
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"{code_prefix}{uuid.uuid4().hex[:6].upper()}",
            name="Procurement Test Tenant",
            status="ACTIVE",
            base_currency_code="USD",
        )
    )
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=f"proc-{uuid.uuid4().hex[:8]}@test.local",
            display_name="Procurement Tester",
            status="ACTIVE",
        )
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"proc-{uuid.uuid4().hex[:8]}@test.local",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    for flag_key in FEATURE_FLAGS:
        await session.execute(
            text(
                "INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source) "
                "VALUES (:id, :tenant, :flag, true, 'TEST') ON CONFLICT (tenant_id, flag_key) DO UPDATE SET is_enabled = true"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id, "flag": flag_key},
        )
    location_id = await _insert_location(session, tenant_id, code=f"{code_prefix}-WH-{uuid.uuid4().hex[:5]}")
    status_id = await _insert_status(session, tenant_id)
    item_service = ItemMasterService(session, tenant_id)
    item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-ITEM-{uuid.uuid4().hex[:8]}",
            "display_name": "Procurement Item",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "5"},
            "purchasing": {"purchase_price": "5", "purchase_price_currency": "USD"},
        }
    )
    service = ProcurementService(session, tenant_id)
    vendor = await service.create_vendor(
        {
            "vendor_number": f"V-{uuid.uuid4().hex[:8].upper()}",
            "name": f"Vendor {uuid.uuid4().hex[:8]}",
            "currency_code": "USD",
        },
        actor_id=str(user_id),
    )
    token = _token(identity_id, user_id, tenant_id)
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": user_id,
        "item_id": uuid.UUID(item["id"]),
        "item_number": item["item_number"],
        "location_id": location_id,
        "status_id": status_id,
        "vendor_id": uuid.UUID(vendor["id"]),
        "vendor": vendor,
        "headers": {"Authorization": f"Bearer {token}"},
        "scopes": PROCUREMENT_SCOPES,
        "service": service,
    }
