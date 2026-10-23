"""Fixtures for FS-00 RBAC/admin API integration tests (task-39).

Uses the shared harness ``db_session`` fixture (outer-transaction rollback; graceful skip without a
database). The test app mounts only the routers under test, overrides ``get_db`` with the shared
rollback session and ``get_current_principal`` with a seeded principal — no edits to conftest/main.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import rbac as rbac_router
from app.api.routers.platform import tenants as tenants_router
from app.core.models import platform as p
from app.core.security.scopes import ScopeRegistry

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
IDENTITY_ID = UUID("22222222-2222-2222-2222-222222222222")
APP_USER_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_USER_ID = UUID("44444444-4444-4444-4444-444444444444")
OTHER_IDENTITY_ID = UUID("55555555-5555-5555-5555-555555555555")
ADMIN_ROLE_ID = UUID("66666666-6666-6666-6666-666666666666")
READONLY_ROLE_ID = UUID("77777777-7777-7777-7777-777777777777")

ADMIN_SCOPES = frozenset(
    {
        "platform.role.manage", "platform.users.manage", "platform.invitations.manage",
        "platform.sessions.revoke", "platform.data_scope.manage", "platform.admin",
    }
)


def _catalogue() -> ScopeRegistry:
    return ScopeRegistry()


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    """Seed a tenant, two memberships and the permission/role rows the admin API needs."""
    registry = _catalogue()
    actor_scopes = ADMIN_SCOPES
    permission_entries = []
    for scope in registry.catalogue.scopes:
        if scope.scope in actor_scopes or scope.scope in {"platform.sso.manage", "platform.sso.read"}:
            permission_entries.append(
                {
                    "scope": scope.scope, "domain": scope.domain, "action": scope.action,
                    "requires_approval": scope.requires_approval, "platform_only": scope.platform_only,
                    "service_only": scope.service_only, "assignable": scope.assignable,
                    "owner_feature_set": scope.owner_feature_set,
                }
            )
    # Plus a few ordinary assignable scopes used by role CRUD tests.
    for scope_name in ("items.read", "items.update", "inventory.adjustment.post", "inventory.adjustment.approve"):
        entry = registry.catalogue.scopes_by_name[scope_name]
        permission_entries.append(
            {
                "scope": entry.scope, "domain": entry.domain, "action": entry.action,
                "requires_approval": entry.requires_approval, "platform_only": entry.platform_only,
                "service_only": entry.service_only, "assignable": entry.assignable,
                "owner_feature_set": entry.owner_feature_set,
            }
        )
    await db_session.execute(
        p.tenant.insert().values(
            id=TENANT_ID, tenant_id=TENANT_ID, code="ACME", name="Acme",
            base_currency_code="USD", timezone="UTC", status="ACTIVE",
            negative_inventory_policy="BLOCK", rounding_mode="PER_MOVEMENT",
        )
    )
    scope_rows = {}
    for entry in permission_entries:
        scope_id = uuid4()
        await db_session.execute(
            p.permission_scope.insert().values(
                id=scope_id, tenant_id=TENANT_ID, scope=entry["scope"], domain=entry["domain"],
                action=entry["action"], description="", requires_approval=entry["requires_approval"],
                is_platform_only=entry["platform_only"], is_assignable=entry["assignable"],
                owner_feature_set=entry["owner_feature_set"],
            )
        )
        scope_rows[entry["scope"]] = scope_id
    for identity_id, email, display, platform_admin in (
        (IDENTITY_ID, "admin@acme.test", "Acme Admin", True),
        (OTHER_IDENTITY_ID, "user@acme.test", "Acme User", False),
    ):
        await db_session.execute(
            p.user_identity.insert().values(
                id=identity_id, email=email, display_name=display, status="ACTIVE", is_platform_admin=platform_admin
            )
        )
    await db_session.execute(
        p.app_user.insert().values(
            id=APP_USER_ID, tenant_id=TENANT_ID, identity_id=IDENTITY_ID, email="admin@acme.test",
            display_name="Acme Admin", status="ACTIVE", membership_status="ACTIVE", token_version=0,
        )
    )
    await db_session.execute(
        p.app_user.insert().values(
            id=OTHER_USER_ID, tenant_id=TENANT_ID, identity_id=OTHER_IDENTITY_ID, email="user@acme.test",
            display_name="Acme User", status="ACTIVE", membership_status="ACTIVE", token_version=0,
        )
    )
    await db_session.execute(
        p.role.insert().values(
            id=ADMIN_ROLE_ID, tenant_id=TENANT_ID, code="admin", name="Tenant Administrator",
            is_system=False, is_template=False, is_editable=True,
        )
    )
    for scope_name in ("platform.role.manage", "platform.users.manage", "platform.invitations.manage",
                       "platform.sessions.revoke", "platform.data_scope.manage", "platform.admin"):
        await db_session.execute(
            p.role_permission.insert().values(tenant_id=TENANT_ID, role_id=ADMIN_ROLE_ID, scope_id=scope_rows[scope_name])
        )
    await db_session.execute(
        p.role.insert().values(
            id=READONLY_ROLE_ID, tenant_id=TENANT_ID, code="readonly", name="Read Only",
            is_system=False, is_template=False, is_editable=True,
        )
    )
    await db_session.execute(
        p.role_permission.insert().values(tenant_id=TENANT_ID, role_id=READONLY_ROLE_ID, scope_id=scope_rows["items.read"])
    )
    await db_session.execute(
        p.user_role.insert().values(tenant_id=TENANT_ID, user_id=APP_USER_ID, role_id=ADMIN_ROLE_ID)
    )
    await db_session.flush()
    return {"registry": registry, "scopes": scope_rows}


@pytest.fixture()
def principal() -> Principal:
    return Principal(
        identity_id=str(IDENTITY_ID), app_user_id=str(APP_USER_ID), tenant_id=str(TENANT_ID),
        token_version=0, scopes=ADMIN_SCOPES,
    )


@pytest.fixture()
async def app_client(db_session: Any, seeded: dict[str, Any], principal: Principal) -> AsyncIterator[Any]:
    import httpx

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(rbac_router.router)
    app.include_router(tenants_router.router)

    async def _get_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
def actor_scopes() -> frozenset[str]:
    return ADMIN_SCOPES
