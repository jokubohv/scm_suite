"""Shared FastAPI dependencies: DB session, bearer/API-key authentication, capability scopes.

Full tenant/membership resolution is layered on top of these primitives by the FS-00 auth/rbac
services; this module stays DB-thin so routers compose it explicitly.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import text
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
        config=TokenConfig(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
        ),
    )


@lru_cache(maxsize=1)
def scope_registry() -> ScopeRegistry:
    return ScopeRegistry()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def get_request_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Request DB session with tenant RLS context / auth-role routing.

    * Authenticated requests set ``app.tenant_id`` from the verified access-token claim in the same
      transaction as the handler (``SET LOCAL``), so RLS policies apply to the app login.
    * Public authentication endpoints run as the ``platform_auth`` service role (BYPASSRLS) because
      login must discover identities/memberships before a tenant context exists. The role is optional
      in dev (superuser test DB); in production provision-db-roles.sh creates it.
    """
    from app.core.security.errors import SecurityError

    async with session_scope() as session:
        path = request.url.path
        auth_path = path.startswith("/api/v1/auth") or (
            path.startswith("/api/v1/admin/invitations/") and path.endswith("/accept")
        )
        if auth_path:
            exists = await session.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'platform_auth'")
            )
            if exists:
                await session.execute(text("SET LOCAL ROLE platform_auth"))
        else:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                token = authorization.split(" ", 1)[1].strip()
                try:
                    claims = token_service().decode_access_token(token)
                except SecurityError:
                    pass
                else:
                    await session.execute(
                        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                        {"tenant_id": claims.tid},
                    )
        yield session


def _super_admin_value() -> str:
    """The configured bootstrap token, or empty when bootstrap is disabled."""
    value = get_settings().super_admin_token or ""
    return value.strip()


def _require_super_admin_token(supplied: str | None) -> None:
    expected = _super_admin_value()
    if not expected or len(expected) < 24:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOOTSTRAP_DISABLED", "message": "super-admin bootstrap is not configured", "details": {}},
        )
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail={"code": "SUPER_ADMIN_TOKEN_INVALID", "message": "valid super-admin token required", "details": {}},
        )


async def require_super_admin(
    x_super_admin_token: Annotated[str | None, Header(alias="X-Super-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI guard for operator-only bootstrap endpoints.

    Accepts ``X-Super-Admin-Token`` (preferred) or ``Authorization: Bearer <SUPER_ADMIN_TOKEN>``.
    The token is compared in constant time and never logged. Unset/short configuration disables the
    endpoints with 503 rather than falling back to an insecure default.
    """
    supplied = x_super_admin_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    _require_super_admin_token(supplied)


async def require_account_creation_authority(
    x_super_admin_token: Annotated[str | None, Header(alias="X-Super-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Gate ordinary account/tenant-creation routes in hardened deployments.

    With ``ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true`` the regular tenant-admin invitation/user routes
    and tenant provisioning require the operator token as well. In dev the setting defaults false so
    existing tenant-admin tests/flows stay usable.
    """
    if not get_settings().account_creation_super_admin_only:
        return
    supplied = x_super_admin_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    _require_super_admin_token(supplied)


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
DbDep = Annotated[AsyncSession, Depends(get_request_db)]
PrincipalDep = Annotated[Principal, Depends(get_current_principal)]
