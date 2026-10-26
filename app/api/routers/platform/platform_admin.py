"""Platform-admin tenant API (B1 / FS-00 platform-admin window).

Implements the platform-admin tenant surface declared in ``feature-sets/FS-00.md`` §6 and
``design/ARCHITECTURE.md`` §5.6 (platform admin & tenant provisioning):

* ``GET  /api/v1/platform/tenants`` — list tenants (status/code filters, cursor pagination);
* ``POST /api/v1/platform/tenants`` — provision a tenant row (unique code; platform-only);
* ``GET  /api/v1/platform/tenants/{tenant_id}`` — detail (settings summary + membership count);
* ``PATCH /api/v1/platform/tenants/{tenant_id}`` — profile/policy update and guarded status
  transitions (``If-Match`` optional optimistic check; no hard delete);
* ``POST /api/v1/platform/tenants/{tenant_id}/suspend|reactivate`` — explicit lifecycle commands
  (audit + session revocation on suspend);
* ``GET  /api/v1/platform/tenants/{tenant_id}/memberships`` — platform-admin membership projection.

Authorization (RULE-PLAT-16): the caller must hold the global ``platform.tenant.manage`` scope **and** be a
global platform administrator in the ``SYSTEM_TENANT_ID`` context. Every write is audited with
``SYSTEM_TENANT_ID`` as the actor tenant. Writes are idempotent via ``Idempotency-Key`` (durable rows in
``platform.idempotency_key``).

The integrator registers this module's ``router`` (``app/api/registry.py``); this module never edits
main/registry/deps or any other router.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbDep, Principal, PrincipalDep, require_account_creation_authority, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import PlatformObject
from app.core.repositories.platform_admin import (
    DEFAULT_LIST_LIMIT,
    PlatformAdminError,
    PlatformAdminRepository,
    ReservedTenantError,
    TenantNotFoundError,
    payload_hash,
    settings_summary,
    tenant_view,
    validate_create,
    validate_idempotency_key,
    validate_patch,
    validate_reason,
)
from app.core.security.errors import PlatformAdminRequired
from app.core.security.tenant_context import PLATFORM_TENANT_ADMIN_SCOPE, SYSTEM_TENANT_ID

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["platform-admin"], route_class=SCMAPIRoute)

_ENDPOINT_CREATE = "platform-admin:tenants:create"
_ENDPOINT_SUSPEND = "platform-admin:tenants:suspend"
_ENDPOINT_REACTIVATE = "platform-admin:tenants:reactivate"


# --------------------------------------------------------------------------- dependencies ------
def _repo(db: DbDep) -> PlatformAdminRepository:
    return PlatformAdminRepository(db)


RepoDep = Annotated[PlatformAdminRepository, Depends(_repo)]


async def _platform_admin_principal(principal: PrincipalDep, repo: RepoDep) -> Principal:
    """Platform-admin context guard: SYSTEM tenant claim + global ``is_platform_admin`` flag."""
    if str(principal.tenant_id) != str(SYSTEM_TENANT_ID):
        raise PlatformAdminRequired("platform-admin operations must run in the system tenant context")
    if not await repo.is_platform_admin(principal.identity_id):
        raise PlatformAdminRequired("a global platform administrator is required")
    return principal


PlatformAdminDep = Annotated[Principal, Depends(_platform_admin_principal)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


def _raise(exc: PlatformAdminError) -> NoReturn:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value.strip().strip('"')
    try:
        return int(candidate)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "If-Match must be a tenant version", "details": []},
        ) from None


# --------------------------------------------------------------------------- schemas ----------
class TenantCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=40, description="Tenant code/slug (normalised upper-case)")
    name: str = Field(min_length=1, max_length=200)
    status: Literal["ACTIVE", "SUSPENDED"] = "ACTIVE"
    timezone: str = "UTC"
    base_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    negative_inventory_policy: Literal["BLOCK", "WARN", "ALLOW"] = "BLOCK"
    rounding_mode: Literal["PER_MOVEMENT", "PER_TRANSACTION", "PER_DOCUMENT"] = "PER_MOVEMENT"
    retention_ledger_years: int = Field(default=7, ge=1, le=50)
    settings: dict[str, Any] = Field(default_factory=dict)
    bootstrap_admin_email: str | None = None


class TenantPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    base_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    negative_inventory_policy: Literal["BLOCK", "WARN", "ALLOW"] | None = None
    rounding_mode: Literal["PER_MOVEMENT", "PER_TRANSACTION", "PER_DOCUMENT"] | None = None
    retention_ledger_years: int | None = Field(default=None, ge=1, le=50)
    settings: dict[str, Any] | None = None
    status: Literal["ACTIVE", "SUSPENDED", "OFFBOARDING"] | None = None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


# --------------------------------------------------------------------------- routes -----------
@router.get("/platform/tenants", response_model=PlatformObject, dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))])
async def list_tenants(
    repo: RepoDep,
    _: PlatformAdminDep,
    status: str | None = Query(default=None, description="ACTIVE|SUSPENDED|OFFBOARDING"),
    code: str | None = Query(default=None, description="Exact tenant code (case-insensitive)"),
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=200),
) -> dict[str, Any]:
    try:
        items, next_cursor = await repo.list_tenants(status=status, code=code, cursor=cursor, limit=limit)
    except PlatformAdminError as exc:
        _raise(exc)
    return {"items": items, "next_cursor": next_cursor, "limit": limit}


@router.post("/platform/tenants", response_model=PlatformObject, status_code=201, dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE)), Depends(require_account_creation_authority)])
async def create_tenant(
    body: TenantCreateIn,
    repo: RepoDep,
    principal: PlatformAdminDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True, exclude_unset=True)
    try:
        data = validate_create(payload)
        validate_idempotency_key(idempotency_key)
        replay = await repo.begin_idempotent(
            endpoint_id=_ENDPOINT_CREATE,
            key=idempotency_key,
            request_hash=payload_hash({"endpoint": _ENDPOINT_CREATE, "body": data}),
            actor_identity_id=principal.identity_id,
        )
        if replay is not None:
            response.status_code = int(replay["response_status"])
            return dict(replay["response_body"])
        row = await repo.create_tenant(data=data, actor_identity_id=principal.identity_id)
        await repo.audit(
            actor_id=principal.identity_id,
            action="tenant.provisioned",
            resource_type="tenant",
            resource_id=row["id"],
            diff={
                "code": row["code"],
                "name": row["name"],
                "status": row["status"],
                "base_currency_code": row["base_currency_code"],
                "timezone": row["timezone"],
                "bootstrap_admin_email": data.get("bootstrap_admin_email"),
            },
        )
        result: dict[str, Any] = {
            "tenant_id": str(row["id"]),
            "code": row["code"],
            "status": row["status"],
            "tenant": tenant_view(row),
            "provisioning": {
                "tenant_row": "APPLIED",
                "negative_inventory_policy": row["negative_inventory_policy"],
                "rounding_mode": row["rounding_mode"],
                "retention_ledger_years": row["retention_ledger_years"],
                "permission_catalogue": "DEFERRED_OWNED_BY_RBAC",
                "role_templates": "DEFERRED_OWNED_BY_RBAC",
                "number_sequences": "DEFERRED_OWNED_BY_FS00_CONFIG",
                "bootstrap_admin_invitation": (
                    "DEFERRED_OWNED_BY_AUTH" if data.get("bootstrap_admin_email") else "NOT_REQUESTED"
                ),
            },
            "warnings": [
                "cross-context provisioning (permission catalogue, role templates, number sequences, "
                "bootstrap invitation) is owned by the RBAC/auth/config services and is not written by "
                "the platform-admin tenant repository",
            ],
        }
        await repo.complete_idempotent(
            endpoint_id=_ENDPOINT_CREATE,
            key=idempotency_key,
            response_status=201,
            response_body=result,
            resource_id=row["id"],
        )
        response.status_code = 201
        return result
    except PlatformAdminError as exc:
        _raise(exc)


@router.get(
    "/platform/tenants/{tenant_id}", response_model=PlatformObject,
    dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))],
)
async def get_tenant(tenant_id: str, repo: RepoDep, _: PlatformAdminDep) -> dict[str, Any]:
    try:
        row = await repo.get_tenant(tenant_id)
        if row is None:
            raise TenantNotFoundError("tenant not found", details={"tenant_id": tenant_id})
        count = await repo.membership_count(tenant_id)
    except PlatformAdminError as exc:
        _raise(exc)
    return {"tenant": tenant_view(row), "settings_summary": settings_summary(row), "membership_count": count}


@router.patch(
    "/platform/tenants/{tenant_id}", response_model=PlatformObject,
    dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))],
)
async def patch_tenant(
    tenant_id: str,
    body: TenantPatchIn,
    repo: RepoDep,
    principal: PlatformAdminDep,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    try:
        current = await repo.get_tenant(tenant_id)
        if current is None:
            raise TenantNotFoundError("tenant not found", details={"tenant_id": tenant_id})
        if str(current["id"]) == str(SYSTEM_TENANT_ID):
            raise ReservedTenantError("the SYSTEM tenant is reserved and cannot be modified")
        patch = validate_patch(payload, current_status=str(current["status"]))
        row, changed = await repo.update_tenant(
            tenant_id=tenant_id,
            patch=patch,
            actor_identity_id=principal.identity_id,
            expected_version=_parse_if_match(if_match),
        )
    except PlatformAdminError as exc:
        _raise(exc)
    return {"tenant": tenant_view(row), "changed_fields": changed}


async def _transition(
    *,
    tenant_id: str,
    body: ReasonIn,
    repo: PlatformAdminRepository,
    principal: PrincipalDep,
    idempotency_key: str,
    target_status: str,
    endpoint_id: str,
    response: Response,
) -> dict[str, Any]:
    try:
        reason = validate_reason(body.reason)
        validate_idempotency_key(idempotency_key)
        replay = await repo.begin_idempotent(
            endpoint_id=endpoint_id,
            key=idempotency_key,
            request_hash=payload_hash({"endpoint": endpoint_id, "tenant_id": tenant_id, "reason": reason}),
            actor_identity_id=principal.identity_id,
        )
        if replay is not None:
            response.status_code = int(replay["response_status"])
            return dict(replay["response_body"])
        row, already, sessions_revoked = await repo.transition_status(
            tenant_id=tenant_id,
            target_status=target_status,
            reason=reason,
            actor_identity_id=principal.identity_id,
        )
        result: dict[str, Any] = {
            "tenant_id": str(row["id"]),
            "status": row["status"],
            "already": already,
            "sessions_revoked": sessions_revoked,
            "tenant": tenant_view(row),
        }
        await repo.complete_idempotent(
            endpoint_id=endpoint_id,
            key=idempotency_key,
            response_status=200,
            response_body=result,
            resource_id=row["id"],
        )
        return result
    except PlatformAdminError as exc:
        _raise(exc)


@router.post(
    "/platform/tenants/{tenant_id}/suspend", response_model=PlatformObject,
    dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))],
)
async def suspend_tenant(
    tenant_id: str,
    body: ReasonIn,
    repo: RepoDep,
    principal: PlatformAdminDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await _transition(
        tenant_id=tenant_id,
        body=body,
        repo=repo,
        principal=principal,
        idempotency_key=idempotency_key,
        target_status="SUSPENDED",
        endpoint_id=_ENDPOINT_SUSPEND,
        response=response,
    )


@router.post(
    "/platform/tenants/{tenant_id}/reactivate", response_model=PlatformObject,
    dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))],
)
async def reactivate_tenant(
    tenant_id: str,
    body: ReasonIn,
    repo: RepoDep,
    principal: PlatformAdminDep,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await _transition(
        tenant_id=tenant_id,
        body=body,
        repo=repo,
        principal=principal,
        idempotency_key=idempotency_key,
        target_status="ACTIVE",
        endpoint_id=_ENDPOINT_REACTIVATE,
        response=response,
    )


@router.get(
    "/platform/tenants/{tenant_id}/memberships", response_model=PlatformObject,
    dependencies=[Depends(require_scope(PLATFORM_TENANT_ADMIN_SCOPE))],
)
async def list_tenant_memberships(
    tenant_id: str,
    repo: RepoDep,
    _: PlatformAdminDep,
    membership_status: str | None = Query(default=None, description="INVITED|ACTIVE|SUSPENDED"),
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=200),
) -> dict[str, Any]:
    try:
        tenant = await repo.get_tenant(tenant_id)
        if tenant is None:
            raise TenantNotFoundError("tenant not found", details={"tenant_id": tenant_id})
        items, next_cursor = await repo.list_memberships(
            tenant_id,
            membership_status=membership_status,
            cursor=cursor,
            limit=limit,
        )
    except PlatformAdminError as exc:
        _raise(exc)
    return {
        "tenant_id": str(tenant["id"]),
        "tenant_code": tenant["code"],
        "items": items,
        "next_cursor": next_cursor,
        "limit": limit,
    }


__all__ = ["router"]
