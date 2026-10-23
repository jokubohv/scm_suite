"""Fixtures for FS-00 artifact API integration tests (task-44).

Uses the shared harness ``db_session`` fixture (outer-transaction rollback; graceful skip when no
database is available) and mounts only the router under test on top of ``create_app()`` — no edits to
``app/api/main.py``/``registry.py``/``deps.py``. Authentication uses the real platform token service so
tenant scoping and capability scopes are exercised end-to-end.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.api.deps import get_db, token_service
from app.api.main import create_app
from app.api.routers.platform.artifacts import get_upload_signer
from app.api.routers.platform.artifacts import router as artifacts_router
from app.core.models import platform as p
from app.core.repositories.artifacts import PresignedUpload

ATTACHMENT_SCOPES = frozenset({"platform.attachment.manage"})
DOCUMENT_LINK_SCOPES = frozenset({"platform.documentlink.read"})
FINANCE_SCOPES = frozenset({"platform.finance.read"})


class FakeUploadSigner:
    """Deterministic object-storage adapter for tests (no network/S3)."""

    provider = "fake-s3"

    def sign_upload(
        self,
        *,
        tenant_id: UUID,
        storage_key: str,
        content_type: str | None,
        size_bytes: int,
        expires_in_seconds: int = 900,
    ) -> PresignedUpload | None:
        return PresignedUpload(
            provider=self.provider,
            storage_key=storage_key,
            url=f"https://fake-s3.example/{storage_key}?sig=test",
            headers={"Content-Type": content_type or "application/octet-stream"},
            expires_in_seconds=expires_in_seconds,
        )

    def sign_download(self, *, tenant_id: UUID, storage_key: str, expires_in_seconds: int = 900) -> str | None:
        return f"https://fake-s3.example/{storage_key}?download=test"


@pytest.fixture()
def upload_signer() -> FakeUploadSigner:
    return FakeUploadSigner()


@pytest.fixture()
def artifacts_app(db_session: Any, upload_signer: FakeUploadSigner) -> Any:
    app = create_app()
    # ``app/api/main.py`` may already register the artifacts router (integrator-owned wiring);
    # only mount it here when it is not registered yet so the OpenAPI schema has no duplicates.
    already_mounted = any(
        getattr(route, "path", "").startswith("/api/v1/attachments") for route in app.routes
    )
    if not already_mounted:
        app.include_router(artifacts_router)

    async def _override_db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_upload_signer] = lambda: upload_signer
    return app


@pytest.fixture()
async def artifacts_client(artifacts_app: Any, requires_db: Any) -> Any:
    transport = ASGITransport(app=artifacts_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def seed_tenant(session: Any, *, code: str | None = None) -> dict[str, str]:
    """Insert an ACTIVE tenant; return ids + tenant-scoped tokens for the artifact scopes."""
    tenant_id, identity_id, user_id = uuid4(), uuid4(), uuid4()
    tenant_code = code or f"t-{tenant_id.hex[:8]}"
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
        "headers": bearer_headers(tenant_id=tenant_id, identity_id=identity_id, user_id=user_id,
                                 scopes=ATTACHMENT_SCOPES | DOCUMENT_LINK_SCOPES | FINANCE_SCOPES),
    }


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


def scoped_headers(tenant: dict[str, str], scopes: frozenset[str]) -> dict[str, str]:
    return bearer_headers(
        tenant_id=UUID(tenant["tenant_id"]),
        identity_id=UUID(tenant["identity_id"]),
        user_id=UUID(tenant["app_user_id"]),
        scopes=scopes,
    )
