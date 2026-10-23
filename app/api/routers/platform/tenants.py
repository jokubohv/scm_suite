"""FS-00 self-service tenancy helpers (task-39).

The auth router (task-38) owns the canonical ``GET /api/v1/me``, ``GET /api/v1/me/tenants`` and
``POST /api/v1/auth/switch-tenant`` paths. This module therefore exposes:

* ``router`` with a single non-duplicated self-service route ``GET /api/v1/me/memberships`` (membership
  summary + switch candidates for the authenticated identity), and
* reusable async helpers (``list_memberships``, ``resolve_switch_target``) for the auth router so tenant
  switching shares one repository implementation (active membership + active tenant + RLS-scoped rows).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import DbDep, PrincipalDep
from app.core.repositories.rbac import RbacRepository

router = APIRouter(prefix="/api/v1", tags=["self-service"])


def _repo(db: DbDep) -> RbacRepository:
    return RbacRepository(db)


RepoDep = Annotated[RbacRepository, Depends(_repo)]


async def list_memberships(repo: RbacRepository, identity_id: str) -> list[dict[str, Any]]:
    """All memberships for an identity (tenant code/name/status, membership status, roles)."""
    return await repo.list_identity_memberships(identity_id)


async def resolve_switch_target(repo: RbacRepository, identity_id: str, tenant_id: str) -> dict[str, Any] | None:
    """Validate a tenant switch target: ACTIVE membership in an ACTIVE tenant (else ``None``)."""
    return await repo.resolve_switch_target(identity_id, tenant_id)


@router.get("/me/memberships")
async def my_memberships(repo: RepoDep, principal: PrincipalDep) -> dict[str, Any]:
    """Self-service membership summary + switch candidates (self-service allowlist route)."""
    items = await list_memberships(repo, principal.identity_id)
    active = [item for item in items if item["membership_status"] == "ACTIVE" and item["tenant_status"] == "ACTIVE"]
    return {
        "items": items,
        "active_count": len(active),
        "selection_required": len(active) > 1,
        "can_switch": [item["tenant_id"] for item in active],
        "active_tenant_id": principal.tenant_id,
    }


__all__ = ["list_memberships", "resolve_switch_target", "router"]
