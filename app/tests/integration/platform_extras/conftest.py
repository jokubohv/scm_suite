"""Fixtures for FS-00 platform-extras integration tests (task-54).

Mounts only the routers under test on a minimal FastAPI app, injects the shared rollback-isolated
``db_session`` and a mutable principal (so one test can exercise platform-admin and tenant-admin
callers). No edits to conftest/main/registry/deps; no live network or Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.platform import api_keys as api_keys_router
from app.api.routers.platform import extras as extras_router
from app.core.models import platform as p
from app.core.security.tenant_context import SYSTEM_TENANT_ID

SYSTEM_ID = UUID(str(SYSTEM_TENANT_ID))

PLATFORM_ADMIN_SCOPES = frozenset(
    {
        "platform.tenant.manage",
        "platform.audit.read",
        "platform.approval.definition.manage",
        "platform.sequence.manage",
        "platform.customfield.read",
        "platform.customfield.manage",
        "platform.data_scope.manage",
        "platform.admin",
    }
)
TENANT_ADMIN_SCOPES = frozenset(
    {
        "platform.customfield.read",
        "platform.customfield.manage",
        "platform.data_scope.manage",
        "platform.approval.definition.manage",
        "platform.sequence.manage",
        "platform.audit.read",
        "platform.admin",
    }
)


class PrincipalHolder:
    """Mutable current principal used by the overridden ``get_current_principal`` dependency."""

    def __init__(self) -> None:
        self.current: Principal | None = None

    def set(self, principal: Principal) -> Principal:
        self.current = principal
        return principal


@pytest.fixture()
def principal_holder() -> PrincipalHolder:
    return PrincipalHolder()


@pytest.fixture()
def extras_app(db_session: Any, principal_holder: PrincipalHolder) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(extras_router.router)
    app.include_router(api_keys_router.router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    def _override_principal() -> Principal:
        if principal_holder.current is None:  # pragma: no cover - tests always set it
            raise AssertionError("test must set principal_holder before calling the API")
        return principal_holder.current

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = _override_principal
    return app


@pytest.fixture()
async def extras_client(extras_app: FastAPI, requires_db: Any) -> AsyncIterator[Any]:
    import httpx

    transport = httpx.ASGITransport(app=extras_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------- seeding helpers --
def principal(
    tenant_id: str | UUID,
    *,
    scopes: frozenset[str] | set[str] = PLATFORM_ADMIN_SCOPES,
    identity_id: UUID | None = None,
    app_user_id: UUID | None = None,
) -> Principal:
    return Principal(
        identity_id=str(identity_id or uuid4()),
        app_user_id=str(app_user_id or uuid4()),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=frozenset(scopes),
    )


async def ensure_system_tenant(session: Any) -> None:
    existing = await session.execute(select(p.tenant.c.id).where(p.tenant.c.id == SYSTEM_ID))
    if existing.scalar_one_or_none() is None:
        await session.execute(
            insert(p.tenant).values(
                id=SYSTEM_ID,
                tenant_id=SYSTEM_ID,
                code="SYSTEM",
                name="System",
                base_currency_code="USD",
                timezone="UTC",
                status="ACTIVE",
                negative_inventory_policy="BLOCK",
                rounding_mode="PER_MOVEMENT",
                retention_ledger_years=7,
                settings={},
                version=1,
            )
        )


async def seed_tenant(session: Any, code: str | None = None, *, status: str = "ACTIVE") -> UUID:
    tenant_id = uuid4()
    await session.execute(
        insert(p.tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=(code or f"TEN{uuid4().hex[:8].upper()}"),
            name=f"{code or 'Tenant'} Inc",
            base_currency_code="USD",
            timezone="UTC",
            status=status,
            negative_inventory_policy="BLOCK",
            rounding_mode="PER_MOVEMENT",
            retention_ledger_years=7,
            settings={},
            version=1,
        )
    )
    return tenant_id


async def seed_membership(
    session: Any,
    tenant_id: UUID,
    *,
    email: str | None = None,
    is_platform_admin: bool = False,
) -> dict[str, Any]:
    identity_id, user_id = uuid4(), uuid4()
    address = email or f"user-{uuid4().hex[:10]}@example.test"
    now = datetime.now(UTC)
    await session.execute(
        insert(p.user_identity).values(
            id=identity_id,
            email=address,
            display_name="Test User",
            mfa_enabled=False,
            is_platform_admin=is_platform_admin,
            status="ACTIVE",
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    await session.execute(
        insert(p.app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=address,
            display_name="Test User",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
            joined_at=now,
            is_service_account=False,
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return {"identity_id": identity_id, "user_id": user_id, "email": address}


async def seed_role(session: Any, tenant_id: UUID, code: str = "admin") -> UUID:
    role_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.role).values(
            id=role_id,
            tenant_id=tenant_id,
            code=code,
            name=code.title(),
            is_system=True,
            is_template=True,
            is_editable=False,
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return role_id


async def seed_session(session: Any, tenant_id: UUID, membership: dict[str, Any]) -> UUID:
    session_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.user_session).values(
            id=session_id,
            tenant_id=tenant_id,
            app_user_id=membership["user_id"],
            identity_id=membership["identity_id"],
            refresh_token_hash=f"hash-{uuid4().hex}",
            status="ACTIVE",
            issued_at=now,
            expires_at=now + timedelta(days=7),
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return session_id


async def seed_custom_field_def(
    session: Any,
    tenant_id: UUID,
    *,
    entity_type: str = "ITEM",
    field_code: str,
    data_type: str = "TEXT",
    validation: dict[str, Any] | None = None,
) -> UUID:
    field_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.custom_field_def).values(
            id=field_id,
            tenant_id=tenant_id,
            entity_type=entity_type,
            field_code=field_code,
            label=field_code.title(),
            data_type=data_type,
            options=None,
            is_required=False,
            is_searchable=False,
            validation=validation or {},
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return field_id


async def seed_approval_definition(
    session: Any, tenant_id: UUID, *, name: str = "PO approval", priority: int = 100
) -> UUID:
    definition_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.approval_definition).values(
            id=definition_id,
            tenant_id=tenant_id,
            document_type="PURCHASE_ORDER",
            name=name,
            condition={"amount_total": {"gte": 100}},
            steps=[{"order": 1, "role": "admin"}],
            is_enabled=True,
            priority=priority,
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return definition_id


async def seed_number_sequence(session: Any, tenant_id: UUID, *, doc_type: str = "PO", next_value: int = 10) -> UUID:
    sequence_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.number_sequence).values(
            id=sequence_id,
            tenant_id=tenant_id,
            doc_type=doc_type,
            scope_key="",
            prefix="PO",
            padding=6,
            next_value=next_value,
            reset_policy="NEVER",
            gap_policy="NON_GAPLESS",
            period_key=None,
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return sequence_id


async def seed_api_key(session: Any, tenant_id: UUID, *, name: str = "ci-key") -> UUID:
    key_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        insert(p.api_key).values(
            id=key_id,
            tenant_id=tenant_id,
            name=name,
            key_prefix=f"key_{uuid4().hex[:12]}",
            secret_hash=f"hash-{uuid4().hex}",
            scopes=["inventory.items.read"],
            created_at=now,
            updated_at=now,
            version=1,
        )
    )
    return key_id


async def audited_actions(session: Any, tenant_id: UUID | str) -> list[str]:
    rows = await session.execute(
        select(p.audit_event.c.action).where(p.audit_event.c.tenant_id == UUID(str(tenant_id)))
    )
    return [str(row) for row in rows.scalars().all()]
