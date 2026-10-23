"""FS-00 platform "extras" API (task-54): tenant lifecycle extras, custom-field values, user data
scopes, approval-definition / number-sequence patches and the canonical audit read alias.

Endpoints (FS-00 §6; one catalogued scope per route, deny-by-default):

* ``POST /api/v1/platform/tenants/{tenant_id}/offboard`` — ``platform.tenant.manage`` (Idempotency-Key
  required) → OFFBOARDING, export marker, session revocation, SYSTEM-tenant audit.
* ``POST /api/v1/platform/tenants/{tenant_id}/bootstrap-admin`` — ``platform.tenant.manage``
  (Idempotency-Key required) → ``{invitation_id, expires_at}``; plaintext invitation token is never returned.
* ``GET /api/v1/custom-field-values`` — ``platform.customfield.read``.
* ``PUT /api/v1/admin/custom-field-values`` — ``platform.customfield.manage`` (Idempotency-Key required),
  delegated to :mod:`app.core.custom_fields` (typed validation/coercion).
* ``PUT /api/v1/admin/users/{user_id}/data-scopes`` — ``platform.data_scope.manage`` (Idempotency-Key
  required) → replace ``user_data_scope`` rows, audited.
* ``PATCH /api/v1/admin/approval-definitions/{definition_id}`` — ``platform.approval.definition.manage``
  (optional ``If-Match`` version).
* ``PATCH /api/v1/admin/number-sequences/{sequence_id}`` — ``platform.sequence.manage`` (optional
  ``If-Match``; ``next_value`` can never decrease).
* ``GET /api/v1/audit-events`` — ``platform.audit.read`` (canonical alias; ``/admin/audit-events`` stays).

The integrator registers ``router`` in ``app/api/main.py``; this module never edits main/registry/deps.
Responses use the RFC 9457 problem+json envelope via the shared error handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.custom_fields import CustomFieldError
from app.core.repositories.platform_extras import (
    PlatformExtrasError,
    PlatformExtrasRepository,
    parse_version,
)

router = APIRouter(prefix="/api/v1", tags=["platform-extras"])

TENANT_ADMIN = "platform.tenant.manage"
CUSTOM_FIELD_READ = "platform.customfield.read"
CUSTOM_FIELD_MANAGE = "platform.customfield.manage"
DATA_SCOPE_MANAGE = "platform.data_scope.manage"
APPROVAL_DEFINITION_MANAGE = "platform.approval.definition.manage"
SEQUENCE_MANAGE = "platform.sequence.manage"
AUDIT_READ = "platform.audit.read"

#: (method, full path) -> exactly one catalogued scope; asserted by the router contract/coverage checks.
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/platform/tenants/{tenant_id}/offboard"): TENANT_ADMIN,
    ("POST", "/api/v1/platform/tenants/{tenant_id}/bootstrap-admin"): TENANT_ADMIN,
    ("GET", "/api/v1/custom-field-values"): CUSTOM_FIELD_READ,
    ("PUT", "/api/v1/admin/custom-field-values"): CUSTOM_FIELD_MANAGE,
    ("PUT", "/api/v1/admin/users/{user_id}/data-scopes"): DATA_SCOPE_MANAGE,
    ("PATCH", "/api/v1/admin/approval-definitions/{definition_id}"): APPROVAL_DEFINITION_MANAGE,
    ("PATCH", "/api/v1/admin/number-sequences/{sequence_id}"): SEQUENCE_MANAGE,
    ("GET", "/api/v1/audit-events"): AUDIT_READ,
}

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]

#: Endpoint ids used in the idempotency ledger (stable; changing them breaks replay compatibility).
ENDPOINT_OFFBOARD = "platform_extras.tenants.offboard.v1"
ENDPOINT_BOOTSTRAP_ADMIN = "platform_extras.tenants.bootstrap_admin.v1"
ENDPOINT_CUSTOM_FIELD_VALUES = "platform_extras.custom_field_values.upsert.v1"
ENDPOINT_DATA_SCOPES = "platform_extras.users.data_scopes.v1"


# --------------------------------------------------------------------------- dependencies -----
def _repo(db: DbDep) -> PlatformExtrasRepository:
    return PlatformExtrasRepository(db)


RepoDep = Annotated[PlatformExtrasRepository, Depends(_repo)]


def _raise(exc: PlatformExtrasError) -> NoReturn:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _raise_custom_field(exc: CustomFieldError) -> NoReturn:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _require_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key header is required for this endpoint",
                "details": {"header": "Idempotency-Key"},
            },
        )
    return value.strip()


async def _idempotent(
    *,
    request: Request,
    repo: PlatformExtrasRepository,
    principal: PrincipalDep,
    endpoint_id: str,
    idempotency_key: str | None,
    body: Any,
    response: Response,
    execute: Any,
) -> dict[str, Any]:
    """Run ``execute`` under the durable idempotency guard; replay returns the stored response."""
    key = _require_idempotency_key(idempotency_key)
    fingerprint = repo.fingerprint(
        method=request.method, path=request.url.path, tenant_id=principal.tenant_id, endpoint_id=endpoint_id, body=body
    )
    replay = await repo.begin_idempotent(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        fingerprint=fingerprint,
        actor_id=principal.identity_id,
    )
    if replay is not None:
        response.status_code = int(replay["response_status"])
        return dict(replay["response_body"])
    result = await execute()
    await repo.complete_idempotent(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        response_status=200,
        response_body=result,
        resource_id=result.get("tenant_id") or result.get("invitation_id") or result.get("user_id"),
    )
    return result


# --------------------------------------------------------------------------- schemas -----------
class OffboardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)
    export: bool = True


class BootstrapAdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)


class CustomFieldValuesPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=64)
    values: dict[str, Any] = Field(min_length=1)


class DataScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: str = Field(min_length=1, max_length=32)
    scope_value: str = Field(min_length=1, max_length=200)
    include_descendants: bool = False


class DataScopesPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[DataScopeIn] = Field(default_factory=list, max_length=500)


class ApprovalDefinitionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: dict[str, Any] | None = None
    steps: list[Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=100_000)
    is_enabled: bool | None = None


class NumberSequencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str | None = Field(default=None, max_length=32)
    padding: int | None = Field(default=None, ge=1, le=12)
    reset_policy: str | None = Field(default=None, max_length=16)
    gap_policy: str | None = Field(default=None, max_length=16)
    next_value: int | None = Field(default=None, ge=1)


# --------------------------------------------------------------------------- tenant extras -----
@router.post("/platform/tenants/{tenant_id}/offboard")
async def offboard_tenant(
    tenant_id: str,
    body: OffboardIn,
    repo: RepoDep,
    principal: PrincipalDep,
    response: Response,
    request: Request,
    _: Annotated[object, Depends(require_scope(TENANT_ADMIN))],
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    """OFFBOARDING soft-state + export marker; revoked sessions; ledger/audit retained (DM-PLAT-09)."""

    async def _execute() -> dict[str, Any]:
        try:
            return await repo.offboard_tenant(
                tenant_id=tenant_id,
                reason=body.reason,
                export=body.export,
                actor_identity_id=principal.identity_id,
            )
        except PlatformExtrasError as exc:
            _raise(exc)

    return await _idempotent(
        request=request,
        repo=repo,
        principal=principal,
        endpoint_id=ENDPOINT_OFFBOARD,
        idempotency_key=idempotency_key,
        body=body.model_dump(),
        response=response,
        execute=_execute,
    )


@router.post("/platform/tenants/{tenant_id}/bootstrap-admin")
async def bootstrap_admin(
    tenant_id: str,
    body: BootstrapAdminIn,
    repo: RepoDep,
    principal: PrincipalDep,
    response: Response,
    request: Request,
    _: Annotated[object, Depends(require_scope(TENANT_ADMIN))],
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    """Re-issue the tenant bootstrap-admin invitation (no plaintext token is returned)."""

    async def _execute() -> dict[str, Any]:
        try:
            return await repo.bootstrap_admin(
                tenant_id=tenant_id,
                email=body.email,
                actor_identity_id=principal.identity_id,
            )
        except PlatformExtrasError as exc:
            _raise(exc)

    return await _idempotent(
        request=request,
        repo=repo,
        principal=principal,
        endpoint_id=ENDPOINT_BOOTSTRAP_ADMIN,
        idempotency_key=idempotency_key,
        body=body.model_dump(),
        response=response,
        execute=_execute,
    )


# --------------------------------------------------------------------------- custom fields -----
@router.get("/custom-field-values", dependencies=[Depends(require_scope(CUSTOM_FIELD_READ))])
async def read_custom_field_values(
    repo: RepoDep,
    principal: PrincipalDep,
    entity_type: str = Query(min_length=1, max_length=64),
    entity_id: str = Query(min_length=1, max_length=64),
) -> dict[str, Any]:
    try:
        items = await repo.read_custom_values(
            tenant_id=principal.tenant_id, entity_type=entity_type, entity_id=entity_id
        )
    except CustomFieldError as exc:
        _raise_custom_field(exc)
    except PlatformExtrasError as exc:
        _raise(exc)
    return {"items": items, "count": len(items)}


@router.put("/admin/custom-field-values")
async def put_custom_field_values(
    body: CustomFieldValuesPut,
    repo: RepoDep,
    principal: PrincipalDep,
    response: Response,
    request: Request,
    _: Annotated[object, Depends(require_scope(CUSTOM_FIELD_MANAGE))],
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    """Validate + upsert typed custom-field values for one entity (FS-00 §6 contract)."""

    async def _execute() -> dict[str, Any]:
        try:
            values = await repo.write_custom_values(
                tenant_id=principal.tenant_id,
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                values=body.values,
                actor_id=principal.identity_id,
            )
        except CustomFieldError as exc:
            _raise_custom_field(exc)
        except PlatformExtrasError as exc:
            _raise(exc)
        return {
            "entity_type": body.entity_type.strip().upper(),
            "entity_id": body.entity_id,
            "values": values,
            "count": len(values),
        }

    return await _idempotent(
        request=request,
        repo=repo,
        principal=principal,
        endpoint_id=ENDPOINT_CUSTOM_FIELD_VALUES,
        idempotency_key=idempotency_key,
        body=body.model_dump(),
        response=response,
        execute=_execute,
    )


# --------------------------------------------------------------------------- data scopes ------
@router.put("/admin/users/{user_id}/data-scopes")
async def replace_user_data_scopes(
    user_id: str,
    body: DataScopesPut,
    repo: RepoDep,
    principal: PrincipalDep,
    response: Response,
    request: Request,
    _: Annotated[object, Depends(require_scope(DATA_SCOPE_MANAGE))],
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    """Replace the user's data-scope rows (tenant-scoped; user from another tenant → 404)."""

    async def _execute() -> dict[str, Any]:
        try:
            scopes = await repo.replace_user_data_scopes(
                tenant_id=principal.tenant_id,
                user_id=user_id,
                scopes=[scope.model_dump() for scope in body.scopes],
                actor_id=principal.identity_id,
            )
        except PlatformExtrasError as exc:
            _raise(exc)
        return {"user_id": user_id, "scopes": scopes, "count": len(scopes)}

    return await _idempotent(
        request=request,
        repo=repo,
        principal=principal,
        endpoint_id=ENDPOINT_DATA_SCOPES,
        idempotency_key=idempotency_key,
        body=body.model_dump(),
        response=response,
        execute=_execute,
    )


# ------------------------------------------------------------------- definitions / sequences --
@router.patch("/admin/approval-definitions/{definition_id}")
async def patch_approval_definition(
    definition_id: str,
    body: ApprovalDefinitionPatch,
    repo: RepoDep,
    principal: PrincipalDep,
    _: Annotated[object, Depends(require_scope(APPROVAL_DEFINITION_MANAGE))],
    if_match: IfMatch = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "at least one editable field is required", "details": []},
        )
    try:
        expected = parse_version(if_match)
        definition = await repo.patch_approval_definition(
            tenant_id=principal.tenant_id,
            definition_id=definition_id,
            payload=payload,
            expected_version=expected,
            actor_id=principal.identity_id,
        )
    except PlatformExtrasError as exc:
        _raise(exc)
    return {"definition": definition}


@router.patch("/admin/number-sequences/{sequence_id}")
async def patch_number_sequence(
    sequence_id: str,
    body: NumberSequencePatch,
    repo: RepoDep,
    principal: PrincipalDep,
    _: Annotated[object, Depends(require_scope(SEQUENCE_MANAGE))],
    if_match: IfMatch = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": "at least one editable field is required", "details": []},
        )
    try:
        expected = parse_version(if_match)
        sequence = await repo.patch_number_sequence(
            tenant_id=principal.tenant_id,
            sequence_id=sequence_id,
            payload=payload,
            expected_version=expected,
            actor_id=principal.identity_id,
        )
    except PlatformExtrasError as exc:
        _raise(exc)
    return {"sequence": sequence}


# --------------------------------------------------------------------------- audit alias ------
@router.get("/audit-events", dependencies=[Depends(require_scope(AUDIT_READ))])
async def query_audit_events(
    repo: RepoDep,
    principal: PrincipalDep,
    resource_type: str | None = Query(default=None, max_length=128),
    resource_id: str | None = Query(default=None, max_length=200),
    actor_id: str | None = Query(default=None, max_length=64),
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Canonical audit read alias (tenant-scoped; ``/admin/audit-events`` remains for compatibility)."""
    try:
        items, next_cursor = await repo.query_audit_events(
            tenant_id=principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=actor_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            cursor=cursor,
            limit=limit,
        )
    except PlatformExtrasError as exc:
        _raise(exc)
    return {"items": items, "next_cursor": next_cursor, "limit": limit}


__all__ = ["ROUTE_SCOPES", "router"]
