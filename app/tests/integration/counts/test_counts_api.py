"""FS-07 counts API integration tests (live Postgres; rollback-isolated test tenancy)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.core.models import platform as p
from app.modules.counts import tables as ct
from app.tests.integration.counts.conftest import (
    ITEM_A,
    LOCATION_ID,
    OTHER_TENANT_ID,
    STATUS_ID,
    TENANT_ID,
    idem,
    seed_balance,
)

pytestmark = pytest.mark.integration


async def _create_plan(client: Any, **overrides: Any) -> dict[str, Any]:
    body = {
        "code": "CP1", "name": "Cycle plan", "frequency": "WEEKLY",
        "next_run_date": "2026-10-24", "blind_count": False, "approval_required": True,
        "include_zero_qty": False,
        "filters": [{"filter_type": "LOCATION", "filter_operator": "INCLUDE", "value_uuid": str(LOCATION_ID)}],
    }
    body.update(overrides)
    response = await client.post("/api/v1/count-plans", json=body, headers=idem())
    assert response.status_code == 201, response.text
    return response.json()


async def _create_manual_count(client: Any, *, item_ids: list[str] | None = None, blind: bool = False) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/inventory-counts",
        json={
            "count_type": "MANUAL", "count_date": "2026-10-23", "blind_count": blind,
            "approval_required": False, "scope": {"items": item_ids or [str(ITEM_A)]},
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _start_count(client: Any, count_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/v1/inventory-counts/{count_id}/start", headers=idem())
    assert response.status_code == 200, response.text
    return response.json()


async def _capture(client: Any, count_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/inventory-counts/{count_id}/lines", json={"client_batch_id": uuid4().hex, "lines": lines}, headers=idem()
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_calculated_count_requires_a_real_scope(client: Any) -> None:
    """P1 runtime probe regression: empty/default scope must not create an all-positions count."""
    for scope in (None, {}):
        body: dict[str, Any] = {"count_type": "CALCULATED", "count_date": "2026-10-23"}
        if scope is not None:
            body["scope"] = scope
        response = await client.post("/api/v1/inventory-counts", json=body, headers=idem())
        assert response.status_code == 422, (scope, response.status_code, response.text)
        assert response.json()["code"] == "VALIDATION_FAILED"

    scoped = await client.post(
        "/api/v1/inventory-counts",
        json={
            "count_type": "CALCULATED",
            "count_date": "2026-10-23",
            "scope": {"items": [str(ITEM_A)]},
        },
        headers=idem(),
    )
    assert scoped.status_code == 201, scoped.text


async def test_multiple_unposted_counts_are_allowed_per_tenant(client: Any) -> None:
    """P1 runtime probe regression: NULLS NOT DISTINCT allowed only one unposted count per tenant."""
    first = await _create_manual_count(client)
    second = await _create_manual_count(client, blind=True)
    assert first["count_id"] != second["count_id"]
    counts = (await client.get("/api/v1/inventory-counts", params={"limit": 50})).json()
    numbers = {row["id"] for row in counts["items"]}
    assert {first["count_id"], second["count_id"]} <= numbers


async def test_plan_crud_idempotency_and_generation(client: Any) -> None:
    plan = await _create_plan(client)
    assert plan["status"] == "ACTIVE"
    body = {
        "code": "IDEM", "name": "Idem", "frequency": "MONTHLY", "next_run_date": "2026-10-25",
        "filters": [],
    }
    first = await client.post("/api/v1/count-plans", json=body, headers={"Idempotency-Key": "plan-key"})
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/count-plans", json=body, headers={"Idempotency-Key": "plan-key"})
    assert replay.status_code in {200, 201}
    assert replay.json()["id"] == first.json()["id"]

    listing = await client.get("/api/v1/count-plans")
    assert listing.status_code == 200
    assert {row["code"] for row in listing.json()["items"]} >= {"CP1", "IDEM"}

    updated = await client.patch(f"/api/v1/count-plans/{plan['id']}", json={"name": "Renamed"}, headers=idem())
    assert updated.status_code == 200 and updated.json()["name"] == "Renamed"
    paused = await client.post(f"/api/v1/count-plans/{plan['id']}/pause", json={"reason": "test"}, headers=idem())
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    resumed = await client.post(f"/api/v1/count-plans/{plan['id']}/resume", headers=idem())
    assert resumed.status_code == 200 and resumed.json()["status"] == "ACTIVE"

    generated = await client.post(
        f"/api/v1/count-plans/{plan['id']}/generate", json={"count_date": "2026-10-23", "max_lines": 10}, headers=idem()
    )
    assert generated.status_code == 201, generated.text
    assert generated.json()["status"] == "DRAFT"
    counts = await client.get("/api/v1/inventory-counts", params={"count_type": "CYCLE"})
    assert counts.status_code == 200 and counts.json()["count"] >= 1


async def test_manual_count_lifecycle_posts_t20(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    count = await _create_manual_count(client)
    started = await _start_count(client, count["count_id"])
    assert started["line_count"] == 1
    lines = await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")
    assert lines.status_code == 200 and lines.json()["count"] == 1
    line = lines.json()["items"][0]
    assert Decimal(line["snapshot_qty"]) == Decimal("10")
    captured = await _capture(client, count["count_id"], [{"line_id": line["id"], "counted_qty": "7"}])
    assert captured["accepted"][0]["count_status"] == "COUNTED"

    submitted = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/submit", headers=idem())
    assert submitted.status_code == 200 and submitted.json()["status"] == "PENDING_REVIEW"
    approved = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/approve", headers=idem())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "POSTED"
    assert approved.json()["posted_transaction_id"] is not None

    detail = await client.get(f"/api/v1/inventory-counts/{count['count_id']}")
    assert detail.json()["status"] == "POSTED"
    balance = await db_session.execute(
        __import__("sqlalchemy").text(
            "SELECT qty_on_hand FROM inventory_ledger.inventory_balance WHERE tenant_id=:t AND item_id=:i"
        ),
        {"t": TENANT_ID, "i": ITEM_A},
    )
    assert Decimal(str(balance.scalar_one())) == Decimal("7")
    events = (await db_session.execute(select(p.outbox_event.c.event_type).where(p.outbox_event.c.tenant_id == TENANT_ID))).scalars().all()
    assert "count.generated" in events and "count.variance_posted" in events

    second = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/post", headers=idem())
    assert second.status_code == 409
    assert second.json()["code"] == "DOCUMENT_ALREADY_POSTED"


async def test_blind_count_hides_snapshot_until_review(client: Any) -> None:
    count = await _create_manual_count(client, blind=True)
    await _start_count(client, count["count_id"])
    lines = await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")
    assert lines.status_code == 200
    assert lines.json()["blind"] is True
    line = lines.json()["items"][0]
    assert "snapshot_qty" not in line and "variance_qty" not in line
    await _capture(client, count["count_id"], [{"line_id": line["id"], "counted_qty": "8"}])
    submitted = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/submit", headers=idem())
    assert submitted.status_code == 200
    after_review = await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")
    assert after_review.json()["blind"] is False
    assert "snapshot_qty" in after_review.json()["items"][0]


async def test_reject_with_snapshot_recalculation_keeps_history(client: Any) -> None:
    count = await _create_manual_count(client)
    await _start_count(client, count["count_id"])
    lines = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    await _capture(client, count["count_id"], [{"line_id": lines[0]["id"], "counted_qty": "9"}])
    await client.post(f"/api/v1/inventory-counts/{count['count_id']}/submit", headers=idem())
    rejected = await client.post(
        f"/api/v1/inventory-counts/{count['count_id']}/reject",
        json={"reason": "recount required", "recalc_snapshot": True}, headers=idem(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["snapshot_revision"] is not None
    snapshot = await client.get(f"/api/v1/inventory-counts/{count['count_id']}/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["count"] == 2  # original + recalculation revision retained (DM-CNT-01)
    assert snapshot.json()["revision_count"] == 2


async def test_recount_appends_attempts(client: Any) -> None:
    count = await _create_manual_count(client)
    await _start_count(client, count["count_id"])
    lines = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    await _capture(client, count["count_id"], [{"line_id": lines[0]["id"], "counted_qty": "6"}])
    recounted = await client.post(
        f"/api/v1/inventory-counts/{count['count_id']}/recount",
        json={"lines": [{"line_id": lines[0]["id"], "counted_qty": "8"}]}, headers=idem(),
    )
    assert recounted.status_code == 200, recounted.text
    assert recounted.json()["recounts"][0]["attempt"] == 1
    lines_after = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    assert Decimal(lines_after[0]["counted_qty"]) == Decimal("8")


async def test_variance_preview_and_next_count(client: Any) -> None:
    count = await _create_manual_count(client)
    await _start_count(client, count["count_id"])
    lines = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    await _capture(client, count["count_id"], [{"line_id": lines[0]["id"], "counted_qty": "7"}])
    variance = await client.get(f"/api/v1/inventory-counts/{count['count_id']}/variance")
    assert variance.status_code == 200
    variance_body = variance.json()
    row = variance_body["items"][0]
    assert Decimal(row["variance_qty"]) == Decimal("-3")
    assert variance_body["blind"] is False
    assert {"variance_qty_total", "variance_qty_abs", "variance_value_base", "counted_lines"} <= set(variance_body["totals"])
    nxt = await client.get(f"/api/v1/items/{ITEM_A}/next-count")
    assert nxt.status_code == 200
    assert nxt.json()["interval_days"] >= 1 and nxt.json()["next_count_date"]


async def test_snapshot_rows_are_immutable(client: Any, db_session: Any) -> None:
    count = await _create_manual_count(client)
    await _start_count(client, count["count_id"])
    snapshot = (await db_session.execute(select(ct.inventory_count_snapshot).where(ct.inventory_count_snapshot.c.tenant_id == TENANT_ID))).mappings().first()
    assert snapshot is not None
    with pytest.raises(DBAPIError):
        await db_session.execute(ct.inventory_count_snapshot.update().where(ct.inventory_count_snapshot.c.id == snapshot["id"]).values(snapshot_qty=Decimal("99")))
        await db_session.flush()
    await db_session.rollback()


async def test_consigned_position_is_prohibited_from_t20(client: Any, db_session: Any) -> None:
    await seed_balance(
        db_session, tenant_id=TENANT_ID, item_id=ITEM_A, location_id=LOCATION_ID, status_id=STATUS_ID,
        qty="2", value="8", ownership="CONSIGNED",
    )
    await db_session.flush()
    count = await _create_manual_count(client)
    await _start_count(client, count["count_id"])
    lines = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    payload = [{"line_id": line["id"], "counted_qty": "1"} for line in lines]
    await _capture(client, count["count_id"], payload)
    submitted = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/submit", headers=idem())
    assert submitted.status_code == 200
    approved = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/approve", headers=idem())
    assert approved.status_code == 409, approved.text
    assert approved.json()["code"] == "PROHIBITED_COUNT_TYPE"


async def test_disabled_flag_blocks_writes_but_reads_work(client: Any, db_session: Any) -> None:
    await db_session.execute(
        p.feature_flag.update().where(
            p.feature_flag.c.tenant_id == TENANT_ID, p.feature_flag.c.flag_key == "inventory.counts.enabled"
        ).values(is_enabled=False)
    )
    await db_session.flush()
    response = await client.post(
        "/api/v1/inventory-counts",
        json={"count_type": "MANUAL", "scope": {"items": [str(ITEM_A)]}}, headers=idem(),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DOMAIN_DISABLED"
    listing = await client.get("/api/v1/inventory-counts")
    assert listing.status_code == 200


async def test_tenant_scoping(client: Any, db_session: Any) -> None:
    await _create_plan(client)
    plans = await db_session.execute(select(ct.count_plan.c.tenant_id).where(ct.count_plan.c.code == "CP1"))
    assert {str(row[0]) for row in plans.all()} == {str(TENANT_ID)}
    other = await db_session.execute(select(ct.count_plan).where(ct.count_plan.c.tenant_id == OTHER_TENANT_ID))
    assert other.first() is None
