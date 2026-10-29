"""Tenant-admin base-currency setup. Authentication is the only tenant selector."""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import DBAPIError

from app.api.deps import AuthContext, DbDep, PrincipalDep, get_request_auth, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES, Problem
from app.composition.tenant_configuration import tenant_configuration_repository
from app.core.repositories.config import ConfigError
from app.core.repositories.tenant_configuration import CurrencyChangeBlocker
from app.core.security.tenant_context import TENANT_ADMIN_SCOPE

router = APIRouter(
    prefix="/api/v1/admin/tenant-configuration",
    tags=["platform-config"],
    route_class=SCMAPIRoute,
    responses={**PROBLEM_RESPONSES, 428: {"model": Problem}},
    dependencies=[Depends(require_scope(TENANT_ADMIN_SCOPE))],
)


class TenantCurrencyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_currency_code: str = Field(min_length=3, max_length=3, pattern="^[A-Za-z]{3}$")

    @field_validator("base_currency_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.upper()


class TenantConfigurationOut(BaseModel):
    tenant_id: UUID
    base_currency_code: str | None
    version: int
    currency_change_blockers: list[CurrencyChangeBlocker]


def _error(exc: ConfigError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "details": exc.details}
    )


@router.get("", response_model=TenantConfigurationOut)
async def get_tenant_configuration(db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        result = await tenant_configuration_repository(db).get(UUID(principal.tenant_id))
    except ConfigError as exc:
        raise _error(exc) from exc
    response.headers["ETag"] = f'"{result["version"]}"'
    return result


@router.patch("", response_model=TenantConfigurationOut)
async def patch_tenant_configuration(
    body: TenantCurrencyPatch,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    auth: Annotated[AuthContext | None, Depends(get_request_auth)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "PRECONDITION_REQUIRED",
                "message": "Read tenant configuration and supply its ETag in If-Match",
            },
        )
    if not re.fullmatch(r'(?:[1-9][0-9]*|"[1-9][0-9]*")', if_match.strip()):
        raise HTTPException(
            status_code=422,
            detail={"code": "PRECONDITION_INVALID", "message": "If-Match must be a positive version or quoted ETag"},
        )
    try:
        result = await tenant_configuration_repository(db).set_currency(
            UUID(principal.tenant_id),
            body.base_currency_code,
            expected_version=int(if_match.strip().strip('"')),
            actor_id=principal.app_user_id,
            actor_type="SERVICE" if auth is not None and auth.kind == "api_key" else "USER",
        )
    except ConfigError as exc:
        raise _error(exc) from exc
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONFIGURATION_BUSY",
                "message": "Valuation data is being written; retry the configuration change",
            },
        ) from exc
    response.headers["ETag"] = f'"{result["version"]}"'
    return result
