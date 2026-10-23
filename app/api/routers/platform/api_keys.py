"""FS-00 platform API-key lifecycle endpoints (task-43).

Endpoints (FS-00 §4/§6; Δ-RBAC scope ``platform.admin``): create/list/revoke/rotate machine
credentials. The full secret is returned exactly once on create/rotate; only prefix + HMAC digest are
persisted. All responses use the platform problem+json envelope and every mutation is audited.

Session administration is intentionally **not** reimplemented here: the RBAC router already exposes
``POST /api/v1/admin/users/{user_id}/revoke-sessions`` (scope ``platform.sessions.revoke``), which is the
canonical session-revocation surface for this feature set.

The integrator registers this module's ``router``; this file never edits registry/main/deps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, SettingsDep, require_scope, scope_registry
from app.core.config import Settings
from app.core.repositories.api_keys import (
    ApiKeyRepository,
    derive_api_key_pepper,
    public_key_row,
)
from app.core.security.tokens import ApiKeyHasher

router = APIRouter(prefix="/api/v1", tags=["platform-api-keys"])

API_KEY_ADMIN = "platform.admin"


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


def _hasher(settings: Settings) -> ApiKeyHasher:
    return ApiKeyHasher(derive_api_key_pepper(settings.jwt_secret))


def _repo(db: DbDep, settings: SettingsDep) -> ApiKeyRepository:
    return ApiKeyRepository(db, registry=scope_registry(), hasher=_hasher(settings))


RepoDep = Annotated[ApiKeyRepository, Depends(_repo)]


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "API_KEY_NOT_FOUND", "message": "api key not found"},
    )


def _invalid(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message, "details": details})


def _issued_payload(issued: Any) -> dict[str, Any]:
    payload = public_key_row(issued.row)
    payload.update(
        {
            "secret": issued.material.full_key,
            "warning": "This secret is shown exactly once; store it securely now.",
        }
    )
    return payload


@router.post("/admin/api-keys", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_scope(API_KEY_ADMIN))])
async def create_api_key(body: ApiKeyCreate, repo: RepoDep, principal: PrincipalDep) -> dict[str, Any]:
    """Create a machine credential. Scopes are catalogue-validated and SoD-checked."""
    try:
        issued = await repo.create(
            tenant_id=principal.tenant_id, actor_id=principal.app_user_id,
            name=body.name, scopes=body.scopes, expires_at=body.expires_at,
        )
    except ValueError as exc:  # past expiry or invalid name payload
        raise _invalid("VALIDATION_ERROR", str(exc)) from exc
    return _issued_payload(issued)


@router.get("/admin/api-keys", dependencies=[Depends(require_scope(API_KEY_ADMIN))])
async def list_api_keys(
    repo: RepoDep,
    principal: PrincipalDep,
    include_revoked: bool = Query(default=False),
    include_expired: bool = Query(default=True),
) -> dict[str, Any]:
    """List credentials for the active tenant (never returns secrets or digests)."""
    rows = await repo.list(principal.tenant_id, include_revoked=include_revoked,
                           include_expired=include_expired)
    items = [public_key_row(row) for row in rows]
    return {"items": items, "total": len(items)}


@router.delete("/admin/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_scope(API_KEY_ADMIN))])
async def revoke_api_key(key_id: str, repo: RepoDep, principal: PrincipalDep) -> Response:
    """Revoke a credential (idempotent: an already-revoked key still returns 204)."""
    updated = await repo.revoke(tenant_id=principal.tenant_id, actor_id=principal.app_user_id,
                                key_id=key_id, reason="admin revoke")
    if updated is None:
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/api-keys/{key_id}/rotate", dependencies=[Depends(require_scope(API_KEY_ADMIN))])
async def rotate_api_key(key_id: str, repo: RepoDep, principal: PrincipalDep) -> dict[str, Any]:
    """Rotate the secret in place (old secret invalid immediately); new secret shown once."""
    issued = await repo.rotate(tenant_id=principal.tenant_id, actor_id=principal.app_user_id,
                               key_id=key_id)
    if issued is None:
        raise _not_found()
    return _issued_payload(issued)


__all__ = ["API_KEY_ADMIN", "router"]
