"""FS-04↔FS-05 end-to-end: PostingPort values the movement and persists cost entries atomically."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.modules.inventory_ledger.facade import PostingPort
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


async def test_posting_port_persists_costing_entries(db_session) -> None:
    world = await seed_world(db_session, code_prefix="COST")
    port = PostingPort(db_session, world["tenant_id"])
    result = await port.post_receipt(
        transaction_date="2026-10-23",
        source_doc_type="PURCHASE_RECEIPT",
        source_doc_id=None,
        idempotency_key="costing-adapter-1",
        movements=[{
            "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
            "inventory_status_id": str(world["status_id"]), "qty": "2", "unit_cost": "10",
            "movement_type": "RECEIPT", "value_base": "20",
        }],
    )
    assert result["transaction_id"]
    async with db_session.begin_nested():
        pass  # keep transaction state clean
    entries = (
        await db_session.execute(
            text(
                "SELECT count(*), coalesce(sum(value_delta), 0) FROM costing.cost_ledger_entry "
                "WHERE tenant_id = :tenant AND inventory_transaction_id = :txn"
            ),
            {"tenant": world["tenant_id"], "txn": __import__("uuid").UUID(result["transaction_id"])},
        )
    ).one()
    assert int(entries[0]) >= 1
    assert Decimal(str(entries[1])) != Decimal("0")
    line = (
        await db_session.execute(
            text(
                "SELECT value_base FROM inventory_ledger.inventory_transaction_line "
                "WHERE tenant_id = :tenant AND inventory_transaction_id = :txn ORDER BY line_number LIMIT 1"
            ),
            {"tenant": world["tenant_id"], "txn": __import__("uuid").UUID(result["transaction_id"])},
        )
    ).scalar_one()
    assert Decimal(str(line)) != Decimal("0")
