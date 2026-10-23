"""Fixtures for the FS-00 finance-port (journal-intent read API) integration tests.

Uses the shared harness ``db_session`` fixture (outer-transaction rollback; graceful skip when no
database is available) and the real platform token service, so tenant scoping and the
``platform.finance.read`` capability scope are exercised end-to-end without touching app source.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.api.deps import get_db, token_service
from app.api.main import create_app
from app.core.models import platform as p

FINANCE_READ_SCOPES = frozenset({"platform.finance.read"})
JOURNAL = "/api/v1/finance/journal-intents"


@pytest.fixture()
def finance_app(db_session: Any) -> Any:
    app = create_app()

    async def _override_db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture()
async def finance_client(finance_app: Any, requires_db: Any) -> Any:
    transport = ASGITransport(app=finance_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def bearer_headers(
    *,
    tenant_id: UUID,
    identity_id: UUID,
    user_id: UUID,
    scopes: frozenset[str],
) -> dict[str, str]:
    token, _claims = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


async def seed_tenant(session: Any, *, code: str | None = None) -> dict[str, str]:
    """Insert an ACTIVE tenant and return ids plus a token scoped for the finance read API."""
    tenant_id, identity_id, user_id = uuid4(), uuid4(), uuid4()
    tenant_code = code or f"fin-{tenant_id.hex[:8]}"
    await session.execute(
        insert(p.tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=tenant_code,
            name=f"{tenant_code} Inc",
            base_currency_code="USD",
            timezone="UTC",
            status="ACTIVE",
        )
    )
    return {
        "tenant_id": str(tenant_id),
        "identity_id": str(identity_id),
        "app_user_id": str(user_id),
        "code": tenant_code,
        "headers": bearer_headers(
            tenant_id=tenant_id, identity_id=identity_id, user_id=user_id, scopes=FINANCE_READ_SCOPES
        ),
    }


def scoped_headers(tenant: dict[str, str], scopes: frozenset[str]) -> dict[str, str]:
    return bearer_headers(
        tenant_id=UUID(tenant["tenant_id"]),
        identity_id=UUID(tenant["identity_id"]),
        user_id=UUID(tenant["app_user_id"]),
        scopes=scopes,
    )
