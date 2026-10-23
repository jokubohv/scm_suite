"""Local seed/helper utilities for the FS-00 exit-gate integration/platform suites.

All helpers operate on the shared rollback-isolated ``db_session`` fixture; ids are random per test so
the suites are independent and repeatable. No app-source edits.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import token_service
from app.core.models.platform import (
    app_user,
    permission_scope,
    role,
    role_permission,
    tenant,
    tenant_invitation,
    user_identity,
    user_role,
)
from app.core.security.passwords import PasswordPolicy, PasswordService

TEST_PASSWORD = "Exit-Gate-Password-9!"
CHEAP_POLICY = PasswordPolicy(time_cost=1, memory_cost_kib=8192, parallelism=1)

ADMIN_SCOPES = frozenset(
    {
        "platform.role.manage",
        "platform.users.manage",
        "platform.invitations.manage",
        "platform.sessions.revoke",
        "platform.data_scope.manage",
        "platform.admin",
    }
)
SSO_SCOPES = frozenset({"platform.sso.manage", "platform.sso.read"})


def cheap_password_service() -> PasswordService:
    return PasswordService(CHEAP_POLICY)


def hash_password(password: str = TEST_PASSWORD) -> str:
    return PasswordService(CHEAP_POLICY).hash(password)


async def create_tenant(session: AsyncSession, *, code: str, status: str = "ACTIVE") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,  # tenant row is its own tenant scope (DATA-MODEL §3.1)
            code=code,
            name=f"Tenant {code}",
            status=status,
            base_currency_code="USD",
        )
    )
    return tenant_id


async def create_identity(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = TEST_PASSWORD,
    status: str = "ACTIVE",
    is_platform_admin: bool = False,
    display_name: str | None = None,
) -> uuid.UUID:
    identity_id = uuid.uuid4()
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=email.lower(),
            display_name=display_name or email.split("@", 1)[0].title(),
            password_hash=hash_password(password) if password is not None else None,
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
    is_service_account: bool = False,
    role_code: str = "admin",
    scopes: tuple[str, ...] = (),
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> dict[str, Any]:
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
            is_service_account=is_service_account,
        )
    )
    role_id: uuid.UUID | None = None
    if scopes:
        role_id = await create_role(
            session,
            tenant_id=tenant_id,
            code=role_code,
            scopes=scopes,
            effective_from=effective_from,
            effective_to=effective_to,
            user_id=app_user_id,
        )
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": app_user_id,
        "role_id": role_id,
    }


async def create_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    code: str,
    scopes: tuple[str, ...],
    is_template: bool = False,
    is_system: bool = False,
    is_editable: bool = True,
    user_id: uuid.UUID | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> uuid.UUID:
    role_id = uuid.uuid4()
    await session.execute(
        insert(role).values(
            id=role_id,
            tenant_id=tenant_id,
            code=code,
            name=code.replace("_", " ").title(),
            is_system=is_system,
            is_template=is_template,
            is_editable=is_editable,
        )
    )
    for scope_name in scopes:
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
            domain, _, action = scope_name.partition(".")
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
    if user_id is not None:
        await session.execute(
            insert(user_role).values(
                tenant_id=tenant_id,
                user_id=user_id,
                role_id=role_id,
                effective_from=effective_from or datetime.now(UTC) - timedelta(hours=1),
                effective_to=effective_to,
            )
        )
    return role_id


async def assign_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    await session.execute(
        insert(user_role).values(
            tenant_id=tenant_id,
            user_id=user_id,
            role_id=role_id,
            effective_from=datetime.now(UTC) - timedelta(hours=1),
        )
    )


async def create_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    invited_by: uuid.UUID,
    role_ids: tuple[uuid.UUID, ...] = (),
    raw_token: str,
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


def issue_access_token(
    *,
    identity_id: str | uuid.UUID,
    app_user_id: str | uuid.UUID,
    tenant_id: str | uuid.UUID,
    token_version: int = 0,
    scopes: set[str] | frozenset[str] | None = None,
) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=int(token_version),
        scopes=frozenset(scopes or ()),
    )
    return token


def auth_headers(
    *,
    identity_id: str | uuid.UUID,
    app_user_id: str | uuid.UUID,
    tenant_id: str | uuid.UUID,
    token_version: int = 0,
    scopes: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    token = issue_access_token(
        identity_id=identity_id,
        app_user_id=app_user_id,
        tenant_id=tenant_id,
        token_version=token_version,
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- SSO helpers ------
class FakeDex:
    """Deterministic mocked Dex client (no live network): token exchange, ID-token verify, health."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] = {}
        self.health_error: Exception | None = None
        self.exchanges: list[dict[str, Any]] = []

    async def exchange_code(self, **kwargs: Any) -> dict[str, Any]:
        self.exchanges.append(kwargs)
        return {"access_token": "dex-access", "id_token": "fake.id.token", "token_type": "Bearer"}

    async def verify_id_token(self, **_: Any) -> dict[str, Any]:
        return dict(self.claims)

    async def health_check(self, **_: Any) -> bool:
        if self.health_error is not None:
            raise self.health_error
        return True


SSO_ISSUER = "http://localhost:5556/dex"
SSO_CLIENT_ID = "inventory-platform"


def connection_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Acme IdP",
        "protocol": "OIDC",
        "dex_issuer_url": SSO_ISSUER,
        "client_id": SSO_CLIENT_ID,
        "client_secret_ref": "env:DEX_CLIENT_SECRET",
        "email_domains": ["example.com"],
        "jit_provisioning": True,
    }
    payload.update(overrides)
    return payload


def dex_claims(
    *,
    nonce: str,
    email: str = "dev@example.com",
    groups: list[str] | None = None,
    issuer: str = SSO_ISSUER,
    subject: str = "exit-gate-subject-0001",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "iss": issuer,
        "aud": SSO_CLIENT_ID,
        "sub": subject,
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 300,
        "email": email,
        "email_verified": True,
        "groups": groups or [],
    }


async def create_sso_connection(client: Any, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/admin/sso-connections", json=connection_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def activate_sso_connection(client: Any, headers: dict[str, str], connection_id: str) -> dict[str, Any]:
    tested = await client.post(f"/api/v1/admin/sso-connections/{connection_id}/test", headers=headers)
    assert tested.status_code == 200, tested.text
    return tested.json()


async def seed_admin_tenant(
    session: AsyncSession,
    *,
    code: str,
    email_domain: str | None = None,
    tenant_status: str = "ACTIVE",
    scopes: tuple[str, ...] = tuple(ADMIN_SCOPES),
    role_code: str = "admin",
    is_platform_admin: bool = True,
    membership_status: str = "ACTIVE",
    token_version: int = 0,
) -> dict[str, Any]:
    """Seed a tenant + admin identity/membership/role and return ids + bearer headers."""
    domain = email_domain or f"{code.lower()}.example"
    email = f"admin@{domain}"
    tenant_id = await create_tenant(session, code=code, status=tenant_status)
    identity_id = await create_identity(session, email=email, is_platform_admin=is_platform_admin)
    membership = await create_membership(
        session,
        tenant_id=tenant_id,
        identity_id=identity_id,
        email=email,
        membership_status=membership_status,
        token_version=token_version,
        role_code=role_code,
        scopes=scopes,
    )
    headers = auth_headers(
        identity_id=identity_id,
        app_user_id=membership["app_user_id"],
        tenant_id=tenant_id,
        token_version=token_version,
        scopes=set(scopes),
    )
    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "app_user_id": membership["app_user_id"],
        "role_id": membership["role_id"],
        "email": email,
        "headers": headers,
        "scopes": scopes,
    }


async def create_platform_scope_role(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    code: str,
    scope: str = "platform.tenant.manage",
) -> uuid.UUID:
    """Create a role holding a platform-only scope (for SSO group-mapping bound tests)."""
    role_id = uuid.uuid4()
    scope_id = uuid.uuid4()
    await session.execute(
        insert(permission_scope).values(
            id=scope_id,
            tenant_id=tenant_id,
            scope=scope,
            domain=scope.split(".")[0],
            action=scope.split(".")[-1],
            is_assignable=False,
            is_platform_only=True,
        )
    )
    await session.execute(
        insert(role).values(
            id=role_id,
            tenant_id=tenant_id,
            code=code,
            name=code.title(),
            is_system=False,
            is_template=False,
            is_editable=True,
        )
    )
    await session.execute(
        insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id)
    )
    return role_id
