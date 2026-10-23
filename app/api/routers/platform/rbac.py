"""FS-00 RBAC / tenant-administration API (task-39).

Endpoints are declared per `feature-sets/FS-00.md` §6 and `design/PERMISSIONS-ROLES-ADDENDUM.md` §5:
catalogue read, role lifecycle/permissions/data-scopes, membership/role administration, invitations,
tenant settings and SoD policy. Every non-public route declares exactly one capability scope via
``require_scope`` (deny-by-default); guards come from the pure ``ScopeRegistry`` (assignability, SoD,
privilege escalation, template immutability, last-administrator lockout). Errors render as problem+json
through ``app.api.errors``.

The integrator registers this router's ``router`` in ``app/api/registry.py``; this module never edits
the registry/main/deps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope, scope_registry
from app.core.models import platform as p
from app.core.repositories.rbac import ALLOWED_SETTING_NAMESPACES, RbacRepository
from app.core.security.errors import (
    LastAdminLockout,
    RoleInUse,
    TemplateImmutable,
)
from app.core.security.scopes import (
    EffectivePermissions,
    RoleTemplate,
    ScopeRegistry,
    SodContext,
)

router = APIRouter(prefix="/api/v1", tags=["platform-rbac"])

ROLE_ADMIN = "platform.role.manage"
USER_ADMIN = "platform.users.manage"
INVITATION_ADMIN = "platform.invitations.manage"
SESSION_ADMIN = "platform.sessions.revoke"
DATA_SCOPE_ADMIN = "platform.data_scope.manage"
TENANT_ADMIN = "platform.admin"
ACCOUNT_ADMIN = "platform.accounting.manage"
_SETTING_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{1,120}$"
_SOD_MODE_KEY = "sod_enforcement"


# --------------------------------------------------------------------------- dependencies -----
def _repo(db: DbDep) -> RbacRepository:
    return RbacRepository(db)


RepoDep = Annotated[RbacRepository, Depends(_repo)]


def _registry() -> ScopeRegistry:
    return scope_registry()


RegistryDep = Annotated[ScopeRegistry, Depends(_registry)]


def _actor_permissions(principal: PrincipalDep) -> EffectivePermissions:
    return EffectivePermissions(scopes=principal.scopes)


ActorPermissionsDep = Annotated[EffectivePermissions, Depends(_actor_permissions)]


async def _actor_is_platform_admin(repo: RepoDep, principal: PrincipalDep) -> bool:
    return await repo.is_platform_admin(principal.identity_id)


# --------------------------------------------------------------------------- errors -----------
def _not_found(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message, "details": details})


def _unprocessable(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message, "details": details})


# --------------------------------------------------------------------------- schemas ----------
class DataScopeIn(BaseModel):
    scope_type: Literal["LOCATION", "CLASS", "DEPARTMENT", "SUBSIDIARY"]
    scope_value: str = Field(min_length=1, max_length=200)
    include_descendants: bool = False


class RoleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    scope_ids: list[str] = Field(default_factory=list, description="capability scope names")
    data_scopes: list[DataScopeIn] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    inactive: bool | None = None
    reassign_to_role_id: str | None = None


class RolePermissionsUpdate(BaseModel):
    scope_ids: list[str]


class RoleDataScopesUpdate(BaseModel):
    scopes: list[DataScopeIn] = Field(default_factory=list)


class RoleClone(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=200)
    copy_data_scopes: bool = True


class RoleAssignment(BaseModel):
    role_id: str
    effective_from: datetime | None = None
    effective_to: datetime | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=60)
    membership_status: Literal["ACTIVE", "SUSPENDED"] | None = None
    role_ids: list[str] | None = None


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    intended_role_ids: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class TenantSettingIn(BaseModel):
    value: Any
    value_type: Literal["JSON", "STRING", "NUMBER", "BOOLEAN"] = "JSON"


class TenantSettingsPatch(BaseModel):
    settings: list[dict[str, Any]] = Field(min_length=1)


class SodPolicyIn(BaseModel):
    sod_enforcement: Literal["HARD", "WARN"]
    reason: str = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------------- helpers ----------
async def _role_or_404(repo: RbacRepository, tenant_id: str, role_id: str) -> dict[str, Any]:
    role = await repo.get_role(tenant_id, role_id)
    if role is None:
        raise _not_found("ROLE_NOT_FOUND", "role not found", role_id=role_id)
    return role


async def _user_or_404(repo: RbacRepository, tenant_id: str, user_id: str) -> dict[str, Any]:
    user = await repo.get_user(tenant_id, user_id)
    if user is None:
        raise _not_found("USER_NOT_FOUND", "membership not found", user_id=user_id)
    return user


async def _scopes_or_422(repo: RbacRepository, tenant_id: str, scopes: Sequence[str]) -> list[str]:
    resolved = list(dict.fromkeys(scopes))
    rows = await repo.scope_rows(tenant_id, resolved)
    missing = sorted(set(resolved) - set(rows))
    if missing:
        raise _unprocessable(
            "SCOPE_NOT_PROVISIONED",
            "capability scope(s) not provisioned for this tenant",
            missing=missing,
        )
    return resolved


def _require_assignable(registry: ScopeRegistry, scopes: Sequence[str]) -> list[str]:
    canonical = [registry.catalogue.canonical(scope) for scope in dict.fromkeys(scopes)]
    return sorted(registry.require_assignable(canonical))


def _require_sod(registry: ScopeRegistry, scopes: Sequence[str], *, allow_break_glass: bool = False) -> None:
    registry.require_no_sod_conflict(scopes, context=SodContext.ROLE_UPDATE, allow_break_glass=allow_break_glass)


def _require_grant(registry: ScopeRegistry, actor: EffectivePermissions, scopes: Sequence[str], *, is_platform_admin: bool) -> None:
    registry.require_can_grant(actor, scopes, is_platform_admin=is_platform_admin)


def _is_admin_role(role: Mapping[str, Any]) -> bool:
    return str(role.get("code")) == "admin"


def _role_template(registry: ScopeRegistry, role: Mapping[str, Any]) -> RoleTemplate | None:
    if not role.get("is_template"):
        return None
    return registry.catalogue.templates_by_key.get(str(role.get("code")))


def _ensure_role_mutable(registry: ScopeRegistry, role: Mapping[str, Any]) -> None:
    if role.get("is_template") or not role.get("is_editable"):
        template = _role_template(registry, role)
        if template is not None:
            template.require_mutable()
        raise TemplateImmutable("system role templates are clone-only", details={"role": role.get("code")})


# --------------------------------------------------------------------------- catalogue --------
@router.get("/admin/permissions", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def list_permissions(
    registry: RegistryDep,
    domain: str | None = None,
    action: str | None = None,
    assignable: bool | None = None,
    requires_approval: bool | None = None,
    owner_feature_set: str | None = None,
) -> dict[str, Any]:
    catalogue = registry.catalogue
    items = []
    for scope in catalogue.scopes:
        if domain and scope.domain != domain:
            continue
        if action and scope.action != action:
            continue
        if assignable is not None and scope.assignable is not assignable:
            continue
        if requires_approval is not None and scope.requires_approval is not requires_approval:
            continue
        if owner_feature_set and scope.owner_feature_set != owner_feature_set:
            continue
        items.append(
            {
                "scope": scope.scope,
                "domain": scope.domain,
                "action": scope.action,
                "requires_approval": scope.requires_approval,
                "is_platform_only": scope.platform_only,
                "is_assignable": scope.assignable and not scope.platform_only and not scope.service_only,
                "service_only": scope.service_only,
                "owner_feature_set": scope.owner_feature_set,
            }
        )
    return {
        "version": catalogue.plan_version,
        "counts": {
            "scopes": len(catalogue.scopes),
            "assignable": len(catalogue.assignable_scopes()),
            "service_only": sum(1 for scope in catalogue.scopes if scope.service_only),
            "platform_only": sum(1 for scope in catalogue.scopes if scope.platform_only),
            "role_templates": len(catalogue.role_templates),
            "sod_pairs": len(catalogue.sod_pairs),
        },
        "aliases": dict(catalogue.aliases),
        "items": items,
    }


# ------------------------------------------------------------------------------- roles --------
@router.get("/admin/roles", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def list_roles(
    repo: RepoDep,
    principal: PrincipalDep,
    include_inactive: bool = False,
    is_template: bool | None = None,
) -> dict[str, Any]:
    roles = await repo.list_roles(principal.tenant_id, include_inactive=include_inactive, is_template=is_template)
    return {
        "items": [
            {
                "id": role["id"],
                "code": role["code"],
                "name": role["name"],
                "description": role.get("description") or "",
                "is_template": role["is_template"],
                "is_editable": role["is_editable"],
                "scope_count": role["scope_count"],
                "assignment_count": role["assignment_count"],
                "inactive": role["inactive_at"] is not None,
            }
            for role in roles
        ]
    }


@router.post("/admin/roles", status_code=201, dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def create_role(
    body: RoleCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    is_platform_admin = await repo.is_platform_admin(principal.identity_id)
    if body.code in registry.catalogue.templates_by_key:
        raise _unprocessable("ROLE_CODE_RESERVED", "role code is a reserved system template", role_code=body.code)
    if await repo.role_code_exists(principal.tenant_id, body.code):
        raise _unprocessable("ROLE_CODE_EXISTS", "role code already exists", role_code=body.code)
    scopes = _require_assignable(registry, body.scope_ids)
    _require_sod(registry, scopes)
    _require_grant(registry, actor, scopes, is_platform_admin=is_platform_admin)
    await _scopes_or_422(repo, principal.tenant_id, scopes)
    role = await repo.create_role(
        principal.tenant_id,
        code=body.code,
        name=body.name,
        description=body.description,
        scopes=scopes,
        data_scopes=[entry.model_dump() for entry in body.data_scopes],
        is_template=False,
        is_editable=True,
        actor_id=principal.identity_id,
    )
    return _role_payload(role)


def _role_payload(role: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": role["id"],
        "code": role["code"],
        "name": role["name"],
        "description": role.get("description") or "",
        "is_template": role["is_template"],
        "is_editable": role["is_editable"],
        "inactive": role["inactive_at"] is not None,
        "scopes": role["scopes"],
        "data_scopes": role["data_scopes"],
        "assignment_count": role["assignment_count"],
    }


@router.get("/admin/roles/{role_id}", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def get_role(
    role_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    return {"role": _role_payload(role), "scopes": role["scopes"], "data_scopes": role["data_scopes"], "assignment_count": role["assignment_count"]}


@router.patch("/admin/roles/{role_id}", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def patch_role(
    role_id: str,
    body: RoleUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    _ensure_role_mutable(registry, role)
    if body.reassign_to_role_id:
        target = await _role_or_404(repo, principal.tenant_id, body.reassign_to_role_id)
        if target["id"] == role["id"]:
            raise _unprocessable("ROLE_REASSIGN_INVALID", "cannot reassign a role to itself")
        assignments = await repo.count_roles_with_assignments(principal.tenant_id, role_id)
        if assignments:
            await _bulk_reassign(repo, principal.tenant_id, role_id, str(target["id"]), principal.identity_id)
    updated = await repo.update_role(
        principal.tenant_id, role_id, name=body.name, description=body.description,
        inactive=body.inactive, actor_id=principal.identity_id,
    )
    if updated is None:
        raise _not_found("ROLE_NOT_FOUND", "role not found")
    return _role_payload(updated)


async def _bulk_reassign(repo: RbacRepository, tenant_id: str, from_role: str, to_role: str, actor_id: str) -> None:
    assignments = await repo.session.execute(
        p.user_role.select().where(
            p.user_role.c.tenant_id == tenant_id,
            p.user_role.c.role_id == from_role,
            p.user_role.c.effective_to.is_(None),
        )
    )
    for row in assignments.mappings().all():
        await repo.assign_role(tenant_id, row["user_id"], to_role, actor_id=actor_id)
        await repo.end_role_assignment(tenant_id, row["user_id"], from_role, actor_id=actor_id)


@router.delete("/admin/roles/{role_id}", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def delete_role(
    role_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    body: Annotated[RoleUpdate | None, Body()] = None,
) -> dict[str, Any]:
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    _ensure_role_mutable(registry, role)
    reason = ""
    reassign_to = None
    if body is not None:
        reason = body.description or ""
        reassign_to = body.reassign_to_role_id
    if reassign_to:
        await _bulk_reassign(repo, principal.tenant_id, role_id, reassign_to, principal.identity_id)
    in_use = await repo.count_roles_with_assignments(principal.tenant_id, role_id)
    if in_use and not reassign_to:
        raise RoleInUse(
            "role still has active assignments; provide reassign_to_role_id",
            details={"role_id": role_id, "assignment_count": in_use},
        )
    await repo.audit(
        tenant_id=principal.tenant_id, actor_id=principal.identity_id, action="role.deleted",
        resource_type="role", resource_id=role_id, diff={"reason": reason, "reassign_to": reassign_to},
    )
    await repo.inactivate_role(principal.tenant_id, role_id, actor_id=principal.identity_id)
    return {"id": role_id, "status": "INACTIVE"}


@router.post("/admin/roles/{role_id}/clone", status_code=201, dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def clone_role(
    role_id: str,
    body: RoleClone,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    source = await _role_or_404(repo, principal.tenant_id, role_id)
    if not (source.get("is_template") or source.get("is_editable")):
        raise _unprocessable("ROLE_NOT_CLONEABLE", "role is not cloneable")
    if body.code in registry.catalogue.templates_by_key or await repo.role_code_exists(principal.tenant_id, body.code):
        raise _unprocessable("ROLE_CODE_EXISTS", "role code already exists", role_code=body.code)
    scopes = _require_assignable(registry, source["scopes"])
    _require_sod(registry, scopes)
    _require_grant(registry, actor, scopes, is_platform_admin=await repo.is_platform_admin(principal.identity_id))
    await _scopes_or_422(repo, principal.tenant_id, scopes)
    data_scopes = source["data_scopes"] if body.copy_data_scopes else []
    role = await repo.create_role(
        principal.tenant_id, code=body.code, name=body.name or f"{source['name']} (copy)",
        description=source.get("description") or "", scopes=scopes, data_scopes=data_scopes,
        is_template=False, is_editable=True, actor_id=principal.identity_id,
    )
    return _role_payload(role)


@router.put("/admin/roles/{role_id}/permissions", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def replace_role_permissions(
    role_id: str,
    body: RolePermissionsUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    _ensure_role_mutable(registry, role)
    scopes = _require_assignable(registry, body.scope_ids)
    _require_sod(registry, scopes)
    _require_grant(registry, actor, scopes, is_platform_admin=await repo.is_platform_admin(principal.identity_id))
    await _scopes_or_422(repo, principal.tenant_id, scopes)
    final, diff = await repo.replace_role_permissions(principal.tenant_id, role_id, scopes, actor_id=principal.identity_id)
    return {"scopes": final, "diff": diff}


@router.put("/admin/roles/{role_id}/data-scopes", dependencies=[Depends(require_scope(DATA_SCOPE_ADMIN))])
async def replace_role_data_scopes(
    role_id: str,
    body: RoleDataScopesUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    if role.get("is_template"):
        raise TemplateImmutable("system role templates are clone-only", details={"role": role["code"]})
    await repo.replace_role_data_scopes(
        principal.tenant_id, role_id, [entry.model_dump() for entry in body.scopes], actor_id=principal.identity_id
    )
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    return {"role_id": role_id, "data_scopes": role["data_scopes"]}


# ------------------------------------------------------------------------------- SoD ----------
@router.get("/admin/sod-policy", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def get_sod_policy(
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    setting = await repo.get_tenant_setting(principal.tenant_id, "rbac", _SOD_MODE_KEY)
    mode = "HARD"
    if setting and str(setting.get("value")).strip('"') in {"HARD", "WARN"}:
        mode = str(setting["value"]).strip('"')
    return {
        "sod_enforcement": mode,
        "requester_not_approver": True,
        "pairs": [{"a": pair.a, "b": pair.b, "enforcement": pair.enforcement} for pair in registry.catalogue.sod_pairs],
    }


@router.put("/admin/sod-policy", dependencies=[Depends(require_scope(ROLE_ADMIN))])
async def update_sod_policy(
    body: SodPolicyIn,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    setting = await repo.set_tenant_setting(
        principal.tenant_id, namespace="rbac", key=_SOD_MODE_KEY, value=body.sod_enforcement,
        value_type="STRING", actor_id=principal.identity_id,
    )
    await repo.audit(
        tenant_id=principal.tenant_id, actor_id=principal.identity_id, action="sod_policy.updated",
        resource_type="tenant_setting", resource_id="rbac.sod_enforcement",
        diff={"sod_enforcement": body.sod_enforcement, "reason": body.reason},
    )
    return {
        "sod_enforcement": setting["value"],
        "requester_not_approver": True,
        "note": "WARN only downgrades custom-role creation warnings; approval-time requester!=approver cannot be disabled.",
    }


# ------------------------------------------------------------------------------- users --------
def _user_payload(user: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "app_user_id": user["id"],
        "identity_id": user["identity_id"],
        "email": user["identity"]["email"],
        "display_name": user["identity"]["display_name"],
        "membership_status": user["membership_status"],
        "is_service_account": user["is_service_account"],
        "joined_at": user["joined_at"],
        "roles": user["roles"],
        "identity": user["identity"],
        "data_scopes": user["data_scopes"],
    }


@router.get("/admin/users", dependencies=[Depends(require_scope(USER_ADMIN))])
async def list_users(
    repo: RepoDep,
    principal: PrincipalDep,
    membership_status: str | None = None,
    email: str | None = None,
    role_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    users, next_cursor = await repo.list_users(
        principal.tenant_id, membership_status=membership_status, email=email, role_id=role_id, limit=limit, cursor=cursor
    )
    return {
        "items": [
            {
                "app_user_id": user["id"],
                "identity_id": user["identity_id"],
                "email": user["identity_email"],
                "display_name": user["identity_display_name"],
                "membership_status": user["membership_status"],
                "roles": user["roles"],
                "joined_at": user["joined_at"],
            }
            for user in users
        ],
        "next_cursor": next_cursor,
    }


@router.get("/admin/users/{user_id}", dependencies=[Depends(require_scope(USER_ADMIN))])
async def get_user(
    user_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    user = await _user_or_404(repo, principal.tenant_id, user_id)
    return _user_payload(user)


@router.patch("/admin/users/{user_id}", dependencies=[Depends(require_scope(USER_ADMIN))])
async def patch_user(
    user_id: str,
    body: UserUpdate,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    user = await _user_or_404(repo, principal.tenant_id, user_id)
    is_platform_admin = await repo.is_platform_admin(principal.identity_id)
    active_admins = await repo.count_active_admins(principal.tenant_id)
    was_admin = "admin" in user["roles"] and user["membership_status"] == "ACTIVE"

    if body.role_ids is not None:
        target_roles = list(dict.fromkeys(body.role_ids))
        rows = await repo.session.execute(
            p.role.select().where(p.role.c.tenant_id == principal.tenant_id, p.role.c.id.in_(target_roles))
        )
        role_rows = list(rows.mappings().all())
        if len(role_rows) != len(target_roles):
            raise _unprocessable("ROLE_NOT_FOUND", "one or more roles do not exist", role_ids=target_roles)
        target_codes = {str(row["code"]) for row in role_rows}
        union_scopes: set[str] = set()
        for row in role_rows:
            union_scopes.update(await repo.role_scope_names(principal.tenant_id, row["id"]))
        _require_grant(registry, actor, sorted(union_scopes), is_platform_admin=is_platform_admin)
        _require_sod(registry, sorted(union_scopes), allow_break_glass="admin" in target_codes)
        if was_admin and "admin" not in target_codes and active_admins <= 1:
            raise LastAdminLockout("cannot remove the last active tenant administrator", details={"user_id": user_id})
        current = set(user["roles"])
        for role in role_rows:
            if str(role["code"]) not in current:
                await repo.assign_role(principal.tenant_id, user_id, str(role["id"]), actor_id=principal.identity_id)
        keep = {str(role["code"]) for role in role_rows}
        stmt = p.role.select().where(p.role.c.tenant_id == principal.tenant_id)
        if keep:
            stmt = stmt.where(p.role.c.code.not_in(keep))
        rows = await repo.session.execute(stmt)
        for role in rows.mappings().all():
            await repo.end_role_assignment(principal.tenant_id, user_id, str(role["id"]), actor_id=principal.identity_id)
    if body.membership_status == "SUSPENDED" and was_admin and active_admins <= 1:
        raise LastAdminLockout("cannot suspend the last active tenant administrator", details={"user_id": user_id})
    updated = await repo.update_user(
        principal.tenant_id, user_id, display_name=body.display_name, locale=body.locale,
        timezone=body.timezone, membership_status=body.membership_status, actor_id=principal.identity_id,
    )
    if updated is None:
        raise _not_found("USER_NOT_FOUND", "membership not found")
    return _user_payload(updated)


@router.post("/admin/users/{user_id}/roles", dependencies=[Depends(require_scope(USER_ADMIN))])
async def assign_role(
    user_id: str,
    body: RoleAssignment,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    user = await _user_or_404(repo, principal.tenant_id, user_id)
    role = await _role_or_404(repo, principal.tenant_id, body.role_id)
    combined = set(await repo.role_scope_names(principal.tenant_id, body.role_id))
    for code in user["roles"]:
        rows = await repo.session.execute(
            p.role.select().where(p.role.c.tenant_id == principal.tenant_id, p.role.c.code == code)
        )
        row = rows.mappings().first()
        if row:
            combined.update(await repo.role_scope_names(principal.tenant_id, row["id"]))
    _require_grant(registry, actor, sorted(combined), is_platform_admin=await repo.is_platform_admin(principal.identity_id))
    _require_sod(registry, sorted(combined), allow_break_glass=str(role["code"]) == "admin")
    await repo.assign_role(
        principal.tenant_id, user_id, str(role["id"]),
        effective_from=body.effective_from, effective_to=body.effective_to, actor_id=principal.identity_id,
    )
    updated = await _user_or_404(repo, principal.tenant_id, user_id)
    return {"app_user_id": user_id, "roles": updated["roles"]}


@router.delete("/admin/users/{user_id}/roles/{role_id}", status_code=204, dependencies=[Depends(require_scope(USER_ADMIN))])
async def unassign_role(
    user_id: str,
    role_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
) -> None:
    await _user_or_404(repo, principal.tenant_id, user_id)
    role = await _role_or_404(repo, principal.tenant_id, role_id)
    is_admin = str(role["code"]) == "admin"
    active_admins = await repo.count_active_admins(principal.tenant_id)
    if is_admin and await repo.is_active_admin(principal.tenant_id, user_id) and active_admins <= 1:
        raise LastAdminLockout("cannot remove the last active tenant administrator", details={"user_id": user_id})
    await repo.end_role_assignment(principal.tenant_id, user_id, role_id, actor_id=principal.identity_id)
    return None


@router.get("/admin/users/{user_id}/effective-permissions", dependencies=[Depends(require_scope(USER_ADMIN))])
async def user_effective_permissions(
    user_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    await _user_or_404(repo, principal.tenant_id, user_id)
    resolved = await repo.effective_permissions(principal.tenant_id, user_id)
    sod_pairs = registry.sod_conflicts(resolved.get("scopes", []))
    return {
        "app_user_id": user_id,
        "roles": resolved.get("roles", []),
        "scopes": resolved.get("scopes", []),
        "data_scopes": resolved.get("data_scopes", []),
        "sod_pairs": [{"a": pair.a, "b": pair.b} for pair in sod_pairs],
    }


@router.post("/admin/users/{user_id}/revoke-sessions", dependencies=[Depends(require_scope(SESSION_ADMIN))])
async def revoke_user_sessions(
    user_id: str,
    body: ReasonIn,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    await _user_or_404(repo, principal.tenant_id, user_id)
    active = await repo.count_active_sessions(principal.tenant_id, user_id)
    token_version = await repo.revoke_sessions(
        principal.tenant_id, user_id, reason=body.reason, actor_id=principal.identity_id
    )
    return {"sessions_revoked": active, "token_version": token_version}


# ------------------------------------------------------------------------- invitations --------
@router.post("/admin/invitations", status_code=201, dependencies=[Depends(require_scope(INVITATION_ADMIN))])
async def create_invitation(
    body: InvitationCreate,
    repo: RepoDep,
    principal: PrincipalDep,
    registry: RegistryDep,
    actor: ActorPermissionsDep,
) -> dict[str, Any]:
    roles = await _roles_by_ids(repo, principal.tenant_id, body.intended_role_ids)
    union: set[str] = set()
    for role in roles:
        union.update(await repo.role_scope_names(principal.tenant_id, role["id"]))
    _require_grant(registry, actor, sorted(union), is_platform_admin=await repo.is_platform_admin(principal.identity_id))
    _require_sod(registry, sorted(union), allow_break_glass=any(str(role["code"]) == "admin" for role in roles))
    invitation = await repo.create_invitation(
        principal.tenant_id, email=body.email, intended_role_ids=[str(role["id"]) for role in roles],
        invited_by=principal.app_user_id, expires_at=body.expires_at,
    )
    return {
        "invitation_id": invitation["id"], "status": "PENDING",
        "token": invitation["token"], "expires_at": invitation["expires_at"],
    }


async def _roles_by_ids(repo: RbacRepository, tenant_id: str, role_ids: Sequence[str]) -> list[Mapping[str, Any]]:
    if not role_ids:
        return []
    rows = await repo.session.execute(
        p.role.select().where(p.role.c.tenant_id == tenant_id, p.role.c.id.in_(list(role_ids)))
    )
    roles: list[Mapping[str, Any]] = [dict(row) for row in rows.mappings().all()]
    if len(roles) != len(set(role_ids)):
        raise _unprocessable("ROLE_NOT_FOUND", "one or more roles do not exist", role_ids=list(role_ids))
    return roles


@router.get("/admin/invitations", dependencies=[Depends(require_scope(INVITATION_ADMIN))])
async def list_invitations(
    repo: RepoDep,
    principal: PrincipalDep,
    status: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    return {"items": await repo.list_invitations(principal.tenant_id, status=status, email=email), "next_cursor": None}


@router.post("/admin/invitations/{invitation_id}/resend", dependencies=[Depends(require_scope(INVITATION_ADMIN))])
async def resend_invitation(
    invitation_id: str,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    result = await repo.resend_invitation(principal.tenant_id, invitation_id, actor_id=principal.identity_id)
    if result is None:
        raise _not_found("INVITATION_NOT_FOUND", "pending invitation not found", invitation_id=invitation_id)
    return {"invitation_id": result["id"], "status": "PENDING", "token": result["token"], "expires_at": result["expires_at"]}


@router.post("/admin/invitations/{invitation_id}/revoke", dependencies=[Depends(require_scope(INVITATION_ADMIN))])
async def revoke_invitation(
    invitation_id: str,
    body: ReasonIn,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    result = await repo.revoke_invitation(
        principal.tenant_id, invitation_id, reason=body.reason, actor_id=principal.identity_id
    )
    if result is None:
        raise _not_found("INVITATION_NOT_FOUND", "pending invitation not found", invitation_id=invitation_id)
    return result


# ---------------------------------------------------------------------- tenant settings ----
@router.get("/admin/tenant-settings", dependencies=[Depends(require_scope(TENANT_ADMIN))])
async def list_tenant_settings(
    repo: RepoDep,
    principal: PrincipalDep,
    namespace: str | None = None,
) -> dict[str, Any]:
    if namespace and namespace not in ALLOWED_SETTING_NAMESPACES:
        raise _unprocessable("UNKNOWN_SETTING_NAMESPACE", "unknown tenant setting namespace", namespace=namespace)
    return {"items": await repo.list_tenant_settings(principal.tenant_id, namespace=namespace)}


def _validate_setting(namespace: str, key: str, value: Any, value_type: str) -> None:
    if namespace not in ALLOWED_SETTING_NAMESPACES:
        raise _unprocessable("UNKNOWN_SETTING_NAMESPACE", "unknown tenant setting namespace", namespace=namespace)
    import re

    if not re.match(_SETTING_KEY_PATTERN, key):
        raise _unprocessable("INVALID_SETTING_KEY", "invalid tenant setting key", key=key)
    if namespace == "rbac" and key == _SOD_MODE_KEY and value not in {"HARD", "WARN"}:
        raise _unprocessable("SOD_POLICY_INVALID", "sod_enforcement must be HARD or WARN", value=value)
    if value_type not in {"JSON", "STRING", "NUMBER", "BOOLEAN"}:
        raise _unprocessable("INVALID_VALUE_TYPE", "invalid setting value_type", value_type=value_type)


@router.put("/admin/tenant-settings/{namespace}/{key}", dependencies=[Depends(require_scope(TENANT_ADMIN))])
@router.patch("/admin/tenant-settings/{namespace}/{key}", dependencies=[Depends(require_scope(TENANT_ADMIN))])
async def put_tenant_setting(
    namespace: str,
    key: str,
    body: TenantSettingIn,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    _validate_setting(namespace, key, body.value, body.value_type)
    setting = await repo.set_tenant_setting(
        principal.tenant_id, namespace=namespace, key=key, value=body.value,
        value_type=body.value_type, actor_id=principal.identity_id,
    )
    if namespace == "rbac" and key == _SOD_MODE_KEY:
        await repo.audit(
            tenant_id=principal.tenant_id, actor_id=principal.identity_id, action="sod_policy.updated",
            resource_type="tenant_setting", resource_id=f"{namespace}.{key}", diff={"value": body.value},
        )
    return setting


@router.patch("/admin/tenant-settings", dependencies=[Depends(require_scope(TENANT_ADMIN))])
async def patch_tenant_settings(
    body: TenantSettingsPatch,
    repo: RepoDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    updated = []
    for entry in body.settings:
        namespace = str(entry.get("namespace", "")).strip()
        key = str(entry.get("key", "")).strip()
        value = entry.get("value")
        value_type = str(entry.get("value_type", "JSON"))
        _validate_setting(namespace, key, value, value_type)
        updated.append(
            await repo.set_tenant_setting(
                principal.tenant_id, namespace=namespace, key=key, value=value,
                value_type=value_type, actor_id=principal.identity_id,
            )
        )
    return {"items": updated}


__all__ = ["router"]
