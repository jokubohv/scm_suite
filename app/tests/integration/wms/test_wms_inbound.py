"""FS-11 inbound processing integration tests: ASN → receiving (procurement receipt bridge) → putaway."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


async def _create_asn(client: Any, seeded: dict[str, Any], qty: str = "4") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/inbound-shipments",
        json={
            "location_id": seeded["location_id"],
            "vendor_id": seeded["vendor_id"],
            "carrier_code": "UPS",
            "expected_arrival_date": "2026-10-23",
            "lines": [
                {"item_id": seeded["item_id"], "qty_expected": qty, "unit_cost": "3.5"},
            ],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_inbound_receiving_and_putaway_posts_inventory(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import bin_on_hand

    asn = await _create_asn(client, seeded)
    arrived = await client.post(f"/api/v1/inbound-shipments/{asn['id']}/arrive", headers={"Idempotency-Key": uuid4().hex})
    assert arrived.status_code == 200, arrived.text
    assert arrived.json()["status"] == "ARRIVED"

    order_response = await client.post(
        "/api/v1/receiving-orders",
        json={"inbound_shipment_id": asn["id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()
    assert len(order["lines"]) == 1
    started = await client.post(f"/api/v1/receiving-orders/{order['id']}/start", headers={"Idempotency-Key": uuid4().hex})
    assert started.status_code == 200, started.text
    completed = await client.post(
        f"/api/v1/receiving-orders/{order['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_received": "4"}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "COMPLETED"
    assert body["item_receipt"]["status"] == "POSTED"
    assert body["item_receipt"]["lines"][0]["qty_received"].startswith("4")
    tasks = await client.get("/api/v1/putaway-tasks?status=PENDING")
    assert tasks.status_code == 200
    putaway_tasks = tasks.json()["items"]
    assert len(putaway_tasks) == 1
    task = putaway_tasks[0]
    assert task["source_bin_id"] == seeded["receiving_bin"]

    assigned_task = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/assign", json={"assigned_user_id": seeded["app_user_id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert assigned_task.status_code == 200, assigned_task.text
    started_task = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/start", headers={"Idempotency-Key": uuid4().hex}
    )
    assert started_task.status_code == 200, started_task.text
    completed_task = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/complete", json={}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert completed_task.status_code == 200, completed_task.text
    assert completed_task.json()["status"] == "COMPLETED"
    assert completed_task.json()["posted_transaction_id"]
    assert await bin_on_hand(db_session, seeded["receiving_bin"]) == "0.000000000000"
    assert await bin_on_hand(db_session, seeded["storage_bin"]) == "4.000000000000"
    detail = await client.get(f"/api/v1/putaway-tasks/{task['id']}")
    assert detail.json()["lines"][0]["qty"].startswith("4")


async def test_deferred_receipt_posting_leaves_receipt_pending(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import bin_on_hand

    asn = await _create_asn(client, seeded, qty="2")
    order_response = await client.post(
        "/api/v1/receiving-orders",
        json={"inbound_shipment_id": asn["id"], "deferred_posting": True},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()
    completed = await client.post(
        f"/api/v1/receiving-orders/{order['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_received": "2"}], "generate_putaway": False},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["item_receipt"]["status"] == "PENDING_POSTING"
    assert await bin_on_hand(db_session, seeded["receiving_bin"]) == "0.000000000000"


async def test_receiving_reversal_reverses_posted_receipt(
    client: Any, db_session: Any, seeded: dict[str, Any]
) -> None:
    from app.tests.integration.wms.conftest import bin_on_hand

    asn = await _create_asn(client, seeded, qty="2")
    order_response = await client.post(
        "/api/v1/receiving-orders",
        json={"inbound_shipment_id": asn["id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()
    completed = await client.post(
        f"/api/v1/receiving-orders/{order['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_received": "2"}], "generate_putaway": False},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["item_receipt"]["status"] == "POSTED"
    assert await bin_on_hand(db_session, seeded["receiving_bin"]) == "2.000000000000"
    reversed_response = await client.post(
        f"/api/v1/receiving-orders/{order['id']}/reverse-receipt",
        json={"reason": "test reversal"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["receipt_reversal"]["status"] == "REVERSED"
    # FS-04 reversal rebuilds movements from transaction lines (no bin dimension), so the location-level
    # balance is the authoritative check after reversal.
    from app.tests.integration.wms.conftest import balance_on_hand

    assert await balance_on_hand(db_session, seeded["item_id"]) == "0.000000000000"


async def test_putaway_assign_start_exception_cancel_lifecycle(
    client: Any, seeded: dict[str, Any]
) -> None:
    asn = await _create_asn(client, seeded, qty="1")
    order_response = await client.post(
        "/api/v1/receiving-orders",
        json={"inbound_shipment_id": asn["id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    order = order_response.json()
    assert order_response.status_code == 201, order_response.text
    completed = await client.post(
        f"/api/v1/receiving-orders/{order['id']}/complete",
        json={"lines": [{"line_number": 1, "qty_received": "1"}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert completed.status_code == 200, completed.text
    tasks = (await client.get("/api/v1/putaway-tasks")).json()["items"]
    task = tasks[0]
    assigned = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/assign",
        json={"assigned_user_id": seeded["app_user_id"], "cart_id": None},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "ASSIGNED"
    started = await client.post(f"/api/v1/putaway-tasks/{task['id']}/start", headers={"Idempotency-Key": uuid4().hex})
    assert started.status_code == 200 and started.json()["status"] == "IN_PROGRESS"
    exception = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/exception", json={"reason_code": "DAMAGED"}
    )
    assert exception.status_code == 200 and exception.json()["status"] == "EXCEPTION"
    cancelled = await client.post(
        f"/api/v1/putaway-tasks/{task['id']}/cancel", json={"reason": "write off"}
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
