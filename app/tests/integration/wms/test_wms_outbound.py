"""FS-11 outbound execution integration tests: wave → pick → pack → shipment → manifest."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


async def _release_single_line_wave(
    client: Any, db_session: Any, seeded: dict[str, Any], *, qty: str = "3",
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.tests.integration.wms.conftest import seed_stock

    await seed_stock(db_session, item_id=seeded["item_id"], bin_id=seeded["pick_bin"], qty="10")
    wave_response = await client.post(
        "/api/v1/waves",
        json={
            "location_id": seeded["location_id"],
            "lines": [
                {
                    "source_type": "SALES_ORDER",
                    "source_ref": "SO-1001",
                    "source_line_ref": "1",
                    "item_id": seeded["item_id"],
                    "qty": qty,
                }
            ],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert wave_response.status_code == 201, wave_response.text
    wave = wave_response.json()
    released = await client.post(f"/api/v1/waves/{wave['id']}/release", json={}, headers={"Idempotency-Key": uuid4().hex})
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "RELEASED"
    task_ids = released.json()["pick_task_ids"]
    assert len(task_ids) == 1
    task = (await client.get(f"/api/v1/pick-tasks/{task_ids[0]}")).json()
    return wave, task


async def test_wave_release_pick_completion_posts_fulfillment(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import balance_on_hand, bin_on_hand

    wave, task = await _release_single_line_wave(client, db_session, seeded)
    assert task["status"] == "PENDING"
    assert task["lines"][0]["qty_requested"].startswith("3")
    assigned = await client.post(
        f"/api/v1/pick-tasks/{task['id']}/assign", json={"assigned_user_id": seeded["app_user_id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert assigned.status_code == 200 and assigned.json()["status"] == "ASSIGNED"
    started = await client.post(f"/api/v1/pick-tasks/{task['id']}/start", headers={"Idempotency-Key": uuid4().hex})
    assert started.status_code == 200 and started.json()["status"] == "IN_PROGRESS"
    completed = await client.post(
        f"/api/v1/pick-tasks/{task['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_picked": "3", "actual_bin_id": seeded["pick_bin"]}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "PICKED"
    assert body["posted_transaction_id"]
    assert await bin_on_hand(db_session, seeded["pick_bin"]) == "7.000000000000"
    assert await balance_on_hand(db_session, seeded["item_id"]) == "7.000000000000"
    wave_detail = (await client.get(f"/api/v1/waves/{wave['id']}")).json()
    assert wave_detail["lines"][0]["status"] == "PICKED"
    generations = (await client.get("/api/v1/wms/fulfillment-generations")).json()
    assert any(row["pick_task_id"] == task["id"] for row in generations["items"])
    releases = (await client.get("/api/v1/wms/order-release-status")).json()
    assert any(row["wave_number"] == wave["wave_number"] for row in releases["items"])


async def test_partial_pick_rollup_and_short_pick(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    _wave, task = await _release_single_line_wave(client, db_session, seeded, qty="3")
    await client.post(f"/api/v1/pick-tasks/{task['id']}/assign", json={}, headers={"Idempotency-Key": uuid4().hex})
    partial = await client.post(
        f"/api/v1/pick-tasks/{task['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_picked": "2", "actual_bin_id": seeded["pick_bin"]}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "PARTIAL"
    line = partial.json()["lines"][0]
    assert line["qty_short"].startswith("1")


async def test_pick_reverse_restores_balance(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import balance_on_hand

    _wave, task = await _release_single_line_wave(client, db_session, seeded, qty="4")
    await client.post(f"/api/v1/pick-tasks/{task['id']}/assign", json={}, headers={"Idempotency-Key": uuid4().hex})
    await client.post(
        f"/api/v1/pick-tasks/{task['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_picked": "4", "actual_bin_id": seeded["pick_bin"]}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    reversed_response = await client.post(
        f"/api/v1/pick-tasks/{task['id']}/reverse", json={"reason": "order cancelled"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["status"] == "REVERSED"
    assert await balance_on_hand(db_session, seeded["item_id"]) == "10.000000000000"


async def test_pack_and_shipment_lifecycle_with_manifest(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    _wave, task = await _release_single_line_wave(client, db_session, seeded, qty="2")
    await client.post(f"/api/v1/pick-tasks/{task['id']}/assign", json={}, headers={"Idempotency-Key": uuid4().hex})
    picked = await client.post(
        f"/api/v1/pick-tasks/{task['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_picked": "2", "actual_bin_id": seeded["pick_bin"]}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert picked.status_code == 200, picked.text
    pack = await client.post(
        "/api/v1/pack-tasks",
        json={"location_id": seeded["location_id"], "pick_task_id": task["id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert pack.status_code == 201, pack.text
    carton = await client.post(
        f"/api/v1/pack-tasks/{pack.json()['id']}/cartons",
        json={
            "carton_number": "CTN-1",
            "lines": [{"pick_task_line_id": picked.json()["lines"][0]["id"], "item_id": seeded["item_id"], "qty": "2"}],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert carton.status_code == 201, carton.text
    packed = await client.post(
        f"/api/v1/pack-tasks/{pack.json()['id']}/complete", json={"package_count": 1},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert packed.status_code == 200 and packed.json()["status"] == "PACKED"

    shipment = await client.post(
        "/api/v1/shipments",
        json={
            "location_id": seeded["location_id"],
            "carrier_code": "UPS",
            "lines": [{"source_type": "SALES_ORDER", "source_ref": "SO-1001", "item_id": seeded["item_id"], "qty": "2"}],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert shipment.status_code == 201, shipment.text
    shipment_id = shipment.json()["id"]
    ready = await client.post(f"/api/v1/shipments/{shipment_id}/ready", headers={"Idempotency-Key": uuid4().hex})
    assert ready.status_code == 200, ready.text
    shipped = await client.post(f"/api/v1/shipments/{shipment_id}/ship", headers={"Idempotency-Key": uuid4().hex})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["status"] == "SHIPPED" and shipped.json()["shipped_at"]

    manifest = await client.post(
        "/api/v1/ship-manifests",
        json={"shipment_id": shipment_id, "payload": {"carrier": "UPS"}},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert manifest.status_code == 201, manifest.text
    sent = await client.post(f"/api/v1/ship-manifests/{manifest.json()['id']}/send", json={}, headers={"Idempotency-Key": uuid4().hex})
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "SENT"
    assert sent.json()["response"]["adapter"] == "null"
    manifests = (await client.get("/api/v1/ship-manifests")).json()
    assert any(row["id"] == manifest.json()["id"] for row in manifests["items"])
    detail = (await client.get("/api/v1/shipments")).json()
    assert any(row["id"] == shipment_id for row in detail["items"])


async def test_wave_cancel_blocked_by_open_task_completion(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    wave, task = await _release_single_line_wave(client, db_session, seeded, qty="1")
    await client.post(f"/api/v1/pick-tasks/{task['id']}/assign", json={}, headers={"Idempotency-Key": uuid4().hex})
    await client.post(f"/api/v1/pick-tasks/{task['id']}/start", headers={"Idempotency-Key": uuid4().hex})
    assert (await client.get(f"/api/v1/pick-tasks?wave_id={wave['id']}")).json()["items"]
    blocked = await client.post(f"/api/v1/waves/{wave['id']}/cancel", headers={"Idempotency-Key": uuid4().hex})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "WAVE_INCOMPLETE"
