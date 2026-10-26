"""FS-00 platform operations read APIs: audit trail, job catalogue/runs, outbox stats."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import PlatformObject
from app.core.repositories import ops

router = APIRouter(responses=PROBLEM_RESPONSES, tags=["platform-ops"], route_class=SCMAPIRoute)

AuditReader = Annotated[
    object,
    Depends(require_scope("platform.audit.read")),
]
JobManager = Annotated[object, Depends(require_scope("platform.job.manage"))]
InternalService = Annotated[object, Depends(require_scope("platform.internal"))]


@router.get("/api/v1/admin/audit-events", response_model=PlatformObject)
async def list_audit_events(
    session: DbDep,
    principal: AuditReader,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    rows = await ops.list_audit_events(
        session,
        tenant_id=principal.tenant_id,  # type: ignore[attr-defined]
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
    )
    return {"items": rows, "limit": limit}


@router.get("/api/v1/admin/jobs", response_model=PlatformObject)
async def list_jobs(session: DbDep, principal: JobManager) -> dict[str, object]:
    return {"items": await ops.list_jobs(session, tenant_id=principal.tenant_id)}  # type: ignore[attr-defined]


@router.get("/api/v1/admin/jobs/{job_name}/runs", response_model=PlatformObject)
async def list_job_runs(
    job_name: str,
    session: DbDep,
    principal: JobManager,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    rows = await ops.list_job_runs(session, tenant_id=principal.tenant_id, job_name=job_name, limit=limit)  # type: ignore[attr-defined]
    return {"items": rows, "job": job_name, "limit": limit}


@router.get("/api/v1/internal/outbox/stats", response_model=PlatformObject)
async def outbox_stats(session: DbDep, _: InternalService) -> dict[str, object]:
    return await ops.outbox_stats(session)
