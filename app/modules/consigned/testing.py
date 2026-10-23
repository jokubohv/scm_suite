"""Developer/test seeding helper for FS-13 consigned integration & invariant suites."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, text

from app.api.deps import token_service
from app.core.models.platform import app_user, tenant, user_identity
from app.modules.consigned.service import ConsignedService

CONSIGNED_SCOPES = frozenset(
    {
        "consigned.read",
        "consigned.agreement.write",
        "consigned.agreement.manage",
        "consigned.transfer.write",
        "consigned.ownership.transfer",
        "consigned.admin",
        "inventory.balance.read",
    }
)


def _token(identity_id: uuid.UUID, app_user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=CONSIGNED_SCOPES,
    )
    return token


async def _insert_location(session: Any, tenant_id: uuid.UUID, *, code: str) -> uuid.UUID:
    from app.modules.traceability.tables import location

    location_id = uuid.uuid4()
    await session.execute(
        insert(location).values(
            id=location_id,
            tenant_id=tenant_id,
            code=code,
            name=code,
            location_type="WAREHOUSE",
            is_bins_enabled=False,
            is_statuses_enabled=True,
            is_consignment_allowed=True,
            allow_commit_from_non_available=False,
            timezone="UTC",
            is_unallocated=False,
            is_inactive=False,
        )
    )
    return location_id


async def _insert_status(
    session: Any, tenant_id: uuid.UUID, *, code: str, consigned: bool, default: bool = False
) -> uuid.UUID:
    from app.modules.traceability.tables import inventory_status

    status_id = uuid.uuid4()
    await session.execute(
        insert(inventory_status).values(
            id=status_id,
            tenant_id=tenant_id,
            code=code,
            name=code,
            is_available_for_allocation=True,
            is_committable=True,
            is_sellable=not consigned,
            is_consigned=consigned,
            is_default=default,
            is_system=True,
            is_active=True,
        )
    )
    return status_id


async def seed_consigned_world(session: Any, *, code_prefix: str = "CONS") -> dict[str, Any]:
    from app.modules.item_master.service import ItemMasterService
    from app.modules.procurement.service import ProcurementService

    tenant_id = uuid.uuid4()
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"{code_prefix}{uuid.uuid4().hex[:6].upper()}",
            name="Consigned Test Tenant",
            status="ACTIVE",
            base_currency_code="USD",
        )
    )
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=f"cons-{uuid.uuid4().hex[:8]}@test.local",
            display_name="Consigned Tester",
            status="ACTIVE",
        )
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"cons-{uuid.uuid4().hex[:8]}@test.local",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    for flag_key in ("consigned.inventory.enabled", "consigned.assembly_incompatible_guard", "procurement.p2p.enabled"):
        await session.execute(
            text(
                "INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source) VALUES (:id,:t,:f,true,'TEST') ON CONFLICT (tenant_id, flag_key) DO UPDATE SET is_enabled=true"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "f": flag_key},
        )
    location_id = await _insert_location(session, tenant_id, code=f"{code_prefix}-WH")
    owned_status = await _insert_status(session, tenant_id, code="AVAILABLE", consigned=False, default=True)
    consigned_status = await _insert_status(session, tenant_id, code="CONSIGNED_GOOD", consigned=True)
    item_service = ItemMasterService(session, tenant_id)
    item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-ITEM-{uuid.uuid4().hex[:8]}",
            "display_name": "Consigned Item",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "5", "allow_vendor_consignment": True},
            "purchasing": {"purchase_price": "5", "purchase_price_currency": "USD"},
        }
    )
    vendor = await ProcurementService(session, tenant_id).create_vendor(
        {
            "vendor_number": f"CV-{uuid.uuid4().hex[:8].upper()}",
            "name": f"Consigned Vendor {uuid.uuid4().hex[:8]}",
            "currency_code": "USD",
        },
        actor_id=str(user_id),
    )
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": user_id,
        "item_id": uuid.UUID(item["id"]),
        "location_id": location_id,
        "vendor_id": uuid.UUID(vendor["id"]),
        "owned_status_id": owned_status,
        "consigned_status_id": consigned_status,
        "headers": {"Authorization": f"Bearer {_token(identity_id, user_id, tenant_id)}"},
        "scopes": CONSIGNED_SCOPES,
        "service": ConsignedService(session, tenant_id),
    }
