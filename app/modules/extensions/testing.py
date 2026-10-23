"""Developer/test seeding helper for FS-14 extensions integration & invariant suites."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import insert, text

from app.api.deps import token_service
from app.core.models.platform import app_user, tenant, user_identity
from app.modules.extensions.service import ExtensionService

EXTENSION_SCOPES = frozenset(
    {
        "extensions.read",
        "extensions.warranty.write",
        "extensions.claims.write",
        "extensions.customerpart.write",
        "extensions.upc.write",
        "platform.flag.manage",
        "warranty.manage",
        "warranty.claim.manage",
        "upc.manage",
        "upc.batch_assign",
        "customerpart.manage",
        "items.read",
        "items.update",
        "items.barcode.manage",
    }
)


def _token(identity_id: uuid.UUID, app_user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=EXTENSION_SCOPES,
    )
    return token


async def seed_extensions_world(session: Any, *, code_prefix: str = "EXT") -> dict[str, Any]:
    from app.modules.item_master.service import ItemMasterService
    from app.modules.procurement.service import ProcurementService
    from app.modules.traceability.tables import location

    tenant_id = uuid.uuid4()
    identity_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"{code_prefix}{uuid.uuid4().hex[:6].upper()}",
            name="Extensions Test Tenant",
            status="ACTIVE",
            base_currency_code="USD",
        )
    )
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=f"ext-{uuid.uuid4().hex[:8]}@test.local",
            display_name="Extensions Tester",
            status="ACTIVE",
        )
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"ext-{uuid.uuid4().hex[:8]}@test.local",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    for flag_key in ("extensions.registry.enabled", "extensions.upc.auto_assign", "procurement.p2p.enabled"):
        await session.execute(
            text(
                "INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source) VALUES (:id,:t,:f,true,'TEST') ON CONFLICT (tenant_id, flag_key) DO UPDATE SET is_enabled=true"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "f": flag_key},
        )
    await session.execute(
        text(
            "INSERT INTO platform.tenant_setting (id, tenant_id, namespace, key, value, value_type, is_default) VALUES (:id,:t,'inventory','extensions.upc.company_prefix','\"123456\"'::jsonb,'string',false) ON CONFLICT (tenant_id, namespace, key) DO UPDATE SET value='\"123456\"'::jsonb"
        ),
        {"id": uuid.uuid4(), "t": tenant_id},
    )
    location_id = uuid.uuid4()
    await session.execute(
        insert(location).values(
            id=location_id,
            tenant_id=tenant_id,
            code=f"{code_prefix}-WH",
            name="WH",
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
    item_service = ItemMasterService(session, tenant_id)
    item = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-ITEM-{uuid.uuid4().hex[:8]}",
            "display_name": "Warranty Item",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "5"},
        }
    )
    serialized = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-SER-{uuid.uuid4().hex[:8]}",
            "display_name": "Serialized Item",
            "item_type_code": "SERIALIZED",
            "inventory": {"initial_cost": "5"},
        }
    )
    assembly = await item_service.create_item(
        {
            "item_number": f"{code_prefix}-ASM-{uuid.uuid4().hex[:8]}",
            "display_name": "Assembly Item",
            "item_type_code": "ASSEMBLY",
        }
    )
    vendor = await ProcurementService(session, tenant_id).create_vendor(
        {
            "vendor_number": f"EV-{uuid.uuid4().hex[:8].upper()}",
            "name": f"Ext Vendor {uuid.uuid4().hex[:8]}",
            "currency_code": "USD",
        },
        actor_id=str(user_id),
    )
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": user_id,
        "item_id": uuid.UUID(item["id"]),
        "serialized_item_id": uuid.UUID(serialized["id"]),
        "assembly_item_id": uuid.UUID(assembly["id"]),
        "location_id": location_id,
        "vendor_id": uuid.UUID(vendor["id"]),
        "headers": {"Authorization": f"Bearer {_token(identity_id, user_id, tenant_id)}"},
        "scopes": EXTENSION_SCOPES,
        "service": ExtensionService(session, tenant_id),
    }
