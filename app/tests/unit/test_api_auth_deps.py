"""FS-00 API auth dependency primitives: bearer validation and deny-by-default scope guard."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.api.deps import Principal, require_scope, token_service
from app.api.main import create_app

SCOPE = "platform.audit.read"
Protected = Annotated[Principal, Depends(require_scope(SCOPE))]


def _client() -> TestClient:
    app = create_app()
    router = APIRouter()

    @router.get("/protected")
    async def protected(principal: Protected) -> dict[str, str]:
        return {"tenant_id": principal.tenant_id}

    app.include_router(router)
    return TestClient(app)


def _token(*, scopes: tuple[str, ...] = (SCOPE,), tenant_id: str | None = None) -> str:
    token, _ = token_service().issue_access_token(
        identity_id=str(uuid.uuid4()),
        app_user_id=str(uuid.uuid4()),
        tenant_id=tenant_id or str(uuid.uuid4()),
        token_version=1,
        scopes=scopes,
    )
    return token


def test_missing_bearer_is_401_problem() -> None:
    response = _client().get("/protected")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "TOKEN_INVALID"


def test_invalid_bearer_is_401() -> None:
    response = _client().get("/protected", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"


def test_missing_scope_is_403() -> None:
    response = _client().get("/protected", headers={"Authorization": f"Bearer {_token(scopes=())}"})
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_SCOPE"


def test_valid_token_and_scope_allowed() -> None:
    tenant_id = str(uuid.uuid4())
    response = _client().get("/protected", headers={"Authorization": f"Bearer {_token(tenant_id=tenant_id)}"})
    assert response.status_code == 200
    assert response.json()["tenant_id"] == tenant_id


def test_token_is_tenant_bound() -> None:
    token = _token(tenant_id=str(uuid.uuid4()))
    claims = token_service().decode_access_token(token)
    assert claims.tid
    assert claims.aud == "inventory-platform-api"
