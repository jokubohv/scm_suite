"""FS-05 × FS-04 end-to-end: PostingPort posts through the injected costing adapter.

Verifies the real integration contract: the ledger calls `CostingPort.assert_postable/value_movement` and
`CostingWriter.persist_value_result` inside the posting transaction, writing `costing.item_cost` /
`costing.cost_ledger_entry` atomically with the ledger transaction (INV-10/INV-17).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.costing import tables as ct
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


def _receipt_movement(world: dict, *, qty: str = "5", unit_cost: str = "4") -> dict:
    return {
        "item_id": str(world["item_id"]),
        "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]),
        "qty": qty,
        "movement_type": "RECEIPT",
        "source_line_ref": "1",
        "unit_cost": unit_cost,
    }


async def test_posting_port_persists_costing_entries(db_session) -> None:
    from app.modules.inventory_ledger.facade import PostingPort

    world = await seed_world(db_session, code_prefix="COSTLED")
    await db_session.flush()
    port = PostingPort(db_session, world["tenant_id"])

    result = await port.post_receipt(
        transaction_date=date(2026, 10, 23),
        source_doc_type="TEST_RECEIPT",
        source_doc_id=uuid.uuid4(),
        external_ref="cost-e2e-1",
        idempotency_key="cost-e2e-key-1",
        movements=[_receipt_movement(world)],
    )
    assert result["status"] == "POSTED", result

    costs = (
        (await db_session.execute(select(ct.item_cost).where(ct.item_cost.c.tenant_id == world["tenant_id"])))
        .mappings()
        .all()
    )
    assert len(costs) == 1, costs
    cost = costs[0]
    assert cost["item_id"] == world["item_id"]
    assert cost["costing_method"] == "AVERAGE"
    assert Decimal(str(cost["qty_on_hand"])) == Decimal("5")
    assert Decimal(str(cost["value_base"])) == Decimal("20")

    entries = (
        (await db_session.execute(select(ct.cost_ledger_entry).where(ct.cost_ledger_entry.c.tenant_id == world["tenant_id"])))
        .mappings()
        .all()
    )
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["movement_type"] == "IN"
    assert Decimal(str(entry["value_delta"])) == Decimal("20")
    assert entry["inventory_transaction_line_id"] is not None
    assert entry["inventory_transaction_id"] is not None


async def test_costing_settings_reads_tenant_setting_table(db_session) -> None:
    from app.modules.costing.service import CostingService

    world = await seed_world(db_session, code_prefix="COSTSET")
    settings = await CostingService(db_session, world["tenant_id"]).settings()
    assert settings.block_when_recalc_pending is True
    assert settings.allow_provisional is False
    assert settings.rounding_mode == "PER_MOVEMENT"
