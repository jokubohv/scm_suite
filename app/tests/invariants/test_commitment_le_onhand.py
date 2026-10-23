"""FS-04 INV-04/INV-07: commitments never exceed on-hand and availability stays non-negative."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.inventory_ledger.documents import LedgerDocuments
from app.modules.inventory_ledger.errors import InsufficientAvailable
from app.modules.inventory_ledger.service import LedgerService
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


async def test_commitment_guards_and_rollups(db_session) -> None:
    world = await seed_world(db_session)
    ledger = LedgerService(db_session, world["tenant_id"])
    documents = LedgerDocuments(ledger)
    await ledger.post({
        "posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": "commit-inv-seed",
        "movements": [{
            "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
            "inventory_status_id": str(world["status_id"]), "qty": "10", "movement_type": "ADJUSTMENT",
        }],
    })
    commitment = await documents.commit({
        "source_type": "SALES_ORDER", "source_line_ref": "INV-SO-1", "item_id": str(world["item_id"]),
        "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]), "qty": "6",
    }, idempotency_key="commit-inv-1")
    assert commitment["qty_committed"] == "6"
    with pytest.raises(InsufficientAvailable):
        await documents.commit({
            "source_type": "SALES_ORDER", "source_line_ref": "INV-SO-2", "item_id": str(world["item_id"]),
            "location_id": str(world["location_id"]), "inventory_status_id": str(world["status_id"]), "qty": "5",
        }, idempotency_key="commit-inv-2")
    rows = (
        await db_session.execute(
            text("""
                SELECT b.qty_on_hand, b.qty_committed, b.qty_available,
                       (SELECT coalesce(sum(c.qty_committed), 0) FROM inventory_ledger.inventory_commitment c
                        WHERE c.tenant_id = b.tenant_id AND c.status = 'ACTIVE') AS commitment_sum
                FROM inventory_ledger.inventory_balance b WHERE b.tenant_id = :tenant
            """),
            {"tenant": world["tenant_id"]},
        )
    ).mappings().all()
    for row in rows:
        assert Decimal(str(row["qty_committed"])) <= Decimal(str(row["qty_on_hand"]))
        assert Decimal(str(row["qty_available"])) >= 0
        assert Decimal(str(row["commitment_sum"])) <= Decimal(str(row["qty_on_hand"]))
