"""Invariant DM-CONS-05 / D6 (FS-13 exit gate): consigned × assembly/manufacturing mutual exclusion."""

from __future__ import annotations

import pytest

from app.modules.consigned.errors import AssemblyNotConsignable
from app.modules.consigned.testing import seed_consigned_world

pytestmark = pytest.mark.integration


async def test_guard_validation_and_assembly_item_rejection(db_session) -> None:
    world = await seed_consigned_world(db_session, code_prefix="INVGA")
    service = world["service"]
    assert (await service.validate_guards())["ok"] is True

    from app.modules.item_master.service import ItemMasterService

    assembly = await ItemMasterService(db_session, world["tenant_id"]).create_item(
        {
            "item_number": f"INVGA-ASSEMBLY-{__import__('uuid').uuid4().hex[:8]}",
            "display_name": "Assembly",
            "item_type_code": "ASSEMBLY",
            "inventory": {"allow_vendor_consignment": True},
        }
    )
    with pytest.raises(AssemblyNotConsignable):
        agreement = await service.create_agreement({"vendor_id": str(world["vendor_id"]), "start_date": "2026-01-01"})
        await service.add_agreement_item(
            agreement["id"],
            {
                "item_id": str(assembly["id"]),
                "location_id": str(world["location_id"]),
                "consigned_status_id": str(world["consigned_status_id"]),
                "owned_status_id": str(world["owned_status_id"]),
            },
        )
    guard = await service.validate_guards()
    assert guard["ok"] is False
    assert "ASSEMBLY_ITEM_FLAGGED_CONSIGNABLE" in guard["reasons"]


async def test_manufacturing_enabled_rejects_consignment(db_session) -> None:
    world = await seed_consigned_world(db_session, code_prefix="INVGM")
    from sqlalchemy import text

    await db_session.execute(
        text(
            "INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source) VALUES (gen_random_uuid(), :t, 'manufacturing.execution.enabled', true, 'TEST')"
        ),
        {"t": world["tenant_id"]},
    )
    guard = await world["service"].validate_guards()
    assert guard["ok"] is False
    assert "MANUFACTURING_ENABLED" in guard["reasons"]
