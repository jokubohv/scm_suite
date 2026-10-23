"""FS-07 ↔ FS-00 declarative approvals bridge for inventory counts.

No schema change: the count→approval-request link is stored in ``platform.document_link``
(``link_type=COUNT_APPROVAL_REQUEST``, ``from=INVENTORY_COUNT``, ``to=APPROVAL_REQUEST``). Requests are
created/completed through the existing FS-00 :class:`app.core.approvals.ApprovalService` in the caller's
transaction; count approve/reject only proceed once the linked request is terminal-approved.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import approvals as fs00
from app.core.models import platform as p
from app.modules.counts.engine import CountSettings, ToleranceDecision
from app.modules.counts.errors import CountsConflict, CountsNotFound

DOCUMENT_TYPE = "INVENTORY_COUNT"
LINK_TYPE = "COUNT_APPROVAL_REQUEST"


def resolve_approval_mode(
    *, settings: CountSettings, tolerance: ToleranceDecision, approval_required: bool
) -> str:
    """AUTO (tolerance) | REQUEST (declarative approval) | DIRECT (authorized direct flow)."""
    if settings.auto_approve_within_tolerance and tolerance.eligible:
        return "AUTO"
    if approval_required:
        return "REQUEST"
    return "DIRECT"


def _definition_matches(
    definitions: list[dict[str, Any]], context: dict[str, Any]
) -> bool:
    for definition in definitions:
        condition = definition.get("condition")
        if fs00.match_condition(condition, context):
            return True
    return False


async def find_linked_request(session: AsyncSession, tenant_id: UUID | str, count_id: str) -> str | None:
    table = p.metadata.tables["platform.document_link"]
    result = await session.execute(
        select(table.c.to_id).where(
            table.c.tenant_id == tenant_id,
            table.c.link_type == LINK_TYPE,
            table.c.from_type == DOCUMENT_TYPE,
            table.c.from_id == count_id,
            table.c.to_type == "APPROVAL_REQUEST",
        )
    )
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def link_request(session: AsyncSession, tenant_id: UUID | str, count_id: str, request_id: str) -> None:
    if await find_linked_request(session, tenant_id, count_id) == str(request_id):
        return
    table = p.metadata.tables["platform.document_link"]
    await session.execute(
        table.insert().values(
            id=uuid4(), tenant_id=tenant_id, link_type=LINK_TYPE,
            from_type=DOCUMENT_TYPE, from_id=count_id,
            to_type="APPROVAL_REQUEST", to_id=request_id, version=1,
        )
    )


async def submit_count_request(
    session: AsyncSession,
    tenant_id: UUID | str,
    *,
    count: dict[str, Any],
    requested_by: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create (or reuse) the FS-00 approval request for a count; requires a matching definition."""
    context = {"document_type": DOCUMENT_TYPE, "count_type": count.get("count_type"), **(context or {})}
    definitions_table = p.metadata.tables["platform.approval_definition"]
    result = await session.execute(
        select(definitions_table)
        .where(
            definitions_table.c.tenant_id == tenant_id,
            definitions_table.c.document_type == DOCUMENT_TYPE,
            definitions_table.c.is_enabled.is_(True),
        )
        .order_by(definitions_table.c.priority.desc(), definitions_table.c.created_at)
    )
    rows = [dict(row._mapping) for row in result]
    if not rows:
        raise CountsConflict(
            "no enabled declarative approval definition exists for inventory counts",
            code="APPROVAL_DEFINITION_MISSING",
            details={"document_type": DOCUMENT_TYPE},
        )
    if not _definition_matches(rows, context):
        raise CountsConflict(
            "no declarative approval definition matches this count",
            code="APPROVAL_DEFINITION_MISSING",
            details={"document_type": DOCUMENT_TYPE, "context": context},
        )
    service = fs00.ApprovalService(session)
    try:
        outcome = await service.submit(
            str(tenant_id), document_type=DOCUMENT_TYPE, document_id=str(count["id"]),
            requested_by=requested_by, amount_base=count.get("amount_base"), context=context,
        )
    except Exception as exc:  # noqa: BLE001 - FS-00 raises HTTPExceptions with codes
        raise _map_approval_error(exc) from exc
    request = outcome["request"]
    if bool(outcome.get("auto_approved")):
        # A definition was required for declarative approval: auto-approval here would be silent.
        raise CountsConflict(
            "count approval definition resolved to auto-approval; configure an enabled definition",
            code="APPROVAL_DEFINITION_MISSING",
            details={"request_id": str(request.get("id"))},
        )
    await link_request(session, tenant_id, str(count["id"]), str(request["id"]))
    return {
        "approval_request_id": str(request["id"]),
        "status": request.get("status"),
        "reused": bool(outcome.get("reused")),
        "definition_id": str(request.get("definition_id")) if request.get("definition_id") else None,
    }


async def complete_count_request(
    session: AsyncSession,
    tenant_id: UUID | str,
    *,
    count_id: str,
    actor_id: str,
    decision: str,
    comment: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply an APPROVE/REJECT decision to the linked FS-00 request in this transaction."""
    request_id = await find_linked_request(session, tenant_id, count_id)
    if request_id is None:
        raise CountsNotFound("count has no linked approval request", code="APPROVAL_REQUEST_MISSING")
    service = fs00.ApprovalService(session)
    try:
        outcome = await service.decide(
            str(tenant_id), request_id, actor_id=actor_id, decision=decision,
            comment=comment, idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_approval_error(exc) from exc
    request = outcome.get("request") or outcome
    status = request.get("status") if isinstance(request, dict) else None
    return {"approval_request_id": request_id, "status": status, "approval_ok": status == "APPROVED"}


def _map_approval_error(exc: Exception) -> CountsConflict:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return CountsConflict(
            str(detail.get("message", "approval operation failed")),
            code=str(detail.get("code", "APPROVAL_ERROR")),
            details=dict(detail.get("details") or {}),
            status_code=int(getattr(exc, "status_code", 409) or 409),
        )
    return CountsConflict(str(exc) or "approval operation failed", code=getattr(exc, "code", "APPROVAL_ERROR"), status_code=409)


__all__ = [
    "DOCUMENT_TYPE",
    "LINK_TYPE",
    "complete_count_request",
    "find_linked_request",
    "link_request",
    "resolve_approval_mode",
    "submit_count_request",
]
