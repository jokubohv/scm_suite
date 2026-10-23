"""FS-04 INV-01/INV-03: signed line quantities conserve and equal materialised balances."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.inventory_ledger.service import LedgerService
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


def _movement(world, qty, movement_type="ADJUSTMENT"):
    return {
        "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]), "qty": qty, "movement_type": movement_type,
    }


async def test_line_quantities_equal_header_totals_and_balances(db_session) -> None:
    world = await seed_world(db_session)
    service = LedgerService(db_session, world["tenant_id"])
    first = await service.post({"posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": "inv-qty-1", "movements": [_movement(world, "10")]})
    second = await service.post({"posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": "inv-qty-2", "movements": [_movement(world, "-4")]})
    assert first["transaction_id"] and second["transaction_id"]

    totals = (
        await db_session.execute(
            text("""
                SELECT t.id, t.total_qty_base, coalesce(sum(l.qty_base), 0) AS line_total
                FROM inventory_ledger.inventory_transaction t
                LEFT JOIN inventory_ledger.inventory_transaction_line l
                  ON l.tenant_id = t.tenant_id AND l.inventory_transaction_id = t.id
                WHERE t.tenant_id = :tenant GROUP BY t.id, t.total_qty_base
            """),
            {"tenant": world["tenant_id"]},
        )
    ).all()
    for row in totals:
        assert Decimal(str(row.line_total)) == Decimal(str(row.total_qty_base))

    balance_total = (
        await db_session.execute(
            text("SELECT coalesce(sum(qty_on_hand), 0) FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant AND qty_class = 'ON_HAND'"),
            {"tenant": world["tenant_id"]},
        )
    ).scalar_one()
    line_total = (
        await db_session.execute(
            text("""
                SELECT coalesce(sum(l.qty_base), 0)
                FROM inventory_ledger.inventory_transaction_line l
                WHERE l.tenant_id = :tenant
            """),
            {"tenant": world["tenant_id"]},
        )
    ).scalar_one()
    assert Decimal(str(balance_total)) == Decimal(str(line_total)) == Decimal("6")
