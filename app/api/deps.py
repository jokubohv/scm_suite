"""Shared FastAPI dependencies: DB session, bearer/API-key authentication, capability scopes.

Full tenant/membership resolution is layered on top of these primitives by the FS-00 auth/rbac
services; this module stays DB-thin so routers compose it explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security.errors import TokenInvalid
from app.core.security.scopes import EffectivePermissions, ScopeRegistry
from app.core.security.tokens import (
    AccessTokenClaims,
    HmacTokenSigner,
    TokenConfig,
    TokenService,
)
from app.db.session import session_scope


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated platform principal derived from a validated access token."""

    identity_id: str
    app_user_id: str
    tenant_id: str
    token_version: int
    scopes: frozenset[str]

    def permissions(self) -> EffectivePermissions:
        return EffectivePermissions(scopes=self.scopes)


@lru_cache(maxsize=1)
def _settings() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def token_service() -> TokenService:
    settings = _settings()
    return TokenService(
        HmacTokenSigner(settings.jwt_secret),
        config=TokenConfig(issuer=settings.jwt_issuer, audience=settings.jwt_audience),
    )


@lru_cache(maxsize=1)
def scope_registry() -> ScopeRegistry:
    return ScopeRegistry()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def get_bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise TokenInvalid("bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise TokenInvalid("bearer token required")
    return token


async def get_access_claims(
    token: Annotated[str, Depends(get_bearer_token)],
) -> AccessTokenClaims:
    return token_service().decode_access_token(token)


async def get_current_principal(
    claims: Annotated[AccessTokenClaims, Depends(get_access_claims)],
) -> Principal:
    return Principal(
        identity_id=claims.sub,
        app_user_id=claims.uid,
        tenant_id=claims.tid,
        token_version=claims.tv,
        scopes=frozenset(claims.scopes),
    )


def require_scope(scope: str):
    """Dependency factory: require one capability scope (deny-by-default)."""

    async def _dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        scope_registry().require_scope(principal.permissions(), scope)
        return principal

    return _dependency


SettingsDep = Annotated[Settings, Depends(_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
PrincipalDep = Annotated[Principal, Depends(get_current_principal)]
