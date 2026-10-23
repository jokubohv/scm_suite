"""Fixtures for FS-00 auth endpoint integration tests (live Postgres via root conftest DB fixtures).

The API app is built locally with only the auth router (``app/api/main.py``/``registry.py`` are
integrator-owned); DB/tokens/passwords are dependency-overridden to the test fixtures:
``get_db`` → the root ``db_session`` (rollback isolation), ``token_service`` → test TokenService,
``get_password_service`` → cheap Argon2 parameters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI, Header

from app.api.deps import Principal, get_current_principal, get_db, scope_registry, token_service
from app.api.errors import install_error_handlers
from app.api.routers.platform.auth import get_password_service, router
from app.core.security.errors import TokenInvalid
from app.core.security.scopes import ScopeRegistry
from app.core.security.tokens import HmacTokenSigner, TokenConfig, TokenService
from app.tests.integration.platform_auth.support import TEST_JWT_SECRET, cheap_password_service


@pytest.fixture()
def tokens() -> TokenService:
    return TokenService(
        HmacTokenSigner(TEST_JWT_SECRET),
        config=TokenConfig(
            issuer="inventory-platform",
            audience="inventory-platform-api",
        ),
    )


@pytest.fixture()
def auth_app(db_session: object, tokens: TokenService) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def override_db() -> AsyncIterator[object]:
        yield db_session

    async def override_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        # Decode with the test TokenService (deps.get_access_claims calls the cached factory directly,
        # so overriding ``token_service`` alone is not enough for the principal dependency chain).
        if not authorization or not authorization.lower().startswith("bearer "):
            raise TokenInvalid("bearer token required")
        claims = tokens.decode_access_token(authorization.split(" ", 1)[1].strip())
        return Principal(
            identity_id=claims.sub,
            app_user_id=claims.uid,
            tenant_id=claims.tid,
            token_version=claims.tv,
            scopes=frozenset(claims.scopes),
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[token_service] = lambda: tokens
    app.dependency_overrides[get_current_principal] = override_principal
    app.dependency_overrides[get_password_service] = cheap_password_service
    app.dependency_overrides[scope_registry] = lambda: ScopeRegistry()
    return app


@pytest.fixture()
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
