"""FS-00 approval decision/status API (task-47).

Additive to the existing read endpoints in ``app/api/routers/platform/approvals.py`` (not edited):
POST decisions (APPROVE/REJECT/DELEGATE/RECALL) and GET status/progress. Every route requires the
single catalogued scope ``platform.approval.act`` (deny-by-default); decisions support an optional
``Idempotency-Key`` header with replay safety. Lifecycle semantics live in
:mod:`app.core.approvals` (sequential/parallel steps, requester≠approver SoD, terminal guards).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas.platform import PlatformObject
from app.core.approvals import ApprovalService

router = APIRouter(tags=["platform-approvals"])

ApprovalActor = Annotated[object, Depends(require_scope("platform.approval.act"))]


class DecisionIn(BaseModel):
    decision: Literal["APPROVE", "REJECT", "DELEGATE", "RECALL"]
    comment: str | None = Field(default=None, max_length=2000)
    delegated_to: str | None = Field(default=None, max_length=64, description="app_user id for DELEGATE")


def _service(session: DbDep) -> ApprovalService:
    return ApprovalService(session)


@router.post("/api/v1/approvals/{request_id}/actions", response_model=PlatformObject)
async def decide_approval(
    request_id: str,
    body: DecisionIn,
    session: DbDep,
    principal: PrincipalDep,
    _: ApprovalActor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    service = _service(session)
    return await service.decide(
        principal.tenant_id, request_id, actor_id=principal.app_user_id,
        decision=body.decision, comment=body.comment, delegated_to=body.delegated_to,
        idempotency_key=idempotency_key,
    )


@router.get("/api/v1/approvals/{request_id}/status", response_model=PlatformObject)
async def approval_status(
    request_id: str,
    session: DbDep,
    principal: PrincipalDep,
    _: ApprovalActor,
) -> dict[str, Any]:
    service = _service(session)
    try:
        return await service.status(principal.tenant_id, request_id)
    except HTTPException:
        raise


__all__ = ["router"]
