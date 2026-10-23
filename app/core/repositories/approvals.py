"""Approval definitions and request read/write queries (FS-00)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as models


def _row(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row._mapping).items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif hasattr(value, "hex") and not isinstance(value, str):
            out[key] = str(value)
        else:
            out[key] = value
    return out


async def list_definitions(session: AsyncSession, tenant_id: str, document_type: str | None = None) -> list[dict[str, Any]]:
    table = models.metadata.tables["platform.approval_definition"]
    query = select(table).where(table.c.tenant_id == tenant_id)
    if document_type:
        query = query.where(table.c.document_type == document_type)
    result = await session.execute(query.order_by(desc(table.c.priority), table.c.name))
    return [_row(r) for r in result]


async def create_definition(session: AsyncSession, tenant_id: str, payload: dict[str, Any], created_by: str) -> dict[str, Any]:
    table = models.metadata.tables["platform.approval_definition"]
    stmt = table.insert().values(
        tenant_id=tenant_id,
        document_type=payload["document_type"],
        name=payload["name"],
        condition=payload.get("condition"),
        steps=payload.get("steps", []),
        priority=payload.get("priority", 100),
        is_enabled=payload.get("is_enabled", True),
        created_by=created_by,
        updated_by=created_by,
    ).returning(table)
    result = await session.execute(stmt)
    return _row(result.one())


async def list_requests(
    session: AsyncSession,
    tenant_id: str,
    *,
    status: str | None = None,
    document_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    table = models.metadata.tables["platform.approval_request"]
    query = select(table).where(table.c.tenant_id == tenant_id)
    if status:
        query = query.where(table.c.status == status)
    if document_type:
        query = query.where(table.c.document_type == document_type)
    result = await session.execute(query.order_by(desc(table.c.requested_at)).limit(limit))
    return [_row(r) for r in result]


async def get_request(session: AsyncSession, tenant_id: str, request_id: str) -> dict[str, Any] | None:
    requests = models.metadata.tables["platform.approval_request"]
    actions = models.metadata.tables["platform.approval_action"]
    result = await session.execute(select(requests).where(requests.c.tenant_id == tenant_id, requests.c.id == request_id))
    row = result.one_or_none()
    if row is None:
        return None
    payload: dict[str, Any] = {"request": _row(row), "steps": [], "actions": []}
    action_rows = await session.execute(select(actions).where(actions.c.tenant_id == tenant_id, actions.c.approval_request_id == request_id).order_by(actions.c.decided_at))
    payload["actions"] = [_row(r) for r in action_rows]
    return payload
