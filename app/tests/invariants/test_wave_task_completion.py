"""FS-11 invariant: a wave line is complete only when its pick task is posted and complete.

Invariant (DM-WMS-05/DM-WMS-07): wave release materialises pick tasks; a pick task reaches PICKED only
after exactly one inventory fulfilment posting, and repeating completion is idempotent (no double post).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.wms.service import WmsService
from app.tests.integration.wms.conftest import TENANT_ID, seed_stock, seed_wms_world

pytestmark = pytest.mark.integration


@pytest.fixture()
async def world(db_session: Any, requires_db: Any) -> dict[str, Any]:
    return await seed_wms_world(db_session)


@pytest.fixture()
async def stocked(db_session: Any, world: dict[str, Any]) -> dict[str, Any]:
    await seed_stock(db_session, item_id=world["item_id"], bin_id=world["pick_bin"], qty="5")
    return world


async def _release_one_line_task(db_session: Any, world: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    service = WmsService(db_session, TENANT_ID)
    wave = await service.create_wave(
        {
            "location_id": world["location_id"],
            "lines": [
                {
                    "source_type": "SALES_ORDER",
                    "source_ref": "SO-INV-1",
                    "source_line_ref": "1",
                    "item_id": world["item_id"],
                    "qty": "2",
                }
            ],
        },
        actor_id=world["app_user_id"],
    )
    released = await service.release_wave(wave["id"], actor_id=world["app_user_id"])
    task = await service.get_pick_task(released["pick_task_ids"][0])
    await service.assign_pick_task(task["id"], {"assigned_user_id": world["app_user_id"]},
                                   actor_id=world["app_user_id"])
    await service.start_pick_task(task["id"], actor_id=world["app_user_id"])
    return service, task


async def test_wave_line_picked_only_after_posted_task_completion(
    db_session: Any, stocked: dict[str, Any]
) -> None:
    service, task = await _release_one_line_task(db_session, stocked)
    completed = await service.complete_pick_task(
        task["id"],
        {"lines": [{"line_number": 1, "qty_picked": "2", "actual_bin_id": stocked["pick_bin"]}]},
        actor_id=stocked["app_user_id"],
    )
    assert completed["status"] == "PICKED"
    assert completed["posted_transaction_id"]
    wave = await service.get_wave(completed["wave_id"])
    assert wave["lines"][0]["status"] == "PICKED"
    assert wave["lines"][0]["pick_task_id"] == task["id"]
    assert [row["status"] for row in wave["tasks"]] == ["PICKED"]


async def test_repeated_pick_completion_does_not_double_post(
    db_session: Any, stocked: dict[str, Any]
) -> None:
    service, task = await _release_one_line_task(db_session, stocked)
    payload = {"lines": [{"line_number": 1, "qty_picked": "2", "actual_bin_id": stocked["pick_bin"]}]}
    first = await service.complete_pick_task(task["id"], payload, actor_id=stocked["app_user_id"])
    second = await service.complete_pick_task(task["id"], payload, actor_id=stocked["app_user_id"])
    assert first["posted_transaction_id"] == second["posted_transaction_id"]
    transactions = (
        await db_session.execute(
            __import__("sqlalchemy").text(
                "SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant "
                "AND source_doc_type = 'WMS_PICK_TASK' AND source_doc_id = :task"
            ),
            {"tenant": TENANT_ID, "task": task["id"]},
        )
    ).scalar_one()
    assert int(transactions) == 1


async def test_wave_with_short_pick_still_completes_structurally(
    db_session: Any, stocked: dict[str, Any]
) -> None:
    service, task = await _release_one_line_task(db_session, stocked)
    completed = await service.complete_pick_task(
        task["id"],
        {"lines": [{"line_number": 1, "qty_picked": "1", "actual_bin_id": stocked["pick_bin"]}]},
        actor_id=stocked["app_user_id"],
    )
    from decimal import Decimal

    assert completed["status"] == "PARTIAL"
    assert Decimal(completed["lines"][0]["qty_short"]) == Decimal("1")
