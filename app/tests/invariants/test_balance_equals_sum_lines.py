"""FS-04 INV-03/INV-07: every balance position equals the SUM of its ledger lines (reconcile-exact)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.inventory_ledger.service import LedgerService
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


async def test_balance_equals_sum_of_lines_per_position(db_session) -> None:
    world = await seed_world(db_session)
    service = LedgerService(db_session, world["tenant_id"])
    for index, qty in enumerate(("8", "-2", "4")):
        await service.post({
            "posting_type": "T-07", "transaction_date": "2026-10-23",
            "idempotency_key": f"reconcile-{index}",
            "movements": [{
                "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
                "inventory_status_id": str(world["status_id"]), "qty": qty, "movement_type": "ADJUSTMENT",
            }],
        })
    drift = (
        await db_session.execute(
            text("""
                WITH ledger AS (
                    SELECT l.item_id, l.location_id, l.inventory_status_id, l.ownership, l.qty_class,
                           sum(l.qty_base) AS qty
                    FROM inventory_ledger.inventory_transaction_line l
                    WHERE l.tenant_id = :tenant
                    GROUP BY l.item_id, l.location_id, l.inventory_status_id, l.ownership, l.qty_class
                )
                SELECT b.item_id, b.location_id, b.qty_on_hand, coalesce(ledger.qty, 0) AS ledger_qty
                FROM inventory_ledger.inventory_balance b
                LEFT JOIN ledger
                  ON ledger.item_id = b.item_id AND ledger.location_id = b.location_id
                 AND ledger.inventory_status_id = b.inventory_status_id
                 AND ledger.ownership = b.ownership AND ledger.qty_class = b.qty_class
                WHERE b.tenant_id = :tenant AND b.qty_on_hand <> coalesce(ledger.qty, 0)
            """),
            {"tenant": world["tenant_id"]},
        )
    ).all()
    assert drift == []
    committed = (
        await db_session.execute(
            text("SELECT qty_committed FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant"),
            {"tenant": world["tenant_id"]},
        )
    ).scalars().all()
    assert all(Decimal(str(value)) >= 0 for value in committed)
