"""Shared FastAPI dependencies: DB session, bearer/API-key authentication, capability scopes.

Full tenant/membership resolution is layered on top of these primitives by the FS-00 auth/rbac
services; this module stays DB-thin so routers compose it explicitly.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.repositories.api_keys import derive_api_key_pepper
from app.core.security.errors import SecurityError, TokenInvalid
from app.core.security.scopes import EffectivePermissions, ScopeRegistry
from app.core.security.tokens import (
    AccessTokenClaims,
    ApiKeyHasher,
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


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Resolved authentication context for either a JWT or a tenant API key."""

    kind: str
    identity_id: str
    app_user_id: str
    tenant_id: str
    token_version: int
    scopes: frozenset[str]


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
    """FastAPI guard for operator-only bootstrap endpoints."""
    supplied = x_super_admin_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    _require_super_admin_token(supplied)


async def require_account_creation_authority(
    x_super_admin_token: Annotated[str | None, Header(alias="X-Super-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Gate ordinary account/tenant-creation routes in hardened deployments."""
    if not get_settings().account_creation_super_admin_only:
        return
    supplied = x_super_admin_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
    _require_super_admin_token(supplied)


_PUBLIC_AUTH_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/select-tenant",
    "/api/v1/auth/providers",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/sso/start",
    "/api/v1/auth/sso/callback",
})


def _is_public_auth_path(path: str) -> bool:
    return path in _PUBLIC_AUTH_PATHS or (
        path.startswith("/api/v1/admin/invitations/") and path.endswith("/accept")
    )


async def authenticate_api_key(raw_key: str) -> AuthContext | None:
    """Resolve an X-API-Key to a tenant-scoped principal.

    API keys are indexed by their non-secret prefix. Lookup runs in a separate privileged session
    because the key must be resolved before a tenant GUC exists; the request's own session is then
    opened with the resolved tenant and normal RLS applies to the handler.
    """
    settings = _settings()
    hasher = ApiKeyHasher(derive_api_key_pepper(settings.jwt_secret))
    try:
        prefix = hasher.split_prefix(raw_key)
    except Exception:  # noqa: BLE001 - malformed credentials are a 401, not a 500
        return None
    rows: list[dict[str, object]] = []
    async with session_scope() as lookup_db:
        can_switch = await lookup_db.scalar(
            text(
                "SELECT 1 FROM pg_roles r WHERE r.rolname = 'platform_auth' "
                "AND has_table_privilege(r.oid, 'platform.api_key', 'SELECT')"
            )
        )
        if can_switch:
            await lookup_db.execute(text("SET LOCAL ROLE platform_auth"))
        try:
            result = await lookup_db.execute(
                text(
                    "SELECT k.id, k.tenant_id, k.secret_hash, k.scopes, k.revoked_at, k.expires_at, "
                    "       k.created_by, u.identity_id, t.status AS tenant_status "
                    "FROM platform.api_key k "
                    "JOIN platform.tenant t ON t.id = k.tenant_id "
                    "LEFT JOIN platform.app_user u ON u.tenant_id = k.tenant_id AND u.id = k.created_by "
                    "WHERE k.key_prefix = :prefix"
                ),
                {"prefix": prefix},
            )
            rows = [dict(row) for row in result.mappings().all()]
        finally:
            if can_switch:
                await lookup_db.execute(text("RESET ROLE"))
    now = datetime.now(UTC)
    for row in rows:
        if not hasher.verify(raw_key, str(row["secret_hash"])):
            continue
        if row.get("revoked_at") is not None:
            return None
        expires_at = row.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, datetime):
                return None
            expired_at = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
            if expired_at <= now:
                return None
        if str(row.get("tenant_status") or "") != "ACTIVE":
            return None
        tenant_id = str(row["tenant_id"])
        created_by = row.get("created_by")
        key_id = str(row["id"])
        app_user_id = str(created_by or key_id)
        identity_id = str(row.get("identity_id") or created_by or key_id)
        return AuthContext(
            kind="api_key",
            identity_id=identity_id,
            app_user_id=app_user_id,
            tenant_id=tenant_id,
            token_version=0,
            scopes=frozenset(str(scope) for scope in (row.get("scopes") or [])),
        )
    return None


async def get_request_auth(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthContext | None:
    """Resolve bearer JWT or X-API-Key without requiring a tenant context yet."""
    del db
    path = request.url.path
    if path.startswith("/api/v1/bootstrap") or _is_public_auth_path(path):
        return None
    api_key = request.headers.get("x-api-key")
    if api_key:
        # Machine credentials are for business integrations, never for identity/session
        # self-service endpoints (login/refresh/switch-tenant/me). Those require a JWT.
        if path.startswith("/api/v1/auth/") or path == "/api/v1/me" or path.startswith("/api/v1/me/"):
            return None
        return await authenticate_api_key(api_key)
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            try:
                claims = token_service().decode_access_token(token)
            except SecurityError:
                return None
            return AuthContext(
                kind="bearer",
                identity_id=claims.sub,
                app_user_id=claims.uid,
                tenant_id=claims.tid,
                token_version=claims.tv,
                scopes=frozenset(claims.scopes),
            )
    return None


async def get_request_db(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext | None, Depends(get_request_auth)],
) -> AsyncSession:
    """Request DB session with tenant RLS context / auth-role routing.

    * Authenticated requests set ``app.tenant_id`` from the verified access-token claim in the same
      transaction as the handler (``SET LOCAL``), so RLS policies apply to the app login.
    * Public authentication endpoints run as the ``platform_auth`` service role (BYPASSRLS) because
      login must discover identities/memberships before a tenant context exists. The role is optional
      in dev (superuser test DB); in production provision-db-roles.sh creates it.
    """
    path = request.url.path
    auth_path = path.startswith("/api/v1/auth") or (
        path.startswith("/api/v1/admin/invitations/") and path.endswith("/accept")
    )
    if path.startswith("/api/v1/bootstrap"):
        # Pre-tenant operator endpoints have no tenant claim. Give RLS a safe SYSTEM context so
        # lookups before the new tenant id exists do not evaluate an empty setting.
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": "00000000-0000-0000-0000-000000000000"},
        )
    elif auth_path:
        # Switch to the auth service role only where it is actually provisioned/granted (production
        # DB). The role is cluster-wide, while the dev/test database intentionally runs as superuser
        # without platform_auth grants; switching there would break tests and is unnecessary.
        can_switch = await db.scalar(
            text(
                "SELECT 1 FROM pg_roles r WHERE r.rolname = 'platform_auth' "
                "AND has_table_privilege(r.oid, 'platform.user_identity', 'SELECT')"
            )
        )
        if can_switch:
            await db.execute(text("SET LOCAL ROLE platform_auth"))
    elif auth is not None:
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": auth.tenant_id},
        )
    return db


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
    auth: Annotated[AuthContext | None, Depends(get_request_auth)],
) -> Principal:
    if auth is None:
        raise TokenInvalid("bearer token or API key required")
    return Principal(
        identity_id=auth.identity_id,
        app_user_id=auth.app_user_id,
        tenant_id=auth.tenant_id,
        token_version=auth.token_version,
        scopes=auth.scopes,
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
_ACTION_BODY_OPTIONAL_TAILS = frozenset({
    "submit", "post", "approve", "reject", "release", "start", "complete", "cancel", "void",
    "reverse", "match", "assign", "activate", "inactivate", "close", "pause", "resume", "apply",
    "rotate", "rotate-secret", "revoke", "resend", "accept", "deactivate", "reactivate", "issue",
    "backflush", "unbuild", "build", "refresh", "archive", "clone", "merge", "suspend", "offboard",
    "logout", "select-tenant", "test", "evaluate", "generate", "recalculate", "reset", "confirm",
    "arrive", "depart", "receive", "dispatch", "deliver", "load", "unload", "pack", "ship", "pick",
    "putaway", "manifest", "collect", "move", "recount", "count", "transfer",
})


async def reject_empty_create_body(request: Request) -> None:
    """Return 422 for an authenticated create-style JSON request with an empty object body.

    Many module routers intentionally accept a free-form ``dict`` payload and validate it in the
    service layer. Empty objects reached those services and raised ``KeyError``/DB NOT NULL errors
    (500). Action endpoints (submit/post/approve/...) legitimately accept an empty body; create
    endpoints require the caller to supply the payload.
    """
    if request.method not in {"POST", "PUT", "PATCH"}:
        return
    if (
        not request.headers.get("authorization")
        and not request.headers.get("x-api-key")
        and not request.headers.get("x-super-admin-token")
    ):
        return
    path = request.url.path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    if tail in _ACTION_BODY_OPTIONAL_TAILS:
        return
    content_type = request.headers.get("content-type", "")
    if "json" not in content_type:
        return
    raw = await request.body()
    if not raw.strip():
        return
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return
    if parsed == {}:
        raise HTTPException(status_code=422, detail="request body must contain the required fields")


DbDep = Annotated[AsyncSession, Depends(get_request_db)]
PrincipalDep = Annotated[Principal, Depends(get_current_principal)]
