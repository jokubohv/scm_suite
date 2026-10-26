"""FS-00 approval definitions/requests read APIs (decisions are enforced by the approval service)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import PlatformObject
from app.core.repositories import approvals

router = APIRouter(responses=PROBLEM_RESPONSES, tags=["platform-approvals"], route_class=SCMAPIRoute)

DefinitionManager = Annotated[object, Depends(require_scope("platform.approval.definition.manage"))]
ApprovalActor = Annotated[object, Depends(require_scope("platform.approval.act"))]


class DefinitionCreate(BaseModel):
    document_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    condition: dict[str, object] | None = None
    steps: list[dict[str, object]] = Field(default_factory=list)
    priority: int = 100
    is_enabled: bool = True


@router.get("/api/v1/admin/approval-definitions", response_model=PlatformObject)
async def list_definitions(session: DbDep, principal: PrincipalDep, _: DefinitionManager, document_type: str | None = None) -> dict[str, object]:
    return {"items": await approvals.list_definitions(session, principal.tenant_id, document_type)}


@router.post("/api/v1/admin/approval-definitions", response_model=PlatformObject, status_code=201)
async def create_definition(payload: DefinitionCreate, session: DbDep, principal: PrincipalDep, _: DefinitionManager) -> dict[str, object]:
    created = await approvals.create_definition(session, principal.tenant_id, payload.model_dump(), principal.app_user_id)
    return {"definition": created}


@router.get("/api/v1/approvals", response_model=PlatformObject)
async def list_requests(
    session: DbDep,
    principal: PrincipalDep,
    _: ApprovalActor,
    status: str | None = None,
    document_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    return {"items": await approvals.list_requests(session, principal.tenant_id, status=status, document_type=document_type, limit=limit)}


@router.get("/api/v1/approvals/{request_id}", response_model=PlatformObject)
async def get_request(request_id: str, session: DbDep, principal: PrincipalDep, _: ApprovalActor) -> dict[str, object]:
    result = await approvals.get_request(session, principal.tenant_id, request_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "approval request not found"})
    return result
