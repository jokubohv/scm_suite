"""Fixtures for Δ-SSO integration tests: FastAPI app with the SSO router, token principals,
a mocked Dex HTTP client (no live network) and an in-memory single-use state store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.api.deps import get_db, token_service
from app.api.main import create_app
from app.api.routers.platform.sso import InMemorySsoStateStore
from app.api.routers.platform.sso import router as sso_router
from app.core.models.platform import app_user, permission_scope, role, role_permission, tenant, user_identity

ISSUER = "http://localhost:5556/dex"
CLIENT_ID = "inventory-platform"
REDIRECT_URI = "http://localhost:8000/api/v1/auth/sso/callback"


class FakeDex:
    """Deterministic Dex stand-in: token exchange + ID-token verification + health."""

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


@pytest.fixture()
def sso_app(db_session: Any):
    app = create_app()
    app.include_router(sso_router)

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.state.sso_state_store = InMemorySsoStateStore()
    app.state.dex_client = FakeDex()
    return app


@pytest.fixture()
async def sso_client(sso_app: Any, requires_db: Any):
    transport = ASGITransport(app=sso_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _token(identity_id: str, app_user_id: str, tenant_id: str, scopes: set[str]) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=identity_id,
        app_user_id=app_user_id,
        tenant_id=tenant_id,
        token_version=0,
        scopes=frozenset(scopes),
    )
    return token


async def seed_tenant(
    session: Any,
    *,
    code: str,
    scopes: set[str] | None = None,
    roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a tenant + admin identity/membership + optional roles/scopes; return ids/token."""
    tenant_id, identity_id, user_id = uuid4(), uuid4(), uuid4()
    await session.execute(insert(tenant).values(id=tenant_id, code=code, name=f"{code} Inc", status="ACTIVE"))
    await session.execute(
        insert(user_identity).values(
            id=identity_id,
            email=f"admin@{code}.example",
            status="ACTIVE",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            version=1,
        )
    )
    await session.execute(
        insert(app_user).values(
            id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=f"admin@{code}.example",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
            joined_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            version=1,
        )
    )
    role_ids: dict[str, Any] = {}
    for entry in roles or []:
        role_id = uuid4()
        await session.execute(
            insert(role).values(
                id=role_id,
                tenant_id=tenant_id,
                code=entry["code"],
                name=entry["code"],
                is_system=False,
                is_template=False,
                is_editable=True,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                version=1,
            )
        )
        for scope in entry.get("scopes", []):
            domain, action = scope.split(".")[0], scope.split(".")[-1]
            scope_id = uuid4()
            await session.execute(
                insert(permission_scope).values(
                    id=scope_id,
                    tenant_id=tenant_id,
                    scope=scope,
                    domain=domain,
                    action=action,
                    is_assignable=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    version=1,
                )
            )
            await session.execute(
                insert(role_permission).values(tenant_id=tenant_id, role_id=role_id, scope_id=scope_id)
            )
        role_ids[entry["code"]] = role_id
    token_scopes = scopes if scopes is not None else {"platform.sso.manage", "platform.sso.read"}
    return {
        "tenant_id": str(tenant_id),
        "identity_id": str(identity_id),
        "app_user_id": str(user_id),
        "roles": role_ids,
        "headers": {"Authorization": f"Bearer {_token(str(identity_id), str(user_id), str(tenant_id), token_scopes)}"},
    }


def connection_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Acme IdP",
        "protocol": "OIDC",
        "dex_issuer_url": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret_ref": "env:DEX_CLIENT_SECRET",
        "email_domains": ["example.com"],
        "jit_provisioning": True,
    }
    payload.update(overrides)
    return payload


async def create_connection(client: AsyncClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/admin/sso-connections", json=connection_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def dex_claims(
    *, nonce: str, email: str = "dev@example.com", groups: list[str] | None = None, issuer: str = ISSUER
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "iss": issuer,
        "aud": CLIENT_ID,
        "sub": "dev-user-0001",
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 300,
        "email": email,
        "email_verified": True,
        "groups": groups or [],
    }
