"""Read-only platform operations queries (audit trail, jobs, outbox) for FS-00 admin/internal APIs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as models


def _iso(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "hex") and not isinstance(value, str):
        return str(value)
    return value


def _row(row: Any) -> dict[str, Any]:
    return {key: _iso(value) for key, value in dict(row._mapping).items()}


async def list_audit_events(
    session: AsyncSession,
    *,
    tenant_id: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    table = models.metadata.tables["platform.audit_event"]
    query: Select = select(table).where(table.c.tenant_id == tenant_id)
    if resource_type:
        query = query.where(table.c.resource_type == resource_type)
    if resource_id:
        query = query.where(table.c.resource_id == resource_id)
    query = query.order_by(desc(table.c.occurred_at)).limit(limit)
    result = await session.execute(query)
    return [_row(row) for row in result]


async def list_jobs(session: AsyncSession, *, tenant_id: str) -> list[dict[str, Any]]:
    table = models.metadata.tables["platform.job_definition"]
    result = await session.execute(select(table).where(table.c.tenant_id == tenant_id).order_by(table.c.name))
    return [_row(row) for row in result]


async def list_job_runs(session: AsyncSession, *, tenant_id: str, job_name: str, limit: int = 50) -> list[dict[str, Any]]:
    jobs = models.metadata.tables["platform.job_definition"]
    runs = models.metadata.tables["platform.job_run"]
    query = (
        select(runs)
        .join(jobs, jobs.c.id == runs.c.job_definition_id)
        .where(runs.c.tenant_id == tenant_id, jobs.c.name == job_name)
        .order_by(desc(runs.c.created_at))
        .limit(limit)
    )
    result = await session.execute(query)
    return [_row(row) for row in result]


async def outbox_stats(session: AsyncSession) -> dict[str, Any]:
    table = models.metadata.tables["platform.outbox_event"]
    result = await session.execute(
        select(func.count().label("total"), func.count(table.c.published_at).label("published"))
    )
    total, published = result.one()
    return {"total": int(total or 0), "published": int(published or 0), "pending": int((total or 0) - (published or 0))}
