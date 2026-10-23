"""FS-07 × FS-00 declarative approvals integration tests (live DB, rollback-isolated)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.models import platform as p
from app.modules.counts import tables as ct
from app.tests.integration.counts.conftest import (
    APPROVER_APP_USER_ID,
    ITEM_A,
    OTHER_TENANT_ID,
    REQUESTER_APP_USER_ID,
    TENANT_ID,
    idem,
)

pytestmark = pytest.mark.integration


async def _create_count(client: Any, *, approval_required: bool = True) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/inventory-counts",
        json={
            "count_type": "MANUAL", "count_date": "2026-10-23", "approval_required": approval_required,
            "scope": {"items": [str(ITEM_A)]},
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _prepare_count(client: Any, *, counted_qty: str = "10", approval_required: bool = True) -> str:
    count = await _create_count(client, approval_required=approval_required)
    started = await client.post(f"/api/v1/inventory-counts/{count['count_id']}/start", headers=idem())
    assert started.status_code == 200, started.text
    lines = (await client.get(f"/api/v1/inventory-counts/{count['count_id']}/lines")).json()["items"]
    captured = await client.post(
        f"/api/v1/inventory-counts/{count['count_id']}/lines",
        json={"lines": [{"line_id": lines[0]["id"], "counted_qty": counted_qty}]}, headers=idem(),
    )
    assert captured.status_code == 200, captured.text
    return count["count_id"]


async def _submit(client: Any, count_id: str, *, key: str | None = None) -> Any:
    headers = {"Idempotency-Key": key} if key else idem()
    return await client.post(f"/api/v1/inventory-counts/{count_id}/submit", headers=headers)


async def _request_row(db_session: Any, request_id: str) -> dict[str, Any]:
    row = (
        await db_session.execute(
            select(
                p.approval_request.c.id, p.approval_request.c.status,
                p.approval_request.c.document_type, p.approval_request.c.document_id,
                p.approval_request.c.tenant_id, p.approval_request.c.requested_by,
            ).where(p.approval_request.c.id == str(request_id))
        )
    ).mappings().first()
    assert row is not None
    return dict(row)


async def test_required_submit_creates_and_links_approval_request(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    submitted = await _submit(client, count_id)
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "PENDING_REVIEW"
    assert body["approval_request_id"] is not None
    assert body["approval_mode"] == "REQUEST"

    request = await _request_row(db_session, body["approval_request_id"])
    assert request["status"] == "PENDING"
    assert request["document_type"] == "INVENTORY_COUNT"
    assert str(request["document_id"]) == count_id
    assert str(request["tenant_id"]) == str(TENANT_ID)
    assert str(request["requested_by"]) == str(REQUESTER_APP_USER_ID)

    link = (
        await db_session.execute(
            select(p.document_link.c.link_type, p.document_link.c.from_type, p.document_link.c.to_type).where(
                p.document_link.c.tenant_id == TENANT_ID, p.document_link.c.to_id == body["approval_request_id"]
            )
        )
    ).mappings().first()
    assert link is not None and link["link_type"] == "COUNT_APPROVAL_REQUEST"

    detail = await client.get(f"/api/v1/inventory-counts/{count_id}")
    assert detail.json()["approval_request_id"] == body["approval_request_id"]


async def test_approve_completes_approval_request(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    request_id = (await _submit(client, count_id)).json()["approval_request_id"]
    approved = await client.post(
        f"/api/v1/inventory-counts/{count_id}/approve",
        json={"approved_by": str(APPROVER_APP_USER_ID), "comment": "counted correctly"}, headers=idem(),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "POSTED"
    request = await _request_row(db_session, request_id)
    assert request["status"] == "APPROVED"
    count = (await db_session.execute(select(ct.inventory_count.c.status).where(ct.inventory_count.c.id == str(count_id)))).scalar_one()
    assert count == "POSTED"


async def test_requester_cannot_approve_own_count_sod(client: Any) -> None:
    count_id = await _prepare_count(client)
    await _submit(client, count_id)
    approved = await client.post(
        f"/api/v1/inventory-counts/{count_id}/approve", json={"comment": "self approval"}, headers=idem()
    )
    assert approved.status_code == 409
    assert approved.json()["code"] == "SOD_CONFLICT"
    count = (await client.get(f"/api/v1/inventory-counts/{count_id}")).json()
    assert count["status"] == "PENDING_REVIEW"


async def test_reject_completes_approval_request(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    request_id = (await _submit(client, count_id)).json()["approval_request_id"]
    rejected = await client.post(
        f"/api/v1/inventory-counts/{count_id}/reject",
        json={"reason": "miscount", "recalc_snapshot": False, "approved_by": str(APPROVER_APP_USER_ID)}, headers=idem(),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    request = await _request_row(db_session, request_id)
    assert request["status"] == "REJECTED"


async def test_auto_approval_path_creates_no_request(client: Any, db_session: Any) -> None:
    await db_session.execute(
        p.metadata.tables["platform.tenant_setting"].insert().values(
            id=uuid4(), tenant_id=TENANT_ID, namespace="inventory", key="counts.auto_approve_within_tolerance",
            value=True, value_type="boolean", is_default=True, version=1,
        )
    )
    await db_session.execute(
        p.metadata.tables["platform.tenant_setting"].insert().values(
            id=uuid4(), tenant_id=TENANT_ID, namespace="inventory", key="counts.tolerance_qty",
            value="1000", value_type="number", is_default=True, version=1,
        )
    )
    await db_session.flush()
    count_id = await _prepare_count(client, counted_qty="10")
    submitted = await _submit(client, count_id)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["approval_request_id"] is None
    assert submitted.json()["approval_mode"] == "AUTO"
    approved = await client.post(f"/api/v1/inventory-counts/{count_id}/approve", headers=idem())
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "POSTED"
    requests = (
        await db_session.execute(select(p.approval_request).where(p.approval_request.c.tenant_id == TENANT_ID))
    ).mappings().all()
    assert requests == []


async def test_submit_idempotent_replay_returns_same_request(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    key = uuid4().hex
    first = await _submit(client, count_id, key=key)
    assert first.status_code == 200, first.text
    replay = await _submit(client, count_id, key=key)
    assert replay.status_code == 200
    assert replay.json()["approval_request_id"] == first.json()["approval_request_id"]


async def test_missing_approval_definition_returns_409(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    await db_session.execute(
        p.approval_definition.update()
        .where(p.approval_definition.c.tenant_id == TENANT_ID, p.approval_definition.c.document_type == "INVENTORY_COUNT")
        .values(is_enabled=False)
    )
    await db_session.flush()
    submitted = await _submit(client, count_id)
    assert submitted.status_code == 409
    assert submitted.json()["code"] == "APPROVAL_DEFINITION_MISSING"


async def test_approval_request_tenant_isolated(client: Any, db_session: Any) -> None:
    count_id = await _prepare_count(client)
    request_id = (await _submit(client, count_id)).json()["approval_request_id"]
    other = (
        await db_session.execute(
            select(p.approval_request.c.id).where(
                p.approval_request.c.id == request_id, p.approval_request.c.tenant_id == OTHER_TENANT_ID
            )
        )
    ).scalar_one_or_none()
    assert other is None
