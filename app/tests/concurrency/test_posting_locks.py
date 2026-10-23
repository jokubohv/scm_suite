"""FS-04 concurrency: parallel postings on one position serialise with no lost updates."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory_ledger.service import LedgerService
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


async def _seed_committed(engine):
    async with AsyncSession(engine) as session:
        world = await seed_world(session, code_prefix="CONC")
        await session.commit()
    return world


async def _allocate(engine, world, *, qty: str, key: str) -> dict:
    async with AsyncSession(engine) as session:
        service = LedgerService(session, world["tenant_id"])
        result = await service.post({
            "posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": key,
            "movements": [{
                "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
                "inventory_status_id": str(world["status_id"]), "qty": qty, "movement_type": "ADJUSTMENT",
            }],
        })
        await session.commit()
        return result


async def test_parallel_postings_have_unique_sequences_and_summed_balance(db_engine) -> None:
    world = await _seed_committed(db_engine)
    results = await asyncio.gather(
        _allocate(db_engine, world, qty="10", key="conc-1"),
        _allocate(db_engine, world, qty="7", key="conc-2"),
        _allocate(db_engine, world, qty="3", key="conc-3"),
        _allocate(db_engine, world, qty="5", key="conc-4"),
    )
    sequences = sorted(result["ledger_sequence"] for result in results)
    assert sequences == [1, 2, 3, 4]
    assert len({result["transaction_id"] for result in results}) == 4
    async with AsyncSession(db_engine) as session:
        balance = (
            await session.execute(
                text("SELECT qty_on_hand FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant AND item_id = :item AND location_id = :location"),
                {"tenant": world["tenant_id"], "item": world["item_id"], "location": world["location_id"]},
            )
        ).scalar_one()
        count = (
            await session.execute(
                text("SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant"),
                {"tenant": world["tenant_id"]},
            )
        ).scalar_one()
    assert Decimal(str(balance)) == Decimal("25")
    assert int(count) == 4
