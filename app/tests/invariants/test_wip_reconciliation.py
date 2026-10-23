"""INV: WIP reconciliation (DM-MFG-07) — WIP value equals issued minus completed/closed value; a WIP
work order closes only through T-19 and leaves a zero residual balance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tests.integration.manufacturing.conftest import seed_manufacturing_world

pytestmark = pytest.mark.integration


async def _wip_assembly(db_session, service, world):
    from app.modules.item_master.service import ItemMasterService

    items = ItemMasterService(db_session, world["tenant_id"])
    item = await items.create_item(
        {
            "item_number": "INVWIP-ASM",
            "display_name": "WIP Assembly",
            "item_type_code": "INVENTORY",
            "inventory": {"initial_cost": "5"},
        }
    )
    return await service.create_assembly_item({"item_id": item["id"], "assembly_type": "WIP"})


async def test_wip_value_identity_and_close(db_session) -> None:
    world = await seed_manufacturing_world(db_session, code_prefix="INVWIP")
    service = world["service"]
    wip_assembly = await _wip_assembly(db_session, service, world)
    bom = await service.create_bom({"assembly_item_id": wip_assembly["id"], "bom_code": "WIP-BOM", "name": "WIP BOM"})
    await service.activate_bom(bom["id"])
    revision = await service.create_bom_revision(bom["id"], {"revision_code": "R1", "effective_from": "2020-01-01"})
    await service.add_bom_component(
        revision["id"],
        {
            "line_number": 1,
            "component_item_id": str(world["other_item_id"]),
            "qty_per": "1",
            "uom_id": str(world["uom_id"]),
        },
    )
    await service.activate_bom_revision(revision["id"])
    work_order = await service.create_work_order(
        {
            "assembly_item_id": wip_assembly["id"],
            "qty_ordered": "10",
            "wo_type": "WIP",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        }
    )
    await service.firm_work_order(work_order["id"])
    await service.release_work_order(work_order["id"])
    await service.issue_components(work_order["id"], {"value_issued": "100"})
    await service.complete_work_order(work_order["id"], {"qty": "4", "value_completed": "40"})
    balance = await service.get_wip_balance(work_order["id"])
    assert Decimal(balance["value_issued"]) == Decimal("100")
    assert Decimal(balance["value_completed"]) == Decimal("40")
    assert Decimal(balance["wip_value"]) == Decimal("60")  # issued - completed (DM-MFG-07)
    assert Decimal(balance["qty_completed"]) == Decimal("4")

    await service.complete_work_order(work_order["id"], {"qty": "6", "value_completed": "60"})
    closed = await service.close_work_order(work_order["id"], {})
    assert closed["status"] == "CLOSED"
    final = await service.get_wip_balance(work_order["id"])
    assert Decimal(final["wip_value"]) == Decimal("0")
    assert final["status"] == "CLOSED"
    assert world["posting"].calls[-1]["posting_type"] == "T-19"
