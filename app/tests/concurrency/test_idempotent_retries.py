"""FS-04 concurrency: retries with the same Idempotency-Key produce exactly one posting."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory_ledger.service import LedgerService
from app.tests.integration.inventory_ledger.conftest import seed_world

pytestmark = pytest.mark.integration


async def _seed_committed(engine):
    async with AsyncSession(engine) as session:
        world = await seed_world(session, code_prefix="IDEM")
        await session.commit()
    return world


async def _post(engine, world, key: str) -> dict:
    async with AsyncSession(engine) as session:
        service = LedgerService(session, world["tenant_id"])
        result = await service.post({
            "posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": key,
            "movements": [{
                "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
                "inventory_status_id": str(world["status_id"]), "qty": "5", "movement_type": "ADJUSTMENT",
            }],
        })
        await session.commit()
        return result


async def test_concurrent_same_key_posts_once(db_engine) -> None:
    world = await _seed_committed(db_engine)
    first, second = await asyncio.gather(
        _post(db_engine, world, "idem-concurrent"),
        _post(db_engine, world, "idem-concurrent"),
    )
    assert first["transaction_id"] == second["transaction_id"]
    assert second.get("replayed") is True
    async with AsyncSession(db_engine) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant AND idempotency_key = 'idem-concurrent'"),
                {"tenant": world["tenant_id"]},
            )
        ).scalar_one()
        balance = (
            await session.execute(
                text("SELECT qty_on_hand FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant AND item_id = :item"),
                {"tenant": world["tenant_id"], "item": world["item_id"]},
            )
        ).scalar_one()
    assert int(count) == 1
    assert str(balance) in {"5.000000000000", "5"}


async def test_sequential_retry_returns_original_transaction(db_engine) -> None:
    world = await _seed_committed(db_engine)
    first = await _post(db_engine, world, "idem-sequential")
    retry = await _post(db_engine, world, "idem-sequential")
    assert retry["transaction_id"] == first["transaction_id"]
    assert retry.get("replayed") is True
