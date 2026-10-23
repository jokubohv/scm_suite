"""FS-11 invariant: a blocked bin can never be picked from (DM-WMS-03/DM-WMS-06).

Completion resolves the actual bin from the task payload/recommendation; if that bin has an active
``wms.bin_block`` the engine raises ``BIN_BLOCKED`` before any ledger posting, and the guard is released
by releasing the block (not by deleting evidence).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.wms.errors import BinBlocked
from app.modules.wms.service import WmsService
from app.tests.integration.wms.conftest import TENANT_ID, balance_on_hand, seed_stock, seed_wms_world
from app.tests.invariants.test_wave_task_completion import _release_one_line_task

pytestmark = pytest.mark.integration


@pytest.fixture()
async def blocked_world(db_session: Any, requires_db: Any) -> dict[str, Any]:
    world = await seed_wms_world(db_session)
    await seed_stock(db_session, item_id=world["item_id"], bin_id=world["pick_bin"], qty="5")
    service = WmsService(db_session, TENANT_ID)
    block = await service.create_bin_block(
        {"bin_id": world["pick_bin"], "reason": "quality hold"}, actor_id=world["app_user_id"]
    )
    _service, task = await _release_one_line_task(db_session, world)
    return {"service": service, "task": task, "block": block, **world}


async def test_blocked_bin_pick_is_refused_then_allowed_after_release(
    db_session: Any, blocked_world: dict[str, Any]
) -> None:
    service: WmsService = blocked_world["service"]
    task = blocked_world["task"]
    payload = {"lines": [{"line_number": 1, "qty_picked": "2", "actual_bin_id": blocked_world["pick_bin"]}]}
    with pytest.raises(BinBlocked):
        await service.complete_pick_task(task["id"], payload, actor_id=blocked_world["app_user_id"])
    assert await balance_on_hand(db_session, blocked_world["item_id"]) == "5.000000000000"
    fresh = await service.get_pick_task(task["id"])
    assert fresh["status"] == "IN_PROGRESS" and fresh["posted_transaction_id"] is None
    released = await service.release_bin_block(blocked_world["block"]["id"], actor_id=blocked_world["app_user_id"])
    assert released["released_at"] is not None
    completed = await service.complete_pick_task(task["id"], payload, actor_id=blocked_world["app_user_id"])
    assert completed["status"] == "PICKED"
    assert await balance_on_hand(db_session, blocked_world["item_id"]) == "3.000000000000"
