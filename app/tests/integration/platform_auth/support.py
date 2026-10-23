"""Shared DB seeding helpers for the FS-00 auth integration tests (test-only code)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import (
    app_user,
    permission_scope,
    role,
    role_permission,
    tenant,
    tenant_invitation,
    tenant_sso_connection,
    user_identity,
    user_role,
)
from app.core.security.passwords import PasswordPolicy, PasswordService

TEST_PASSWORD = "Correct-Horse-Battery-9!"
TEST_JWT_SECRET = "integration-test-jwt-secret-32-bytes-minimum"
CHEAP_POLICY = PasswordPolicy(time_cost=1, memory_cost_kib=8192, parallelism=1)


def cheap_password_service() -> PasswordService:
    return PasswordService(CHEAP_POLICY)


def hash_password(password: str = TEST_PASSWORD, *, policy: PasswordPolicy = CHEAP_POLICY) -> str:
    return PasswordService(policy).hash(password)


async def create_tenant(
    session: AsyncSession,
    *,
    code: str,
    name: str | None = None,
    status: str = "ACTIVE",
) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,  # tenant row is its own tenant scope (DATA-MODEL §3.1)
            code=code,
            name=name or f"Tenant {code}",
            status=status,
            base_currency_code="USD",
        )
    )
    return tenant_id


async def create_identity(
    session: AsyncSession,
    *,
    email: str,
    password: str = TEST_PASSWORD,
    status: str = "ACTIVE",
    is_platform_admin: bool = False,
    password_hash: str | None = None,
) -> uuid.UUID:
    identity_id = uuid.uuid4()
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=email.lower(),
            display_name=email.split("@", 1)[0].title(),
            password_hash=password_hash if password_hash is not None else hash_password(password),
            status=status,
            is_platform_admin=is_platform_admin,
        )
    )
    return identity_id


async def create_membership(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity_id: uuid.UUID,
    email: str,
    status: str = "ACTIVE",
    membership_status: str = "ACTIVE",
    token_version: int = 0,
    role_code: str = "admin",
    scopes: tuple[str, ...] = ("platform.admin", "items.read", "items.create"),
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> uuid.UUID:
    app_user_id = uuid.uuid4()
    await session.execute(
        insert(app_user).values(
            id=app_user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=email.lower(),
            display_name=email.split("@", 1)[0].title(),
            status=status,
            membership_status=membership_status,
            token_version=token_version,
            joined_at=datetime.now(UTC),
        )
    )
    role_id = uuid.uuid4()
    await session.execute(
        insert(role).values(
            id=role_id,
            tenant_id=tenant_id,
            code=role_code,
            name=role_code.replace("_", " ").title(),
            is_system=False,
            is_template=False,
            is_editable=True,
        )
    )
    for scope_name in scopes:
        domain, _, action = scope_name.partition(".")
        scope_id = uuid.uuid4()
        await session.execute(
            insert(permission_scope).values(
                id=scope_id,
                tenant_id=tenant_id,
                scope=scope_name,
                domain=domain or scope_name,
                action=action or "read",
                is_assignable=True,
            )
        )
        await session.execute(
            insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id)
        )
    start = effective_from or datetime.now(UTC) - timedelta(hours=1)
    await session.execute(
        insert(user_role).values(
            tenant_id=tenant_id,
            user_id=app_user_id,
            role_id=role_id,
            effective_from=start,
            effective_to=effective_to,
        )
    )
    return app_user_id


async def create_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    code: str,
    scopes: tuple[str, ...] = ("items.read",),
) -> uuid.UUID:
    role_id = uuid.uuid4()
    await session.execute(
        insert(role).values(
            id=role_id,
            tenant_id=tenant_id,
            code=code,
            name=code.replace("_", " ").title(),
            is_system=False,
            is_template=False,
            is_editable=True,
        )
    )
    for scope_name in scopes:
        domain, _, action = scope_name.partition(".")
        existing = (
            await session.execute(
                select(permission_scope.c.id).where(
                    permission_scope.c.tenant_id == tenant_id,
                    permission_scope.c.scope == scope_name,
                )
            )
        ).scalar_one_or_none()
        scope_id = existing or uuid.uuid4()
        if existing is None:
            await session.execute(
                insert(permission_scope).values(
                    id=scope_id,
                    tenant_id=tenant_id,
                    scope=scope_name,
                    domain=domain or scope_name,
                    action=action or "read",
                    is_assignable=True,
                )
            )
        await session.execute(
            insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id)
        )
    return role_id


async def create_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    invited_by: uuid.UUID,
    role_ids: tuple[uuid.UUID, ...] = (),
    raw_token: str = "invitation-raw-token-123456",
    status: str = "PENDING",
    expires_in_hours: int = 48,
) -> uuid.UUID:
    invitation_id = uuid.uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=expires_in_hours)
    created_at = min(now, expires_at - timedelta(hours=1))
    await session.execute(
        insert(tenant_invitation).values(
            id=invitation_id,
            tenant_id=tenant_id,
            email=email.lower(),
            intended_role_ids=list(role_ids),
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            status=status,
            invited_by=invited_by,
            expires_at=expires_at,
            created_at=created_at,
        )
    )
    return invitation_id


async def create_sso_connection(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str = "Company IdP",
    protocol: str = "OIDC",
    status: str = "ACTIVE",
    issuer: str = "https://sso-example.auth.example.com",
    email_domains: tuple[str, ...] = ("example.com",),
    is_enforced: bool = False,
) -> uuid.UUID:
    connection_id = uuid.uuid4()
    await session.execute(
        insert(tenant_sso_connection).values(
            id=connection_id,
            tenant_id=tenant_id,
            name=name,
            protocol=protocol,
            status=status,
            dex_issuer_url=issuer,
            client_id="inventory-platform",
            email_domains=list(email_domains),
            is_enforced=is_enforced,
            jit_provisioning=False,
        )
    )
    return connection_id


def auth_headers(access_token: str) -> dict[str, Any]:
    return {"Authorization": f"Bearer {access_token}"}


__all__ = [
    "CHEAP_POLICY",
    "TEST_JWT_SECRET",
    "TEST_PASSWORD",
    "auth_headers",
    "cheap_password_service",
    "create_identity",
    "create_invitation",
    "create_membership",
    "create_role",
    "create_sso_connection",
    "create_tenant",
    "hash_password",
]
