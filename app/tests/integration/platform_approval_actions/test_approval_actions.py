"""Approval lifecycle/decision endpoint integration tests (task-47)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.core.approvals import ApprovalService
from app.core.models import platform as p
from app.core.security.errors import SodConflict
from app.tests.integration.platform_approval_actions.conftest import (
    APPROVER1_ID,
    APPROVER2_ID,
    OUTSIDER_ID,
    REQUESTER_ID,
    TENANT_ID,
    as_actor,
)

DOCUMENT_ID = "99999999-9999-9999-9999-999999999999"
SEQUENTIAL_STEPS = [
    {"step": 1, "name": "Manager", "roles": ["approver"]},
    {"step": 2, "name": "Controller", "roles": ["approver"]},
]
PARALLEL_STEPS = [
    {"step": 1, "name": "Dual control", "mode": "PARALLEL", "user_ids": [str(APPROVER1_ID), str(APPROVER2_ID)]},
]


async def _submit(session: Any, document_id: str = DOCUMENT_ID) -> dict[str, Any]:
    return await ApprovalService(session).submit(
        str(TENANT_ID), document_type="PURCHASE_ORDER", document_id=document_id,
        requested_by=str(REQUESTER_ID), amount_base="1500.00", context={"amount_base": 1500},
    )


async def test_submit_creates_pending_request_with_outbox_and_audit(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    result = await _submit(db_session)
    request = result["request"]
    assert request["status"] == "PENDING" and request["current_step"] == 1
    assert result["reused"] is False
    outbox = await db_session.execute(
        p.outbox_event.select().where(p.outbox_event.c.event_type == "platform.approval.requested")
    )
    events = outbox.mappings().all()
    assert len(events) == 1 and events[0]["payload"]["data"]["request_id"] == request["id"]
    audits = await db_session.execute(p.audit_event.select().where(p.audit_event.c.action == "approval.submitted"))
    assert len(list(audits.mappings().all())) == 1


async def test_no_definition_auto_approves(db_session, seeded) -> None:
    result = await _submit(db_session)
    assert result["auto_approved"] is True
    assert result["request"]["status"] == "APPROVED"
    outbox = await db_session.execute(p.outbox_event.select().where(p.outbox_event.c.event_type == "platform.approval.approved"))
    assert len(list(outbox.mappings().all())) == 1


async def test_sequential_approve_advances_then_approves(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    service = ApprovalService(db_session)
    first = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER1_ID), decision="APPROVE")
    assert first["request"]["status"] == "PENDING" and first["request"]["current_step"] == 2
    second = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER1_ID), decision="APPROVE")
    assert second["request"]["status"] == "APPROVED"
    events = await db_session.execute(p.outbox_event.select().where(p.outbox_event.c.event_type == "platform.approval.approved"))
    assert len(list(events.mappings().all())) == 1
    actions = await db_session.execute(p.approval_action.select().where(p.approval_action.c.approval_request_id == request["id"]))
    assert len(list(actions.mappings().all())) == 2


async def test_parallel_requires_all_approvers(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=PARALLEL_STEPS)
    request = (await _submit(db_session))["request"]
    service = ApprovalService(db_session)
    first = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER1_ID), decision="APPROVE")
    assert first["request"]["status"] == "PENDING"
    second = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER2_ID), decision="APPROVE")
    assert second["request"]["status"] == "APPROVED"


async def test_reject_terminal_and_terminal_guard(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    service = ApprovalService(db_session)
    rejected = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER1_ID), decision="REJECT", comment="no")
    assert rejected["request"]["status"] == "REJECTED"
    with pytest.raises(HTTPException) as exc:
        await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER2_ID), decision="APPROVE")
    assert exc.value.status_code == 409
    events = await db_session.execute(p.outbox_event.select().where(p.outbox_event.c.event_type == "platform.approval.rejected"))
    assert len(list(events.mappings().all())) == 1


async def test_requester_cannot_approve_own_request(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    with pytest.raises(SodConflict):
        await ApprovalService(db_session).decide(str(TENANT_ID), request["id"], actor_id=str(REQUESTER_ID), decision="APPROVE")


async def test_unauthorized_step_actor_rejected(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    with pytest.raises(HTTPException) as exc:
        await ApprovalService(db_session).decide(str(TENANT_ID), request["id"], actor_id=str(OUTSIDER_ID), decision="APPROVE")
    assert exc.value.status_code == 403


async def test_delegate_then_delegatee_approves(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    service = ApprovalService(db_session)
    delegated = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER1_ID), decision="DELEGATE", delegated_to=str(APPROVER2_ID))
    assert delegated["action"]["decision"] == "DELEGATE"
    approved = await service.decide(str(TENANT_ID), request["id"], actor_id=str(APPROVER2_ID), decision="APPROVE")
    assert approved["request"]["current_step"] == 2


async def test_recall_by_requester(db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    service = ApprovalService(db_session)
    recalled = await service.decide(str(TENANT_ID), request["id"], actor_id=str(REQUESTER_ID), decision="RECALL", comment="dup")
    assert recalled["request"]["status"] == "CANCELLED"


async def test_http_decide_and_status_with_idempotency(db_session, seeded, definition_factory, app_client) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    as_actor(app_client, APPROVER1_ID)
    payload = {"decision": "APPROVE", "comment": "ok"}
    first = await app_client.post(f"/api/v1/approvals/{request['id']}/actions", json=payload, headers={"Idempotency-Key": "decide-1"})
    assert first.status_code == 200, first.text
    replay = await app_client.post(f"/api/v1/approvals/{request['id']}/actions", json=payload, headers={"Idempotency-Key": "decide-1"})
    assert replay.status_code == 200 and replay.json() == first.json()
    count = await db_session.execute(p.approval_action.select().where(p.approval_action.c.approval_request_id == request["id"]))
    assert len(list(count.mappings().all())) == 1
    status = await app_client.get(f"/api/v1/approvals/{request['id']}/status")
    assert status.status_code == 200
    body = status.json()
    assert body["request"]["status"] == "PENDING" and body["current_step"] == 2
    assert len(body["steps"]) == 2


async def test_http_requester_approval_returns_409(app_client, db_session, seeded, definition_factory) -> None:
    await definition_factory(steps=SEQUENTIAL_STEPS)
    request = (await _submit(db_session))["request"]
    as_actor(app_client, REQUESTER_ID)
    response = await app_client.post(f"/api/v1/approvals/{request['id']}/actions", json={"decision": "APPROVE"})
    assert response.status_code == 409, response.text
