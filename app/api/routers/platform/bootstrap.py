"""Operator-only bootstrap API for tenants and the first accounts.

This is the *only* account-creation path in a hardened deployment: every route requires the
``SUPER_ADMIN_TOKEN`` configured in the environment. It intentionally bypasses normal tenant-admin
RBAC because it runs before any tenant administrator exists; it then provisions the tenant RBAC
catalogue/roles and creates the requested memberships atomically.

There is no public sign-up/registration endpoint. Public account creation is limited to accepting a
single-use invitation token (``POST /api/v1/admin/invitations/{token}/accept``), which cannot create an
account without a valid invitation issued by an authorised path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.api.deps import DbDep, require_super_admin
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import PlatformObject
from app.core.config import get_settings
from app.core.models import platform as p
from app.core.platform_events import emit_platform_event
from app.core.repositories.platform_admin import PlatformAdminRepository
from app.core.security.passwords import InvalidCredentials, PasswordService
from app.seed.permissions import load_permissions

router = APIRouter(prefix="/api/v1/bootstrap", responses=PROBLEM_RESPONSES, tags=["platform-bootstrap"], route_class=SCMAPIRoute)
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class TenantBootstrapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "UTC"
    base_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    negative_inventory_policy: Literal["BLOCK", "WARN", "ALLOW"] = "BLOCK"
    rounding_mode: Literal["PER_MOVEMENT", "PER_TRANSACTION", "PER_DOCUMENT"] = "PER_MOVEMENT"
    retention_ledger_years: int = Field(default=7, ge=1, le=50)
    admin_email: str = Field(min_length=3, max_length=320)
    admin_display_name: str | None = Field(default=None, max_length=200)
    admin_password: str = Field(min_length=12, max_length=128)
    admin_role_codes: list[str] = Field(default_factory=lambda: ["admin"], max_length=20)


class UserBootstrapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    password: str = Field(min_length=12, max_length=128)
    role_codes: list[str] = Field(default_factory=lambda: ["admin"], max_length=20)


class BootstrapWhoAmI(BaseModel):
    enabled: bool
    account_creation_super_admin_only: bool
    super_admin_token_min_length: int


def _http(code: str, message: str, status: int = 422, **details: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "details": details})


def _normalise_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise _http("EMAIL_INVALID", "invalid email address")
    return email


async def _provision_tenant_rbac(session: Any, tenant_id: UUID) -> dict[str, int]:
    """Idempotently seed the permission catalogue, role templates and role→scope links."""
    catalogue = load_permissions()
    existing_scopes = {
        row[0]
        for row in (await session.execute(
            select(p.permission_scope.c.scope).where(p.permission_scope.c.tenant_id == tenant_id)
        )).all()
    }
    scope_ids: dict[str, UUID] = {}
    inserted_scopes = 0
    for row in catalogue.permission_rows():
        scope = row["scope"]
        if scope not in existing_scopes:
            scope_id = uuid.uuid4()
            await session.execute(
                p.permission_scope.insert().values(
                    id=scope_id,
                    tenant_id=tenant_id,
                    scope=scope,
                    domain=row["domain"],
                    action=row["action"],
                    description=f"{row['domain']}.{row['action']}",
                    requires_approval=bool(row["requires_approval"]),
                    is_platform_only=bool(row["platform_only"]),
                    is_assignable=bool(row["assignable"]),
                    owner_feature_set=row.get("owner_feature_set"),
                )
            )
            scope_ids[scope] = scope_id
            inserted_scopes += 1
    if not scope_ids:
        scope_ids = {
            row[0]: row[1]
            for row in (await session.execute(
                select(p.permission_scope.c.scope, p.permission_scope.c.id).where(
                    p.permission_scope.c.tenant_id == tenant_id
                )
            )).all()
        }
    existing_roles = {
        row[0]: row[1]
        for row in (await session.execute(
            select(p.role.c.code, p.role.c.id).where(p.role.c.tenant_id == tenant_id)
        )).all()
    }
    role_ids: dict[str, UUID] = dict(existing_roles)
    inserted_roles = 0
    for row in catalogue.role_rows():
        code = row["code"]
        if code in role_ids:
            continue
        role_id = uuid.uuid4()
        await session.execute(
            p.role.insert().values(
                id=role_id,
                tenant_id=tenant_id,
                code=code,
                name=row["name"],
                description=row.get("description"),
                is_system=bool(row["is_system"]),
                is_template=True,
                is_editable=False,
            )
        )
        role_ids[code] = role_id
        inserted_roles += 1
    existing_links = {
        (row[0], row[1])
        for row in (await session.execute(
            select(p.role_permission.c.role_id, p.role_permission.c.scope_id).where(
                p.role_permission.c.tenant_id == tenant_id
            )
        )).all()
    }
    inserted_links = 0
    for row in catalogue.role_permission_rows():
        role_id = role_ids.get(row["role_code"])
        scope_id = scope_ids.get(row["scope"])
        if role_id is None or scope_id is None or (role_id, scope_id) in existing_links:
            continue
        await session.execute(
            p.role_permission.insert().values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id)
        )
        inserted_links += 1
    return {"scopes": inserted_scopes, "roles": inserted_roles, "role_permissions": inserted_links}


async def _create_account(
    session: Any,
    *,
    tenant_id: UUID,
    email: str,
    display_name: str | None,
    password: str,
    role_codes: list[str],
) -> dict[str, Any]:
    normalised = _normalise_email(email)
    role_rows = list((await session.execute(
        select(p.role).where(
            p.role.c.tenant_id == tenant_id,
            p.role.c.code.in_(role_codes),
            p.role.c.inactive_at.is_(None),
        )
    )).mappings().all())
    found_codes = {str(row["code"]) for row in role_rows}
    missing = sorted(set(role_codes) - found_codes)
    if missing:
        raise _http("ROLE_NOT_FOUND", "one or more roles do not exist in this tenant", role_codes=missing)

    password_hash = PasswordService().hash(password)  # raises InvalidCredentials when policy fails
    identity = (await session.execute(
        select(p.user_identity).where(p.user_identity.c.email == normalised)
    )).mappings().first()
    if identity is None:
        identity_id = uuid.uuid4()
        await session.execute(
            p.user_identity.insert().values(
                id=identity_id,
                email=normalised,
                display_name=display_name,
                password_hash=password_hash,
                status="ACTIVE",
                is_platform_admin=False,
                mfa_enabled=False,
            )
        )
    else:
        identity_id = identity["id"]
        if not identity.get("password_hash"):
            await session.execute(
                p.user_identity.update()
                .where(p.user_identity.c.id == identity_id)
                .values(password_hash=password_hash, updated_at=datetime.now(UTC))
            )
    existing_membership = (await session.execute(
        select(p.app_user.c.id).where(
            p.app_user.c.tenant_id == tenant_id,
            p.app_user.c.identity_id == identity_id,
        )
    )).scalar_one_or_none()
    if existing_membership is not None:
        raise _http("MEMBERSHIP_EXISTS", "identity already has a membership in this tenant", status=409,
                    app_user_id=str(existing_membership))
    now = datetime.now(UTC)
    app_user_id = uuid.uuid4()
    await session.execute(
        p.app_user.insert().values(
            id=app_user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=normalised,
            display_name=display_name,
            status="ACTIVE",
            membership_status="ACTIVE",
            joined_at=now,
            token_version=0,
            is_service_account=False,
        )
    )
    for role in role_rows:
        await session.execute(
            p.user_role.insert().values(
                tenant_id=tenant_id,
                user_id=app_user_id,
                role_id=role["id"],
                effective_from=now,
            )
        )
    await emit_platform_event(
        session,
        "membership.activated",
        tenant_id=str(tenant_id),
        aggregate_type="app_user",
        aggregate_id=str(app_user_id),
        payload={
            "tenant_id": str(tenant_id),
            "app_user_id": str(app_user_id),
            "identity_id": str(identity_id),
            "joined_at": now.isoformat(),
            "role_ids": [str(role["id"]) for role in role_rows],
            "role_codes": sorted(found_codes),
        },
    )
    return {
        "app_user_id": str(app_user_id),
        "identity_id": str(identity_id),
        "email": normalised,
        "role_codes": sorted(found_codes),
    }


@router.get("/whoami", response_model=BootstrapWhoAmI, dependencies=[Depends(require_super_admin)])
async def whoami() -> BootstrapWhoAmI:
    settings = get_settings()
    return BootstrapWhoAmI(
        enabled=bool(settings.super_admin_token),
        account_creation_super_admin_only=bool(settings.account_creation_super_admin_only),
        super_admin_token_min_length=len(settings.super_admin_token),
    )


@router.post("/tenants", response_model=PlatformObject, status_code=201, dependencies=[Depends(require_super_admin)])
async def bootstrap_tenant(body: TenantBootstrapIn, db: DbDep) -> dict[str, Any]:
    repo = PlatformAdminRepository(db)
    payload = {
        "code": body.code,
        "name": body.name,
        "status": "ACTIVE",
        "timezone": body.timezone,
        "base_currency_code": body.base_currency_code,
        "negative_inventory_policy": body.negative_inventory_policy,
        "rounding_mode": body.rounding_mode,
        "retention_ledger_years": body.retention_ledger_years,
        "settings": {},
    }
    try:
        tenant = await repo.create_tenant(data=payload, actor_identity_id=None)
    except Exception as exc:  # typed platform-admin errors carry a code/status
        code = getattr(exc, "code", "TENANT_CREATE_FAILED")
        status = int(getattr(exc, "http_status", 400))
        raise HTTPException(status_code=status, detail={"code": code, "message": str(exc), "details": {}}) from exc
    tenant_id = UUID(str(tenant["id"]))
    try:
        rbac = await _provision_tenant_rbac(db, tenant_id)
        admin = await _create_account(
            db,
            tenant_id=tenant_id,
            email=body.admin_email,
            display_name=body.admin_display_name,
            password=body.admin_password,
            role_codes=body.admin_role_codes,
        )
    except InvalidCredentials as exc:
        raise _http("PASSWORD_POLICY_INVALID", "admin password does not meet the password policy") from exc
    return {
        "tenant_id": str(tenant_id),
        "code": tenant["code"],
        "status": tenant["status"],
        "rbac": rbac,
        "admin": admin,
    }


@router.post("/users", response_model=PlatformObject, status_code=201, dependencies=[Depends(require_super_admin)])
async def bootstrap_user(body: UserBootstrapIn, db: DbDep) -> dict[str, Any]:
    await db.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(body.tenant_id)})
    tenant = (await db.execute(
        select(p.tenant.c.id, p.tenant.c.status).where(p.tenant.c.id == body.tenant_id)
    )).mappings().first()
    if tenant is None:
        raise _http("TENANT_NOT_FOUND", "tenant not found", status=404)
    if tenant["status"] != "ACTIVE":
        raise _http("TENANT_NOT_ACTIVE", "tenant is not active", status=409)
    rbac = await _provision_tenant_rbac(db, body.tenant_id)
    try:
        user = await _create_account(
            db,
            tenant_id=body.tenant_id,
            email=body.email,
            display_name=body.display_name,
            password=body.password,
            role_codes=body.role_codes,
        )
    except InvalidCredentials as exc:
        raise _http("PASSWORD_POLICY_INVALID", "password does not meet the password policy") from exc
    return {"tenant_id": str(body.tenant_id), "rbac": rbac, "user": user}


__all__ = ["router"]
