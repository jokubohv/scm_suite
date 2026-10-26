"""FS-00 Δ-SSO API: public SSO login (start/callback) + tenant-admin SSO administration.

Implements the FS-00 §6/§7 delta-SSO surface (DEX-SSO-ADDENDUM §3–§6):

* public ``POST /api/v1/auth/sso/start`` and ``GET /api/v1/auth/sso/callback`` — state/nonce/PKCE S256
  single-use, tenant/redirect-bound, Dex authorization-code exchange behind an injected HTTP client,
  strict ID-token claim validation, domain policy, link/JIT identity resolution, bounded group→role
  mapping and platform token issuance. Typed SSO errors; **no silent local fallback**.
* tenant-admin connections CRUD/test/rotate-secret/group-mappings/identity-links under
  ``platform.sso.manage`` (writes) / ``platform.sso.read`` (reads); secrets are secret-store refs only;
  cross-tenant ids return 404; every mutation is audited.

The integrator registers ``router`` in ``app/api/registry.py`` — this module never edits main/registry/deps,
models or migrations. Pure policy helpers come from :mod:`app.core.security.authn`; persistence from
:mod:`app.core.repositories.sso`; platform tokens from ``app.api.deps.token_service``.

OpenID Connect client behaviour (token exchange/JWKS) is behind :class:`DexClient`; tests inject a fake so
no live network is required. SSO state is single-use in an injected store (Redis in deployments, in-memory
for tests). Dex tokens are never accepted as platform bearer tokens.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, require_scope, scope_registry, token_service
from app.api.schemas.platform import PlatformObject
from app.core.config import get_settings
from app.core.platform_events import emit_platform_event, sod_blocked
from app.core.repositories.auth import AuthRepository, hash_refresh_token
from app.core.repositories.sso import (
    CONNECTION_PROTOCOLS,
    CONNECTION_STATUSES,
    SsoRepository,
    as_uuid,
    utc_now,
)
from app.core.security import authn
from app.core.security.errors import (
    SecurityError,
    SodConflict,
    SsoConnectionInvalid,
    SsoDisabled,
    SsoEmailDomainNotAllowed,
    SsoGroupMappingInvalid,
    SsoIdpUnavailable,
    SsoIdTokenInvalid,
    SsoStateInvalid,
)
from app.core.security.scopes import SodContext

router = APIRouter(prefix="/api/v1", tags=["platform-sso"])

SSO_MANAGE = "platform.sso.manage"
SSO_READ = "platform.sso.read"
DEFAULT_REDIRECT_PATH = "/api/v1/auth/sso/callback"
_STATE_PREFIX = "sso:state:"
_DEFAULT_REDIRECT = "http://localhost:8000" + DEFAULT_REDIRECT_PATH


# =========================================================================== dependencies ======
def _repo(db: DbDep) -> SsoRepository:
    return SsoRepository(db)


RepoDep = Annotated[SsoRepository, Depends(_repo)]


def _auth_repo(db: DbDep) -> AuthRepository:
    return AuthRepository(db)


AuthRepoDep = Annotated[AuthRepository, Depends(_auth_repo)]


ManageDep = Annotated[PrincipalDep, Depends(require_scope(SSO_MANAGE))]
ReadDep = Annotated[PrincipalDep, Depends(require_scope(SSO_READ))]


# =========================================================================== Dex client ========
class DexClient(Protocol):
    """Injected OpenID Connect client (network I/O isolated for tests)."""

    async def exchange_code(
        self,
        *,
        connection: authn.SsoConnectionView,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        client_secret_ref: str | None = None,
    ) -> Mapping[str, Any]: ...

    async def verify_id_token(self, *, connection: authn.SsoConnectionView, id_token: str) -> Mapping[str, Any]: ...

    async def health_check(self, *, connection: authn.SsoConnectionView) -> bool: ...


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


class EnvSecretResolver:
    """Dev/test secret resolver: ``env:NAME`` references; production uses the secret manager."""

    def resolve(self, secret_ref: str) -> str:
        if secret_ref.startswith("env:"):
            value = os.environ.get(secret_ref[4:], "")
            if value:
                return value
            raise SsoIdpUnavailable("configured SSO secret is missing", details={"secret_ref": secret_ref})
        raise SsoIdpUnavailable(
            "SSO secret reference is not resolvable by this deployment", details={"secret_ref": secret_ref}
        )


class HttpDexClient:
    """Dex OIDC client over HTTP(S): discovery, code exchange and JWKS-backed ID-token verification."""

    def __init__(
        self,
        *,
        settings: Any,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._settings = settings
        self._secrets = secret_resolver or EnvSecretResolver()
        self._transport = transport
        self._timeout = timeout_seconds
        self._discovery_cache: dict[str, dict[str, Any]] = {}
        self._jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                return await client.request(method, url, **kwargs)
        except (httpx.HTTPError, OSError) as exc:  # network failures never downgrade to local auth
            raise SsoIdpUnavailable("identity provider is unavailable", details={"reason": type(exc).__name__}) from exc

    async def _discovery(self, issuer: str) -> dict[str, Any]:
        cached = self._discovery_cache.get(issuer)
        if cached is not None:
            return cached
        response = await self._request("GET", issuer.rstrip("/") + "/.well-known/openid-configuration")
        if response.status_code >= 400:
            raise SsoIdpUnavailable("identity provider discovery failed", details={"status": response.status_code})
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - malformed upstream
            raise SsoIdpUnavailable("identity provider discovery response is invalid") from exc
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
            if not payload.get(field):
                raise SsoIdpUnavailable(
                    "identity provider discovery is missing required metadata", details={"field": field}
                )
        self._discovery_cache[issuer] = payload
        return payload

    def _secret_for(self, connection: authn.SsoConnectionView, *, client_secret_ref: str | None = None) -> str:
        if client_secret_ref:
            return self._secrets.resolve(client_secret_ref)
        # Shared dev issuer: the platform client secret is deployment configuration.
        secret = getattr(self._settings, "dex_client_secret", "") or ""
        if not secret:
            raise SsoIdpUnavailable("identity provider client secret is not configured")
        return secret

    async def exchange_code(
        self,
        *,
        connection: authn.SsoConnectionView,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        client_secret_ref: str | None = None,
    ) -> Mapping[str, Any]:
        discovery = await self._discovery(connection.dex_issuer_url)
        if discovery.get("token_endpoint_auth_methods_supported") and "client_secret_basic" in (
            discovery.get("token_endpoint_auth_methods_supported") or []
        ):
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "client_id": connection.client_id,
            }
            response = await self._request(
                "POST",
                discovery["token_endpoint"],
                data=data,
                auth=(connection.client_id, self._secret_for(connection, client_secret_ref=client_secret_ref)),
            )
        else:
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "client_id": connection.client_id,
                "client_secret": self._secret_for(connection, client_secret_ref=client_secret_ref),
            }
            response = await self._request("POST", discovery["token_endpoint"], data=data)
        if response.status_code == 400:
            raise SsoIdTokenInvalid("authorization code exchange was rejected", details={"reason": "CODE_REJECTED"})
        if response.status_code >= 400:
            raise SsoIdpUnavailable("authorization code exchange failed", details={"status": response.status_code})
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - malformed upstream
            raise SsoIdpUnavailable("token endpoint returned invalid JSON") from exc

    async def _jwks(self, issuer: str) -> dict[str, Any]:
        cached = self._jwks_cache.get(issuer)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        discovery = await self._discovery(issuer)
        response = await self._request("GET", discovery["jwks_uri"])
        if response.status_code >= 400:
            raise SsoIdpUnavailable("identity provider JWKS is unavailable", details={"status": response.status_code})
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover
            raise SsoIdpUnavailable("identity provider JWKS response is invalid") from exc
        self._jwks_cache[issuer] = (time.monotonic() + 300.0, payload)
        return payload

    async def verify_id_token(self, *, connection: authn.SsoConnectionView, id_token: str) -> Mapping[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SsoIdTokenInvalid("ID token header is invalid", details={"reason": type(exc).__name__}) from exc
        try:
            jwks = await self._jwks(connection.dex_issuer_url)
            keys = payload = jwks.get("keys", [])
            kid = header.get("kid")
            candidates = [key for key in payload if key.get("kid") == kid] if kid else list(keys)
            if not candidates:
                raise SsoIdTokenInvalid("ID token signing key is unknown", details={"reason": "UNKNOWN_KID"})
            last_error: Exception | None = None
            for jwk in candidates:
                try:
                    public_key: Any = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
                    claims = jwt.decode(
                        id_token,
                        public_key,
                        algorithms=[jwk.get("alg", header.get("alg", "RS256"))],
                        options={
                            "verify_exp": False,
                            "verify_iat": False,
                            "verify_nbf": False,
                            "verify_aud": False,
                            "verify_iss": False,
                        },
                    )
                    if isinstance(claims, Mapping):
                        return claims
                except jwt.PyJWTError as exc:  # try the next candidate key
                    last_error = exc
            raise SsoIdTokenInvalid(
                "ID token signature is invalid",
                details={"reason": type(last_error).__name__ if last_error else "SIGNATURE_INVALID"},
            )
        except SsoIdTokenInvalid:
            raise
        except SsoIdpUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise SsoIdTokenInvalid("ID token could not be verified", details={"reason": type(exc).__name__}) from exc

    async def health_check(self, *, connection: authn.SsoConnectionView) -> bool:
        await self._discovery(connection.dex_issuer_url)
        await self._jwks(connection.dex_issuer_url)
        return True


def _dex_client(request: Request) -> DexClient:
    injected = getattr(request.app.state, "dex_client", None)
    if injected is not None:
        return injected
    if not hasattr(request.app.state, "_default_dex_client"):
        request.app.state._default_dex_client = HttpDexClient(settings=get_settings())  # noqa: SLF001
    return request.app.state._default_dex_client  # noqa: SLF001


DexDep = Annotated[DexClient, Depends(_dex_client)]


# =========================================================================== state store =======
class SsoStateStore(Protocol):
    async def put(self, request: authn.AuthorizationRequest) -> None: ...

    async def pop(self, state: str) -> authn.AuthorizationRequest | None: ...


class InMemorySsoStateStore:
    """Deterministic single-process store for tests/dev fallback (single-use ``pop``)."""

    def __init__(self) -> None:
        self._requests: dict[str, authn.AuthorizationRequest] = {}

    async def put(self, request: authn.AuthorizationRequest) -> None:
        self._requests[request.state] = request

    async def pop(self, state: str) -> authn.AuthorizationRequest | None:
        request = self._requests.pop(state, None)
        if request is not None and request.is_expired(utc_now()):
            return None
        return request


class RedisSsoStateStore:
    """Redis-backed single-use state store (5-minute TTL, atomic GETDEL)."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._client: Any = None

    def _redis(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis_asyncio

            self._client = redis_asyncio.from_url(self._url, decode_responses=True)
        return self._client

    async def put(self, request: authn.AuthorizationRequest) -> None:
        try:
            await self._redis().setex(
                _STATE_PREFIX + request.state,
                max(1, int((request.expires_at - utc_now()).total_seconds())),
                json.dumps(_authorization_request_payload(request)),
            )
        except Exception as exc:  # noqa: BLE001 - state store outage is a typed 503
            raise SsoIdpUnavailable("SSO state store is unavailable") from exc

    async def pop(self, state: str) -> authn.AuthorizationRequest | None:
        try:
            raw = await self._redis().getdel(_STATE_PREFIX + state)
        except Exception as exc:  # noqa: BLE001
            raise SsoIdpUnavailable("SSO state store is unavailable") from exc
        if not raw:
            return None
        payload = json.loads(raw)
        try:
            return authn.AuthorizationRequest(
                connection_id=payload["connection_id"],
                tenant_id=payload["tenant_id"],
                issuer=payload["issuer"],
                state=payload["state"],
                nonce=payload["nonce"],
                code_verifier=payload["code_verifier"],
                code_challenge=payload["code_challenge"],
                redirect_uri=payload["redirect_uri"],
                created_at=datetime.fromisoformat(payload["created_at"]),
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
        except (KeyError, ValueError):  # pragma: no cover - corrupted state
            return None


def _authorization_request_payload(request: authn.AuthorizationRequest) -> dict[str, Any]:
    payload = asdict(request)
    payload["created_at"] = request.created_at.isoformat()
    payload["expires_at"] = request.expires_at.isoformat()
    return payload


_IN_MEMORY_STATE = InMemorySsoStateStore()


def _state_store(request: Request) -> SsoStateStore:
    injected = getattr(request.app.state, "sso_state_store", None)
    if injected is not None:
        return injected
    settings = get_settings()
    if settings.env == "test" or not settings.redis_url:
        return _IN_MEMORY_STATE
    if not hasattr(request.app.state, "_default_state_store"):
        request.app.state._default_state_store = RedisSsoStateStore(settings.redis_url)  # noqa: SLF001
    return request.app.state._default_state_store  # noqa: SLF001


StateStoreDep = Annotated[SsoStateStore, Depends(_state_store)]


# =========================================================================== helpers ==========
def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _not_found(message: str = "SSO connection not found") -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "SSO_CONNECTION_NOT_FOUND", "message": message})


def _redirect_allowlist() -> tuple[str, ...]:
    raw = os.environ.get("SSO_REDIRECT_URI_ALLOWLIST", "").strip()
    if raw:
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    base = os.environ.get("SSO_PUBLIC_BASE_URL", "").rstrip("/")
    return (base + DEFAULT_REDIRECT_PATH,) if base else (_DEFAULT_REDIRECT,)


def _platform_redirect_uri() -> str:
    configured = os.environ.get("SSO_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return _REDIRECT_ALLOWLIST_DEFAULT[0]


_REDIRECT_ALLOWLIST_DEFAULT = _redirect_allowlist()
_PLATFORM_REDIRECT_URI = _platform_redirect_uri()


def _connection_response(row: Mapping[str, Any], *, role_codes: Mapping[str, str] | None = None) -> dict[str, Any]:
    default_role_ids = [str(role_id) for role_id in (row.get("default_role_ids") or ())]
    codes = role_codes or {}
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "name": row["name"],
        "protocol": row["protocol"],
        "status": row["status"],
        "dex_issuer_url": row.get("dex_issuer_url"),
        "dex_instance_ref": row.get("dex_instance_ref"),
        "client_id": row.get("client_id"),
        "client_secret_configured": bool(row.get("client_secret_ref")),
        "connector_config": row.get("connector_config") or {},
        "email_domains": list(row.get("email_domains") or ()),
        "is_enforced": bool(row.get("is_enforced")),
        "jit_provisioning": bool(row.get("jit_provisioning")),
        "default_role_ids": default_role_ids,
        "default_role_keys": [codes.get(role_id, role_id) for role_id in default_role_ids],
        "group_claim_name": row.get("group_claim_name") or "groups",
        "last_tested_at": _iso(row.get("last_tested_at")),
        "last_error": row.get("last_error"),
        "inactive_at": _iso(row.get("inactive_at")),
        "version": int(row.get("version") or 1),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _connection_view(
    row: Mapping[str, Any],
    *,
    tenant_code: str,
    tenant_name: str,
    default_role_keys: tuple[str, ...] = (),
) -> authn.SsoConnectionView:
    if not row.get("dex_issuer_url"):
        raise SsoConnectionInvalid(
            "SSO connection has no Dex issuer configured", details={"connection_id": str(row["id"])}
        )
    if not row.get("client_id"):
        raise SsoConnectionInvalid(
            "SSO connection has no client id configured", details={"connection_id": str(row["id"])}
        )
    return authn.SsoConnectionView(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        tenant_code=tenant_code,
        tenant_name=tenant_name,
        protocol=authn.SsoProtocol(str(row["protocol"])),
        status=authn.SsoConnectionStatus(str(row["status"])),
        dex_issuer_url=str(row["dex_issuer_url"]),
        client_id=str(row["client_id"]),
        email_domains=tuple(str(domain) for domain in (row.get("email_domains") or ())),
        is_enforced=bool(row.get("is_enforced")),
        jit_provisioning=bool(row.get("jit_provisioning")),
        default_role_keys=default_role_keys,
        group_claim_name=str(row.get("group_claim_name") or "groups"),
        name=str(row.get("name") or ""),
    )


async def _default_role_codes(repo: SsoRepository, tenant_id: Any, row: Mapping[str, Any]) -> tuple[str, ...]:
    roles = await repo.roles_by_ids(tenant_id, row.get("default_role_ids") or ())
    return tuple(role["code"] for role in roles.values())


async def _connection_row_or_404(repo: SsoRepository, tenant_id: Any, connection_id: Any) -> dict[str, Any]:
    row = await repo.get_connection_row(tenant_id, connection_id)
    if row is None:
        raise _not_found()
    return row


# =========================================================================== schemas ===========
class SsoStartRequest(BaseModel):
    email_or_tenant: str = Field(min_length=1, max_length=320)
    tenant_id: str | None = None
    redirect_uri: str | None = Field(default=None, max_length=500)


class GroupMappingIn(BaseModel):
    match_type: str = Field(pattern="^(EXACT|REGEX)$")
    group_value: str = Field(min_length=1, max_length=500)
    role_key: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=100, ge=0, le=100000)
    is_active: bool = True


class GroupMappingsPut(BaseModel):
    mappings: list[GroupMappingIn] = Field(default_factory=list, max_length=200)


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    protocol: str
    dex_issuer_url: str = Field(min_length=1, max_length=500)
    dex_instance_ref: str | None = Field(default=None, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret_ref: str | None = Field(default=None, max_length=300)
    connector_config: dict[str, Any] = Field(default_factory=dict)
    email_domains: list[str] = Field(default_factory=list, max_length=100)
    is_enforced: bool = False
    jit_provisioning: bool = False
    default_role_keys: list[str] = Field(default_factory=list, max_length=50)
    group_claim_name: str = Field(default="groups", min_length=1, max_length=100)


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    dex_issuer_url: str | None = Field(default=None, min_length=1, max_length=500)
    dex_instance_ref: str | None = Field(default=None, max_length=200)
    client_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_secret_ref: str | None = Field(default=None, max_length=300)
    connector_config: dict[str, Any] | None = None
    email_domains: list[str] | None = Field(default=None, max_length=100)
    is_enforced: bool | None = None
    jit_provisioning: bool | None = None
    default_role_keys: list[str] | None = Field(default=None, max_length=50)
    group_claim_name: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = None


class SecretRotation(BaseModel):
    secret_ref: str = Field(min_length=1, max_length=300)


class IdentityLinkCreate(BaseModel):
    connection_id: str = Field(min_length=1)
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=500)
    email_at_idp: str | None = Field(default=None, max_length=320)
    email_verified: bool = False


# =========================================================================== validation ========
def _validate_connection_values(values: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(values)
    protocol = str(result.get("protocol", "OIDC")).upper()
    if protocol not in CONNECTION_PROTOCOLS:
        raise SsoConnectionInvalid("unsupported SSO protocol", details={"protocol": protocol})
    result["protocol"] = protocol
    if "dex_issuer_url" in result and result["dex_issuer_url"] is not None:
        issuer = str(result["dex_issuer_url"]).strip().rstrip("/")
        if not issuer.startswith(("https://", "http://")):
            raise SsoConnectionInvalid("dex_issuer_url must be an http(s) URL")
        result["dex_issuer_url"] = issuer
    if "email_domains" in result and result["email_domains"] is not None:
        result["email_domains"] = list(authn.normalize_email_domains(result["email_domains"]))
    if "group_claim_name" in result and result["group_claim_name"] is not None:
        claim = str(result["group_claim_name"]).strip()
        if not claim:
            raise SsoConnectionInvalid("group_claim_name must not be empty")
        result["group_claim_name"] = claim
    if "status" in result and result["status"] is not None:
        status = str(result["status"]).upper()
        if status not in CONNECTION_STATUSES:
            raise SsoConnectionInvalid("unsupported SSO connection status", details={"status": status})
        result["status"] = status
    return result


async def _resolve_default_role_ids(repo: SsoRepository, tenant_id: Any, role_keys: list[str] | None) -> list[str]:
    if not role_keys:
        return []
    roles = await repo.roles_by_codes(tenant_id, role_keys)
    missing = sorted(set(role_keys) - set(roles))
    if missing:
        raise SsoGroupMappingInvalid("unknown default role key", details={"missing": missing})
    return [str(roles[key]["id"]) for key in role_keys]


async def _validate_mapping_roles(
    repo: SsoRepository, tenant_id: Any, mappings: list[GroupMappingIn]
) -> list[dict[str, Any]]:
    registry = scope_registry()
    role_map = await repo.roles_by_codes(tenant_id, [mapping.role_key for mapping in mappings])
    missing = sorted({mapping.role_key for mapping in mappings} - set(role_map))
    if missing:
        raise SsoGroupMappingInvalid("group mapping references unknown role", details={"missing": missing})
    scope_map = await repo.role_scopes(tenant_id, [role["id"] for role in role_map.values()])
    assignable = registry.catalogue.assignable_scopes()
    for key, role_row in role_map.items():
        scopes = scope_map.get(role_row["id"], frozenset())
        if not scopes and role_row.get("is_template"):
            scopes = registry.catalogue.template(key).scopes if key in registry.catalogue.templates_by_key else scopes
        non_assignable = sorted(scope for scope in scopes if scope not in assignable)
        if non_assignable:
            raise SsoGroupMappingInvalid(
                "group mapping role includes non-assignable scopes",
                details={"role_key": key, "scopes": non_assignable},
            )
    rows: list[dict[str, Any]] = []
    for mapping in mappings:
        role_row = role_map[mapping.role_key]
        rows.append(
            {
                "match_type": mapping.match_type,
                "group_value": mapping.group_value,
                "role_id": role_row["id"],
                "role_key": mapping.role_key,
                "priority": mapping.priority,
                "is_active": mapping.is_active,
            }
        )
    return rows


# =========================================================================== public endpoints ===
@router.post("/auth/sso/start", response_model=PlatformObject)
async def sso_start(payload: SsoStartRequest, repo: RepoDep, store: StateStoreDep) -> dict[str, Any]:
    """Resolve the tenant connection and return the Dex authorization URL (public, no auth)."""
    rows = await repo.list_active_connection_rows()
    views: list[authn.SsoConnectionView] = []
    for row in rows:
        try:
            views.append(
                _connection_view(
                    row,
                    tenant_code=str(row["tenant_code"]),
                    tenant_name=str(row["tenant_name"]),
                    default_role_keys=await _default_role_codes(repo, row["tenant_id"], row),
                )
            )
        except SsoConnectionInvalid:
            continue  # misconfigured ACTIVE connection is skipped, never silently downgraded
    connection = authn.resolve_connection_for_start(
        payload.email_or_tenant,
        connections=views,
        requested_tenant_id=payload.tenant_id,
    )
    redirect_uri = payload.redirect_uri or _PLATFORM_REDIRECT_URI
    authorization = authn.create_authorization_request(
        connection,
        redirect_uri=redirect_uri,
        redirect_allowlist=_REDIRECT_ALLOWLIST_DEFAULT,
    )
    await store.put(authorization)
    return {
        "authorization_url": authn.authorization_url(authorization, connection),
        "connection_id": connection.id,
        "tenant_id": connection.tenant_id,
        "tenant_code": connection.tenant_code,
        "tenant_name": connection.tenant_name,
        "protocol": connection.protocol.value,
        "redirect_uri": authorization.redirect_uri,
        "state_expires_at": authorization.expires_at.isoformat(),
        "local_login_allowed": authn.local_login_allowed(connection, break_glass_enabled=True),
    }


@router.get("/auth/sso/callback", response_model=PlatformObject)
async def sso_callback(
    request: Request,
    repo: RepoDep,
    auth_repo: AuthRepoDep,
    dex: DexDep,
    store: StateStoreDep,
    code: str = Query(..., max_length=8000),
    state: str = Query(..., min_length=8, max_length=500),
) -> dict[str, Any]:
    """Validate state/nonce/PKCE, exchange the code with Dex, link/JIT and issue a platform token pair."""
    stored = await store.pop(state)
    if stored is None:
        raise SsoStateInvalid(
            "authorization state is unknown, expired or already used", details={"reason": "STATE_UNKNOWN"}
        )
    authorization = authn.validate_callback_state(stored, state=state)
    row = await repo.get_connection_row(authorization.tenant_id, authorization.connection_id)
    if row is None:
        await emit_platform_event(
            repo.session, "sso.login.failed", tenant_id=str(authorization.tenant_id),
            aggregate_type="tenant_sso_connection", aggregate_id=str(authorization.connection_id),
            payload={"tenant_id": str(authorization.tenant_id),
                     "connection_id": str(authorization.connection_id), "identity_id": "unknown",
                     "reason_code": "SSO_CONNECTION_MISSING", "occurred_at": utc_now().isoformat()},
        )
        raise SsoDisabled("SSO connection no longer exists", details={"connection_id": authorization.connection_id})
    tenant = await repo.get_tenant(authorization.tenant_id)
    if tenant is None or str(tenant.get("status")) != "ACTIVE":
        await emit_platform_event(
            repo.session, "sso.login.failed", tenant_id=str(authorization.tenant_id),
            aggregate_type="tenant_sso_connection", aggregate_id=str(authorization.connection_id),
            payload={"tenant_id": str(authorization.tenant_id),
                     "connection_id": str(authorization.connection_id), "identity_id": "unknown",
                     "reason_code": "TENANT_INACTIVE", "occurred_at": utc_now().isoformat()},
        )
        raise SsoDisabled("tenant is not active for SSO login")
    default_keys = await _default_role_codes(repo, authorization.tenant_id, row)
    connection = _connection_view(
        row, tenant_code=str(tenant["code"]), tenant_name=str(tenant["name"]), default_role_keys=default_keys
    )
    authn.validate_connection_usable(connection)
    token_payload = await dex.exchange_code(
        connection=connection,
        code=code,
        redirect_uri=authorization.redirect_uri,
        code_verifier=authorization.code_verifier,
        client_secret_ref=row.get("client_secret_ref"),
    )
    id_token = token_payload.get("id_token") if isinstance(token_payload, Mapping) else None
    if not id_token or not isinstance(id_token, str):
        await emit_platform_event(
            repo.session, "sso.login.failed", tenant_id=str(authorization.tenant_id),
            aggregate_type="tenant_sso_connection", aggregate_id=str(authorization.connection_id),
            payload={"tenant_id": str(authorization.tenant_id),
                     "connection_id": str(authorization.connection_id), "identity_id": "unknown",
                     "reason_code": "ID_TOKEN_MISSING", "occurred_at": utc_now().isoformat()},
        )
        raise SsoIdTokenInvalid("token endpoint response is missing id_token", details={"reason": "ID_TOKEN_MISSING"})
    raw_claims = await dex.verify_id_token(connection=connection, id_token=id_token)
    claims = authn.validate_id_token_claims(
        raw_claims,
        expected_issuer=connection.dex_issuer_url,
        client_id=connection.client_id,
        expected_nonce=authorization.nonce,
        group_claim_name=connection.group_claim_name,
    )
    authn.ensure_email_domain_allowed(claims.email, connection)
    # -- identity resolution: link -> verified email -> JIT -> deny
    link = await repo.get_active_link(connection.tenant_id, connection.id, claims.issuer, claims.subject)
    member_by_email = await repo.tenant_member_by_email(connection.tenant_id, claims.email)
    global_identity = await repo.get_identity_by_email(claims.email)
    resolution = authn.decide_identity_resolution(
        existing_link_identity_id=str(link["identity_id"]) if link else None,
        linked_member_identity_id=str(member_by_email["identity"]["id"]) if member_by_email else None,
        identity_exists=global_identity is not None,
        email_verified=claims.email_verified,
        domain_allowed=True,
        jit_provisioning=bool(connection.jit_provisioning and get_settings().sso_jit_provisioning_enabled),
    )
    authn.require_identity_resolution_allowed(resolution)
    identity_id: str | None = resolution.identity_id
    if identity_id is None and global_identity is not None:
        identity_id = str(global_identity["id"])
    membership_row = await repo.get_membership(connection.tenant_id, identity_id) if identity_id is not None else None
    created_membership = False
    if resolution.create_identity and identity_id is None:
        identity = await repo.create_identity(email=claims.email, display_name=claims.email.split("@", 1)[0])
        identity_id = str(identity["id"])
    if resolution.create_membership and membership_row is None:
        if identity_id is None:  # pragma: no cover - resolution guarantees an identity
            raise SsoIdTokenInvalid("identity could not be resolved")
        membership_row = await repo.create_membership(connection.tenant_id, identity_id=identity_id, email=claims.email)
        created_membership = True
        default_roles = await repo.roles_by_ids(connection.tenant_id, row.get("default_role_ids") or ())
        await repo.assign_roles(
            connection.tenant_id,
            membership_row["id"],
            list(default_roles),
            actor_id=None,
        )
    if link is None:
        if identity_id is None:  # pragma: no cover - defensive
            raise SsoIdTokenInvalid("identity could not be resolved")
        link = await repo.insert_identity_link(
            connection.tenant_id,
            connection_id=connection.id,
            identity_id=identity_id,
            issuer=claims.issuer,
            subject=claims.subject,
            email_at_idp=claims.email,
            email_verified=claims.email_verified,
        )
    else:
        await repo.touch_link_login(connection.tenant_id, str(link["id"]))
    if membership_row is None:  # linked identity without tenant membership -> denied (JIT/off or invitation needed)
        raise SsoEmailDomainNotAllowed(
            "SSO identity has no active membership in this tenant", details={"tenant_id": connection.tenant_id}
        )
    if identity_id is None:  # pragma: no cover - defensive (membership implies an identity)
        raise SsoIdTokenInvalid("SSO identity could not be resolved")
    membership_row = await repo.get_membership(connection.tenant_id, identity_id) or membership_row
    # -- group mapping (bounded by assignable roles + SoD)
    mapping_rows = await repo.list_group_mappings(connection.tenant_id, connection.id)
    mappings = [
        authn.GroupMapping(
            match_type=str(item["match_type"]),
            group_value=str(item["group_value"]),
            role_key=str(item["role_code"]),
            priority=int(item["priority"]),
            is_active=bool(item["is_active"]),
            mapping_id=str(item["id"]),
        )
        for item in mapping_rows
    ]
    if mappings:
        group_result = authn.evaluate_group_mappings_with_registry(
            claims.groups, mappings, registry=scope_registry(), allow_break_glass=False
        )
        mapped_role_ids = [
            item["role_id"] for item in mapping_rows if str(item["id"]) in set(group_result.matched_mapping_ids)
        ]
        if mapped_role_ids:
            await repo.assign_roles(connection.tenant_id, membership_row["id"], mapped_role_ids, actor_id=None)
    tenant_uuid = as_uuid(connection.tenant_id)
    user_uuid = as_uuid(membership_row["id"])
    identity_uuid = as_uuid(identity_id)
    if (
        tenant_uuid is None or user_uuid is None or identity_uuid is None
    ):  # pragma: no cover - resolution guarantees ids
        raise SsoIdTokenInvalid("SSO identity could not be resolved")
    await auth_repo.touch_last_login(
        identity_id=identity_uuid,
        tenant_id=tenant_uuid,
        app_user_id=user_uuid,
        now=utc_now(),
    )
    scopes = await auth_repo.effective_scopes(tenant_uuid, user_uuid)
    session_id = uuid4()
    token_pair = token_service().issue_token_pair(
        identity_id=identity_id or "",
        app_user_id=str(user_uuid),
        tenant_id=connection.tenant_id,
        token_version=int(membership_row.get("token_version") or 0),
        scopes=scopes,
        session_id=str(session_id),
    )
    await auth_repo.create_session(
        tenant_id=tenant_uuid,
        app_user_id=user_uuid,
        identity_id=identity_uuid,
        refresh_token_hash=hash_refresh_token(token_pair.refresh_token),
        issued_at=token_pair.access_claims.iat,
        expires_at=token_pair.refresh_claims.exp,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await repo.audit(
        tenant_id=connection.tenant_id,
        actor_id=identity_id,
        action="sso.login.succeeded",
        resource_type="user_identity_link",
        resource_id=str(link["id"]),
        diff={"connection_id": connection.id, "created_membership": created_membership, "method": "SSO"},
    )
    return {
        "access_token": token_pair.access_token,
        "refresh_token": token_pair.refresh_token,
        "token_type": token_pair.token_type,
        "expires_in": token_pair.expires_in,
        "identity_id": identity_id,
        "app_user_id": str(membership_row["id"]),
        "tenant_id": connection.tenant_id,
        "tenant_code": connection.tenant_code,
        "scopes": sorted(scopes),
        "connection_id": connection.id,
    }


# =========================================================================== admin: connections ==
async def _connection_role_codes(repo: SsoRepository, tenant_id: Any, row: Mapping[str, Any]) -> dict[str, str]:
    roles = await repo.roles_by_ids(tenant_id, row.get("default_role_ids") or ())
    return {str(role_id): role["code"] for role_id, role in roles.items()}


@router.get("/admin/sso-connections", response_model=PlatformObject)
async def list_sso_connections(
    repo: RepoDep,
    principal: ReadDep,
    status: str | None = None,
    protocol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await repo.list_connections(
        principal.tenant_id, status=status, protocol=protocol, limit=limit, offset=offset
    )
    items = []
    for row in rows:
        codes = await _connection_role_codes(repo, principal.tenant_id, row)
        items.append(_connection_response(row, role_codes=codes))
    return {"items": items, "limit": limit, "offset": offset}


@router.post("/admin/sso-connections", response_model=PlatformObject, status_code=201)
async def create_sso_connection(payload: ConnectionCreate, repo: RepoDep, principal: ManageDep) -> dict[str, Any]:
    if payload.jit_provisioning and not get_settings().sso_jit_provisioning_enabled:
        raise HTTPException(status_code=403, detail={"code": "JIT_PROVISIONING_DISABLED",
                                                     "message": "SSO JIT account creation is disabled by deployment policy",
                                                     "details": {}})
    values = _validate_connection_values(
        payload.model_dump(exclude_none=True) | {"default_role_keys": payload.default_role_keys, "status": "DRAFT"}
    )
    values.pop("default_role_keys", None)
    default_role_ids = await _resolve_default_role_ids(repo, principal.tenant_id, payload.default_role_keys)
    values["default_role_ids"] = [as_uuid(role_id) for role_id in default_role_ids]
    row = await repo.create_connection(principal.tenant_id, values, actor_id=principal.identity_id)
    await repo.audit(
        tenant_id=principal.tenant_id,
        actor_id=principal.identity_id,
        action="sso.connection.created",
        resource_type="tenant_sso_connection",
        resource_id=str(row["id"]),
        diff={"name": row["name"], "protocol": row["protocol"]},
    )
    await emit_platform_event(
        repo.session, "sso.connection.created", tenant_id=principal.tenant_id,
        aggregate_type="tenant_sso_connection", aggregate_id=str(row["id"]),
        payload={"tenant_id": principal.tenant_id, "connection_id": str(row["id"]),
                 "name": row["name"], "protocol": row["protocol"], "status": row["status"]},
    )
    codes = await _connection_role_codes(repo, principal.tenant_id, row)
    return _connection_response(row, role_codes=codes)


@router.get("/admin/sso-connections/{connection_id}", response_model=PlatformObject)
async def get_sso_connection(connection_id: str, repo: RepoDep, principal: ReadDep) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    codes = await _connection_role_codes(repo, principal.tenant_id, row)
    active_links = await repo.count_active_links(principal.tenant_id, row["id"])
    return _connection_response(row, role_codes=codes) | {"active_identity_links": active_links}


@router.patch("/admin/sso-connections/{connection_id}", response_model=PlatformObject)
async def update_sso_connection(
    connection_id: str, payload: ConnectionUpdate, repo: RepoDep, principal: ManageDep
) -> dict[str, Any]:
    current = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("jit_provisioning") is True and not get_settings().sso_jit_provisioning_enabled:
        raise HTTPException(status_code=403, detail={"code": "JIT_PROVISIONING_DISABLED",
                                                     "message": "SSO JIT account creation is disabled by deployment policy",
                                                     "details": {}})
    if changes.get("status") not in (None, "DRAFT", "DISABLED"):
        raise SsoConnectionInvalid("status changes to TESTING/ACTIVE go through the /test action")
    values = _validate_connection_values(changes)
    if "default_role_keys" in values:
        role_ids = await _resolve_default_role_ids(repo, principal.tenant_id, values.pop("default_role_keys"))
        values["default_role_ids"] = [as_uuid(role_id) for role_id in role_ids]
    row = await repo.update_connection(principal.tenant_id, current["id"], values, actor_id=principal.identity_id)
    if row is None:  # pragma: no cover - race
        raise _not_found()
    await repo.audit(
        tenant_id=principal.tenant_id,
        actor_id=principal.identity_id,
        action="sso.connection.updated",
        resource_type="tenant_sso_connection",
        resource_id=str(row["id"]),
        diff={"keys": sorted(values)},
    )
    await emit_platform_event(
        repo.session, "sso.connection.updated", tenant_id=principal.tenant_id,
        aggregate_type="tenant_sso_connection", aggregate_id=str(row["id"]),
        payload={"tenant_id": principal.tenant_id, "connection_id": str(row["id"]),
                 "changed_fields": sorted(values), "status": row["status"]},
    )
    if str(row["status"]).upper() == "DISABLED":
        await emit_platform_event(
            repo.session, "sso.connection.disabled", tenant_id=principal.tenant_id,
            aggregate_type="tenant_sso_connection", aggregate_id=str(row["id"]),
            payload={"tenant_id": principal.tenant_id, "connection_id": str(row["id"]),
                     "reason": "status update", "sessions_revoked": 0},
        )
    codes = await _connection_role_codes(repo, principal.tenant_id, row)
    return _connection_response(row, role_codes=codes)


@router.delete("/admin/sso-connections/{connection_id}", response_model=PlatformObject)
async def delete_sso_connection(connection_id: str, repo: RepoDep, principal: ManageDep) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    active_links = await repo.count_active_links(principal.tenant_id, row["id"])
    if active_links:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSO_CONNECTION_HAS_ACTIVE_LINKS",
                "message": "connection has ACTIVE identity links; disable it instead of deleting",
                "details": {"active_links": active_links},
            },
        )
    deleted = await repo.delete_connection(principal.tenant_id, row["id"], actor_id=principal.identity_id)
    if not deleted:  # pragma: no cover - race
        raise _not_found()
    return {"deleted": True, "connection_id": str(row["id"])}


@router.post("/admin/sso-connections/{connection_id}/test", response_model=PlatformObject)
async def test_sso_connection(connection_id: str, repo: RepoDep, principal: ManageDep, dex: DexDep) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    tenant = await repo.get_tenant(principal.tenant_id)
    try:
        connection = _connection_view(
            row,
            tenant_code=str(tenant["code"]) if tenant else "",
            tenant_name=str(tenant["name"]) if tenant else "",
            default_role_keys=await _default_role_codes(repo, principal.tenant_id, row),
        )
        await dex.health_check(connection=connection)
    except SecurityError as exc:
        await repo.set_connection_status(
            principal.tenant_id,
            row["id"],
            "ERROR",
            last_error=exc.message,
            tested=True,
            actor_id=principal.identity_id,
        )
        await emit_platform_event(
            repo.session, "sso.connection.error", tenant_id=principal.tenant_id,
            aggregate_type="tenant_sso_connection", aggregate_id=str(row["id"]),
            payload={"tenant_id": principal.tenant_id, "connection_id": str(row["id"]),
                     "error_code": getattr(exc, "code", "SSO_HEALTH_CHECK_FAILED"),
                     "last_error_summary": exc.message},
        )
        raise
    updated = await repo.set_connection_status(
        principal.tenant_id, row["id"], "ACTIVE", last_error=None, tested=True, actor_id=principal.identity_id
    )
    await repo.audit(
        tenant_id=principal.tenant_id,
        actor_id=principal.identity_id,
        action="sso.connection.tested",
        resource_type="tenant_sso_connection",
        resource_id=str(row["id"]),
        diff={"result": "ACTIVE"},
    )
    await emit_platform_event(
        repo.session, "sso.connection.tested", tenant_id=principal.tenant_id,
        aggregate_type="tenant_sso_connection", aggregate_id=str(row["id"]),
        payload={"tenant_id": principal.tenant_id, "connection_id": str(row["id"]),
                 "result": "ACTIVE", "status": "ACTIVE", "last_tested_at": utc_now().isoformat()},
    )
    codes = await _connection_role_codes(repo, principal.tenant_id, updated or row)
    return _connection_response(updated or row, role_codes=codes)


@router.post("/admin/sso-connections/{connection_id}/rotate-secret", response_model=PlatformObject)
async def rotate_sso_secret(
    connection_id: str, payload: SecretRotation, repo: RepoDep, principal: ManageDep
) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    updated = await repo.set_secret_ref(
        principal.tenant_id, row["id"], payload.secret_ref, actor_id=principal.identity_id
    )
    await repo.audit(
        tenant_id=principal.tenant_id,
        actor_id=principal.identity_id,
        action="sso.connection.secret_rotated",
        resource_type="tenant_sso_connection",
        resource_id=str(row["id"]),
        diff={"secret_ref_updated": True},
    )
    return {
        "connection_id": str(row["id"]),
        "secret_ref_updated": True,
        "client_secret_configured": bool(updated and updated.get("client_secret_ref")),
        "rotated_at": utc_now().isoformat(),
    }


# =========================================================================== group mappings =====
@router.get("/admin/sso-connections/{connection_id}/group-mappings", response_model=PlatformObject)
async def get_group_mappings(connection_id: str, repo: RepoDep, principal: ReadDep) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    items = await repo.list_group_mappings(principal.tenant_id, row["id"])
    return {
        "connection_id": str(row["id"]),
        "items": [
            {
                "id": str(item["id"]),
                "match_type": item["match_type"],
                "group_value": item["group_value"],
                "role_id": str(item["role_id"]),
                "role_key": item["role_code"],
                "role_name": item["role_name"],
                "priority": int(item["priority"]),
                "is_active": bool(item["is_active"]),
            }
            for item in items
        ],
    }


@router.put("/admin/sso-connections/{connection_id}/group-mappings", response_model=PlatformObject)
async def put_group_mappings(
    connection_id: str, payload: GroupMappingsPut, repo: RepoDep, principal: ManageDep
) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    rows = await _validate_mapping_roles(repo, principal.tenant_id, payload.mappings)
    for mapping in payload.mappings:
        authn.GroupMapping(
            match_type=mapping.match_type,
            group_value=mapping.group_value,
            role_key=mapping.role_key,
            priority=mapping.priority,
            is_active=mapping.is_active,
        )
    if rows:
        registry = scope_registry()
        role_ids = [row["role_id"] for row in rows]
        scope_map = await repo.role_scopes(principal.tenant_id, role_ids)
        union: set[str] = set()
        for role_id in role_ids:
            union.update(scope_map.get(role_id, frozenset()))
        try:
            registry.require_no_sod_conflict(union, context=SodContext.ASSIGNMENT)
        except SodConflict as exc:
            pairs = exc.details.get("pairs") if isinstance(exc.details, dict) else None
            await sod_blocked(repo.session, tenant_id=principal.tenant_id,
                              actor_app_user_id=principal.app_user_id, context="ASSIGNMENT",
                              pair=[str(p) for p in (list(pairs[0]) if pairs else [])],
                              rule="SOD_PAIR", resource_id=str(row["id"]))
            raise
    persisted = await repo.replace_group_mappings(principal.tenant_id, row["id"], rows, actor_id=principal.identity_id)
    return {
        "connection_id": str(row["id"]),
        "items": [
            {
                "id": str(item["id"]),
                "match_type": item["match_type"],
                "group_value": item["group_value"],
                "role_id": str(item["role_id"]),
                "role_key": item["role_code"],
                "priority": int(item["priority"]),
                "is_active": bool(item["is_active"]),
            }
            for item in persisted
        ],
    }


# =========================================================================== identity links =====
@router.get("/admin/sso-connections/{connection_id}/links", response_model=PlatformObject)
async def list_connection_links(
    connection_id: str,
    repo: RepoDep,
    principal: ReadDep,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    items = await repo.list_identity_links(
        principal.tenant_id, connection_id=row["id"], status=status, limit=limit, offset=offset
    )
    return {
        "connection_id": str(row["id"]),
        "items": [_identity_link_response(item) for item in items],
        "limit": limit,
        "offset": offset,
    }


@router.delete("/admin/sso-connections/{connection_id}/links/{link_id}", response_model=PlatformObject)
async def revoke_connection_link(
    connection_id: str, link_id: str, repo: RepoDep, principal: ManageDep
) -> dict[str, Any]:
    row = await _connection_row_or_404(repo, principal.tenant_id, connection_id)
    link = await repo.get_link(principal.tenant_id, link_id)
    if link is None or str(link["connection_id"]) != str(row["id"]):
        raise HTTPException(
            status_code=404, detail={"code": "SSO_IDENTITY_LINK_NOT_FOUND", "message": "identity link not found"}
        )
    revoked = await repo.revoke_identity_link(principal.tenant_id, link["id"], actor_id=principal.identity_id)
    sessions = await repo.revoke_sessions_for_identity(
        principal.tenant_id, link["identity_id"], reason="SSO_IDENTITY_LINK_REVOKED"
    )
    return {"link": _identity_link_response(revoked or link), "sessions_revoked": sessions}


def _identity_link_response(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item["id"]),
        "identity_id": str(item["identity_id"]),
        "identity_email": item.get("identity_email"),
        "connection_id": str(item["connection_id"]),
        "connection_name": item.get("connection_name"),
        "issuer": item["issuer"],
        "subject": item["subject"],
        "email_at_idp": item.get("email_at_idp"),
        "email_verified": bool(item.get("email_verified")),
        "status": item["status"],
        "linked_at": _iso(item.get("linked_at")),
        "last_login_at": _iso(item.get("last_login_at")),
        "revoked_at": _iso(item.get("revoked_at")),
    }


@router.get("/admin/users/{user_id}/identity-links", response_model=PlatformObject)
async def list_user_identity_links(user_id: str, repo: RepoDep, principal: ReadDep) -> dict[str, Any]:
    member = await repo.get_member(principal.tenant_id, user_id)
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "user not found"})
    items = await repo.list_identity_links(principal.tenant_id, identity_id=member["identity_id"])
    return {"user_id": str(member["id"]), "items": [_identity_link_response(item) for item in items]}


@router.post("/admin/users/{user_id}/identity-links", response_model=PlatformObject, status_code=201)
async def create_user_identity_link(
    user_id: str, payload: IdentityLinkCreate, repo: RepoDep, principal: ManageDep
) -> dict[str, Any]:
    member = await repo.get_member(principal.tenant_id, user_id)
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "user not found"})
    connection = await repo.get_connection_row(principal.tenant_id, payload.connection_id)
    if connection is None:
        raise _not_found()
    email = str(member.get("email") or "")
    if payload.email_at_idp:
        email = authn.normalize_email(payload.email_at_idp)
    link = await repo.insert_identity_link(
        principal.tenant_id,
        connection_id=connection["id"],
        identity_id=member["identity_id"],
        issuer=payload.issuer.strip(),
        subject=payload.subject.strip(),
        email_at_idp=email or None,
        email_verified=payload.email_verified,
        actor_id=principal.identity_id,
    )
    return _identity_link_response(link | {"identity_email": email or None, "connection_name": connection.get("name")})


@router.delete("/admin/users/{user_id}/identity-links/{link_id}", response_model=PlatformObject)
async def delete_user_identity_link(user_id: str, link_id: str, repo: RepoDep, principal: ManageDep) -> dict[str, Any]:
    member = await repo.get_member(principal.tenant_id, user_id)
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "user not found"})
    link = await repo.get_link(principal.tenant_id, link_id)
    if link is None or str(link["identity_id"]) != str(member["identity_id"]):
        raise HTTPException(
            status_code=404, detail={"code": "SSO_IDENTITY_LINK_NOT_FOUND", "message": "identity link not found"}
        )
    revoked = await repo.revoke_identity_link(principal.tenant_id, link["id"], actor_id=principal.identity_id)
    sessions = await repo.revoke_sessions_for_identity(
        principal.tenant_id, link["identity_id"], reason="SSO_IDENTITY_LINK_REVOKED"
    )
    return {"link": _identity_link_response(revoked or link), "sessions_revoked": sessions}


__all__ = ["InMemorySsoStateStore", "RedisSsoStateStore", "router"]
