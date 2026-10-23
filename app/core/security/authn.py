"""SSO authentication policy helpers (pure; FS-00 Δ-SSO).

Dex is the OIDC identity broker; this module contains only **decisions**, no network calls: connection
routing/discovery, enforced-SSO/local-fallback policy, authorization-request (state/nonce/PKCE) handling,
ID-token claim validation (the JWKS signature check is injected by the API layer), identity link/JIT
decisioning and bounded group→role mapping (:class:`ScopeRegistry` supplies the RBAC catalogue).

See design/DEX-SSO-ADDENDUM.md §4–§5 and ARCHITECTURE §5.2.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlencode

from app.core.security.errors import (
    JitProvisioningDisabled,
    SodConflict,
    SsoConnectionInvalid,
    SsoDisabled,
    SsoEmailDomainNotAllowed,
    SsoEnforcedLocalDenied,
    SsoGroupMappingInvalid,
    SsoIdTokenInvalid,
    SsoProviderNotFound,
    SsoStateInvalid,
)
from app.core.security.scopes import ADMIN_BREAK_GLASS_ROLE, ScopeRegistry, SodContext

SSO_STATE_TTL_SECONDS = 5 * 60
ID_TOKEN_LEEWAY_SECONDS = 30
PKCE_VERIFIER_MIN_LENGTH = 43
PKCE_VERIFIER_MAX_LENGTH = 128
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REGEX_MATCH = "REGEX"
_EXACT_MATCH = "EXACT"


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("SSO timestamps must be timezone-aware (UTC)")
    return moment.astimezone(UTC)


# --------------------------------------------------------------------------- views/enums --------


class SsoProtocol(StrEnum):
    SAML = "SAML"
    OIDC = "OIDC"
    LDAP_AD = "LDAP_AD"


class SsoConnectionStatus(StrEnum):
    DRAFT = "DRAFT"
    TESTING = "TESTING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class LoginMethod(StrEnum):
    LOCAL = "LOCAL"
    SSO = "SSO"


class IdentityResolutionKind(StrEnum):
    LINKED = "LINKED"
    LINK_BY_VERIFIED_EMAIL = "LINK_BY_VERIFIED_EMAIL"
    JIT_PROVISION = "JIT_PROVISION"
    DENIED = "DENIED"


class IdentityDenialReason(StrEnum):
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    EMAIL_DOMAIN_NOT_ALLOWED = "EMAIL_DOMAIN_NOT_ALLOWED"
    LINK_BY_EMAIL_MISSING = "LINK_BY_EMAIL_MISSING"
    JIT_PROVISIONING_DISABLED = "JIT_PROVISIONING_DISABLED"
    CONNECTION_NOT_ACTIVE = "CONNECTION_NOT_ACTIVE"


@dataclass(frozen=True, slots=True)
class SsoConnectionView:
    """Read projection of ``platform.tenant_sso_connection`` used by the pure policy layer."""

    id: str
    tenant_id: str
    tenant_code: str
    tenant_name: str
    protocol: SsoProtocol
    status: SsoConnectionStatus
    dex_issuer_url: str
    client_id: str
    email_domains: tuple[str, ...] = ()
    is_enforced: bool = False
    jit_provisioning: bool = False
    default_role_keys: tuple[str, ...] = ()
    group_claim_name: str = "groups"
    name: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.tenant_id or not self.tenant_code:
            raise SsoConnectionInvalid("SSO connection requires id, tenant id and tenant code")
        if not self.dex_issuer_url.startswith(("https://", "http://")):
            raise SsoConnectionInvalid("SSO connection issuer must be an http(s) URL")
        object.__setattr__(self, "email_domains", normalize_email_domains(self.email_domains))

    @property
    def is_active(self) -> bool:
        return self.status is SsoConnectionStatus.ACTIVE

    @property
    def login_url(self) -> str:
        return self.dex_issuer_url.rstrip("/") + "/auth"

    def describes(self) -> dict[str, Any]:
        """Public routing information only (no secrets/business data)."""
        return {
            "connection_id": self.id,
            "tenant_id": self.tenant_id,
            "tenant_code": self.tenant_code,
            "tenant_name": self.tenant_name,
            "protocol": self.protocol.value,
            "login_url": self.login_url,
        }


@dataclass(frozen=True, slots=True)
class LoginRouting:
    local_allowed: bool
    sso_choices: tuple[SsoConnectionView, ...] = ()
    requires_tenant_choice: bool = False
    selected_connection_id: str | None = None

    @property
    def methods(self) -> tuple[LoginMethod, ...]:
        methods: list[LoginMethod] = []
        if self.local_allowed:
            methods.append(LoginMethod.LOCAL)
        if self.sso_choices:
            methods.append(LoginMethod.SSO)
        return tuple(methods)

    def describe(self) -> dict[str, Any]:
        return {
            "methods": [method.value for method in self.methods],
            "local_allowed": self.local_allowed,
            "connections": [connection.describes() for connection in self.sso_choices],
            "requires_tenant_choice": self.requires_tenant_choice,
        }


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    kind: IdentityResolutionKind
    identity_id: str | None = None
    create_identity: bool = False
    create_membership: bool = False
    link_required: bool = True
    denial_reason: IdentityDenialReason | None = None

    @property
    def allowed(self) -> bool:
        return self.kind is not IdentityResolutionKind.DENIED


@dataclass(frozen=True, slots=True)
class GroupMapping:
    match_type: str
    group_value: str
    role_key: str
    priority: int = 100
    is_active: bool = True
    mapping_id: str = ""

    def __post_init__(self) -> None:
        match_type = self.match_type.upper()
        if match_type not in {_EXACT_MATCH, _REGEX_MATCH}:
            raise SsoGroupMappingInvalid(f"invalid group mapping match_type {self.match_type!r}")
        object.__setattr__(self, "match_type", match_type)
        if not self.group_value or not self.role_key:
            raise SsoGroupMappingInvalid("group mapping requires group_value and role_key")
        if self.priority < 0:
            raise SsoGroupMappingInvalid("group mapping priority must be >= 0")
        if match_type == _REGEX_MATCH:
            try:
                re.compile(self.group_value)
            except re.error as exc:
                raise SsoGroupMappingInvalid(f"invalid group mapping regex: {exc}") from exc

    def matches(self, groups: Iterable[str]) -> bool:
        group_list = list(groups)
        if self.match_type == _EXACT_MATCH:
            return self.group_value in group_list
        pattern = re.compile(self.group_value)
        return any(pattern.search(group) for group in group_list)


@dataclass(frozen=True, slots=True)
class GroupMappingResult:
    role_keys: tuple[str, ...] = ()
    scopes: frozenset[str] = frozenset()
    matched_mapping_ids: tuple[str, ...] = ()
    break_glass: bool = False

    @property
    def has_roles(self) -> bool:
        return bool(self.role_keys)


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    connection_id: str
    tenant_id: str
    issuer: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    created_at: datetime
    expires_at: datetime
    used: bool = False

    def is_expired(self, now: datetime) -> bool:
        return ensure_utc(now) >= ensure_utc(self.expires_at)


@dataclass(frozen=True, slots=True)
class IdTokenClaims:
    issuer: str
    subject: str
    audience: tuple[str, ...]
    email: str
    email_verified: bool
    issued_at: datetime
    expires_at: datetime
    nonce: str
    groups: tuple[str, ...] = ()


# --------------------------------------------------------------------------- helpers -----------


def normalize_email(email: str) -> str:
    normalised = str(email or "").strip().lower()
    if not _EMAIL_PATTERN.match(normalised):
        raise SsoConnectionInvalid(f"invalid email address {email!r}")
    return normalised


def email_domain(email: str) -> str:
    return normalize_email(email).rsplit("@", 1)[1]


def normalize_email_domains(domains: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({domain.strip().lower() for domain in domains if domain.strip()}))


def domain_allowed(email: str, connection: SsoConnectionView, *, allow_any_domain: bool = False) -> bool:
    """Exact (case-insensitive) email-domain membership of the connection's allowed domains."""
    if allow_any_domain:
        return True
    return email_domain(email) in connection.email_domains


def ensure_email_domain_allowed(
    email: str,
    connection: SsoConnectionView,
    *,
    allow_any_domain: bool = False,
) -> None:
    if not domain_allowed(email, connection, allow_any_domain=allow_any_domain):
        raise SsoEmailDomainNotAllowed(
            "email domain is not allowed for this SSO connection",
            details={"connection_id": connection.id},
        )


def validate_connection_usable(connection: SsoConnectionView) -> None:
    """A connection may start a login only when ACTIVE (DRAFT/TESTING/DISABLED/ERROR are rejected)."""
    if not connection.is_active:
        raise SsoDisabled(f"SSO connection is {connection.status.value.lower()}", details={"connection_id": connection.id})


def local_login_allowed(
    connection: SsoConnectionView | None,
    *,
    is_platform_admin: bool = False,
    break_glass_enabled: bool = True,
) -> bool:
    """Enforced-SSO tenants reject local password logins for their members (audited break-glass exempt)."""
    if connection is None or not connection.is_active or not connection.is_enforced:
        return True
    return bool(is_platform_admin and break_glass_enabled)


def enforce_local_login_allowed(
    connection: SsoConnectionView | None,
    *,
    is_platform_admin: bool = False,
    break_glass_enabled: bool = True,
) -> None:
    if not local_login_allowed(connection, is_platform_admin=is_platform_admin, break_glass_enabled=break_glass_enabled):
        raise SsoEnforcedLocalDenied(
            "this tenant enforces SSO; local password login is disabled for its members",
            details={"connection_id": connection.id if connection else None},
        )


def find_connections(identifier: str, connections: Iterable[SsoConnectionView]) -> tuple[SsoConnectionView, ...]:
    """Match ACTIVE connections by tenant code or verified email domain (routing/discovery helper)."""
    candidate = (identifier or "").strip().lower()
    if not candidate:
        return ()
    matches: list[SsoConnectionView] = []
    for connection in connections:
        if not connection.is_active:
            continue
        if candidate == connection.tenant_code.lower():
            matches.append(connection)
            continue
        if "@" in candidate:
            try:
                if email_domain(candidate) in connection.email_domains:
                    matches.append(connection)
            except SsoConnectionInvalid:
                continue
    return tuple(sorted(matches, key=lambda item: item.tenant_code.lower()))


def resolve_login_routing(
    identifier: str,
    *,
    connections: Iterable[SsoConnectionView],
    local_login_enabled: bool = True,
    platform_admin_break_glass: bool = False,
) -> LoginRouting:
    """Discovery/start routing: one tenant → direct SSO; several → explicit tenant choice required."""
    matches = find_connections(identifier, connections)
    enforced = any(connection.is_enforced for connection in matches)
    local_ok = local_login_enabled and (platform_admin_break_glass or not enforced)
    if not matches:
        return LoginRouting(local_allowed=local_ok)
    if len(matches) == 1:
        return LoginRouting(
            local_allowed=local_ok,
            sso_choices=matches,
            selected_connection_id=matches[0].id,
        )
    return LoginRouting(local_allowed=False, sso_choices=matches, requires_tenant_choice=True)


def resolve_connection_for_start(
    identifier: str,
    *,
    connections: Iterable[SsoConnectionView],
    requested_tenant_id: str | None = None,
) -> SsoConnectionView:
    """Resolve the single connection for ``/auth/sso/start`` (ambiguity requires an explicit tenant)."""
    matches = find_connections(identifier, connections)
    if requested_tenant_id:
        matches = tuple(connection for connection in matches if connection.tenant_id == requested_tenant_id)
    if not matches:
        raise SsoProviderNotFound("no SSO provider matches the login identifier")
    if len(matches) > 1:
        raise SsoConnectionInvalid(
            "multiple SSO providers match the login identifier; supply the tenant code",
            details={"connection_ids": [connection.id for connection in matches]},
        )
    connection = matches[0]
    validate_connection_usable(connection)
    return connection


# --------------------------------------------------------------------------- JIT/link ----------


def decide_identity_resolution(
    *,
    existing_link_identity_id: str | None = None,
    linked_member_identity_id: str | None = None,
    identity_exists: bool = False,
    email_verified: bool,
    domain_allowed: bool,
    jit_provisioning: bool,
    link_by_verified_email: bool = True,
) -> IdentityResolution:
    """Pure identity-resolution decision for the SSO callback (link → verified email → JIT → deny)."""
    if existing_link_identity_id:
        return IdentityResolution(kind=IdentityResolutionKind.LINKED, identity_id=existing_link_identity_id, link_required=False)
    if not email_verified:
        return IdentityResolution(
            kind=IdentityResolutionKind.DENIED,
            denial_reason=IdentityDenialReason.EMAIL_NOT_VERIFIED,
            link_required=False,
        )
    if not domain_allowed:
        return IdentityResolution(
            kind=IdentityResolutionKind.DENIED,
            denial_reason=IdentityDenialReason.EMAIL_DOMAIN_NOT_ALLOWED,
            link_required=False,
        )
    if link_by_verified_email and linked_member_identity_id:
        return IdentityResolution(
            kind=IdentityResolutionKind.LINK_BY_VERIFIED_EMAIL,
            identity_id=linked_member_identity_id,
        )
    if jit_provisioning:
        return IdentityResolution(
            kind=IdentityResolutionKind.JIT_PROVISION,
            identity_id=linked_member_identity_id,
            create_identity=not identity_exists and linked_member_identity_id is None,
            create_membership=True,
        )
    return IdentityResolution(
        kind=IdentityResolutionKind.DENIED,
        denial_reason=IdentityDenialReason.JIT_PROVISIONING_DISABLED,
        link_required=False,
    )


def require_identity_resolution_allowed(resolution: IdentityResolution) -> None:
    if resolution.allowed:
        return
    if resolution.denial_reason is IdentityDenialReason.JIT_PROVISIONING_DISABLED:
        raise JitProvisioningDisabled("JIT provisioning is disabled for this SSO connection; use an invitation")
    raise SsoEmailDomainNotAllowed("identity cannot be resolved from this SSO assertion")


# --------------------------------------------------------------------------- PKCE/state --------


def generate_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def s256_code_challenge(verifier: str) -> str:
    if not PKCE_VERIFIER_MIN_LENGTH <= len(verifier) <= PKCE_VERIFIER_MAX_LENGTH:
        raise SsoConnectionInvalid("PKCE verifier length must be 43–128 characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_authorization_request(
    connection: SsoConnectionView,
    *,
    redirect_uri: str,
    redirect_allowlist: Sequence[str],
    now: datetime | None = None,
    ttl_seconds: int = SSO_STATE_TTL_SECONDS,
    state: str | None = None,
    nonce: str | None = None,
    code_verifier: str | None = None,
) -> AuthorizationRequest:
    """Build a single-use, tenant-bound authorization request (state + nonce + PKCE S256)."""
    validate_connection_usable(connection)
    if redirect_uri not in set(redirect_allowlist):
        raise SsoConnectionInvalid("redirect URI is not allowlisted", details={"redirect_uri": redirect_uri})
    verifier = code_verifier or generate_pkce_verifier()
    if state is not None and len(state) < 16:
        raise SsoConnectionInvalid("state must be at least 16 characters")
    issued_at = ensure_utc(now or utc_now())
    return AuthorizationRequest(
        connection_id=connection.id,
        tenant_id=connection.tenant_id,
        issuer=connection.dex_issuer_url.rstrip("/"),
        state=state or secrets.token_urlsafe(32),
        nonce=nonce or secrets.token_urlsafe(32),
        code_verifier=verifier,
        code_challenge=s256_code_challenge(verifier),
        redirect_uri=redirect_uri,
        created_at=issued_at,
        expires_at=issued_at + _ttl(ttl_seconds),
    )


def _ttl(seconds: int) -> timedelta:
    if seconds <= 0:
        raise ValueError("ttl seconds must be positive")
    return timedelta(seconds=seconds)


def authorization_url(request: AuthorizationRequest, connection: SsoConnectionView) -> str:
    """Dex authorization URL for this request (Dex broker, per-connection issuer)."""
    params = {
        "client_id": connection.client_id,
        "response_type": "code",
        "redirect_uri": request.redirect_uri,
        "scope": "openid email profile groups",
        "state": request.state,
        "nonce": request.nonce,
        "code_challenge": request.code_challenge,
        "code_challenge_method": "S256",
    }
    return connection.dex_issuer_url.rstrip("/") + "/auth?" + urlencode(params)


def validate_callback_state(
    request: AuthorizationRequest,
    *,
    state: str,
    now: datetime | None = None,
) -> AuthorizationRequest:
    """Validate the callback state exactly once (single-use, tenant/redirect-bound) and mark it used."""
    current = ensure_utc(now or utc_now())
    if request.used:
        raise SsoStateInvalid("authorization state was already used (replay)", details={"reason": "STATE_REPLAY"})
    if request.is_expired(current):
        raise SsoStateInvalid("authorization state has expired", details={"reason": "STATE_EXPIRED"})
    if not secrets.compare_digest(request.state, state or ""):
        raise SsoStateInvalid("authorization state does not match", details={"reason": "STATE_MISMATCH"})
    return replace(request, used=True)


# --------------------------------------------------------------------------- ID token ----------


def validate_id_token_claims(
    claims: Mapping[str, Any],
    *,
    expected_issuer: str,
    client_id: str,
    expected_nonce: str,
    now: datetime | None = None,
    leeway_seconds: int = ID_TOKEN_LEEWAY_SECONDS,
    require_email_verified: bool = True,
    group_claim_name: str = "groups",
) -> IdTokenClaims:
    """Validate Dex ID-token claims (signature/JWKS verification is injected by the API layer)."""
    current = ensure_utc(now or utc_now())
    issuer = _claim_str(claims, "iss")
    if issuer.rstrip("/") != expected_issuer.rstrip("/"):
        raise SsoIdTokenInvalid("ID token issuer does not match the SSO connection", details={"reason": "ISSUER_MISMATCH"})
    audience_raw = claims.get("aud")
    audiences = (audience_raw,) if isinstance(audience_raw, str) else tuple(audience_raw or ())
    if client_id not in audiences:
        raise SsoIdTokenInvalid("ID token audience does not match the platform client", details={"reason": "AUDIENCE_MISMATCH"})
    subject = _claim_str(claims, "sub")
    nonce = _claim_str(claims, "nonce")
    if not secrets.compare_digest(nonce, expected_nonce):
        raise SsoIdTokenInvalid("ID token nonce does not match the authorization request", details={"reason": "NONCE_MISMATCH"})
    expires_at = _claim_datetime(claims, "exp")
    issued_at = _claim_datetime(claims, "iat")
    if current.timestamp() > expires_at.timestamp() + leeway_seconds:
        raise SsoIdTokenInvalid("ID token has expired", details={"reason": "TOKEN_EXPIRED"})
    if issued_at.timestamp() > current.timestamp() + leeway_seconds:
        raise SsoIdTokenInvalid("ID token was issued in the future", details={"reason": "IAT_IN_FUTURE"})
    email = str(claims.get("email", "")).strip().lower()
    if not email:
        raise SsoIdTokenInvalid("ID token is missing an email claim", details={"reason": "EMAIL_MISSING"})
    email_verified = bool(claims.get("email_verified", False))
    if require_email_verified and not email_verified:
        raise SsoIdTokenInvalid("ID token email is not verified", details={"reason": "EMAIL_NOT_VERIFIED"})
    groups_raw = claims.get(group_claim_name, claims.get("groups", ()))
    groups = tuple(str(group) for group in groups_raw) if isinstance(groups_raw, list | tuple) else ()
    return IdTokenClaims(
        issuer=issuer,
        subject=subject,
        audience=audiences,
        email=email,
        email_verified=email_verified,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        groups=groups,
    )


def _claim_str(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise SsoIdTokenInvalid(f"ID token claim {name!r} is missing", details={"reason": f"{name.upper()}_MISSING"})
    return value


def _claim_datetime(claims: Mapping[str, Any], name: str) -> datetime:
    value = claims.get(name)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SsoIdTokenInvalid(f"ID token claim {name!r} is missing", details={"reason": f"{name.upper()}_MISSING"})
    return datetime.fromtimestamp(float(value), tz=UTC)


# --------------------------------------------------------------------------- group mapping ----


def validate_group_mappings(
    mappings: Iterable[GroupMapping],
    *,
    known_role_keys: Iterable[str],
    assignable_role_keys: Iterable[str],
    max_mappings: int = 200,
) -> tuple[GroupMapping, ...]:
    """Validate the connection's mapping configuration (write-time validation; raises 422)."""
    known = set(known_role_keys)
    assignable = set(assignable_role_keys)
    validated: list[GroupMapping] = []
    seen: set[tuple[str, str, str]] = set()
    for mapping in mappings:
        if mapping.role_key not in known:
            raise SsoGroupMappingInvalid(f"group mapping references unknown role {mapping.role_key!r}")
        if mapping.role_key not in assignable:
            raise SsoGroupMappingInvalid(f"group mapping role {mapping.role_key!r} is not assignable")
        key = (mapping.match_type, mapping.group_value, mapping.role_key)
        if key in seen:
            raise SsoGroupMappingInvalid("duplicate group mapping")
        seen.add(key)
        validated.append(mapping)
    if len(validated) > max_mappings:
        raise SsoGroupMappingInvalid(f"too many group mappings (max {max_mappings})")
    return tuple(validated)


def evaluate_group_role_mappings(
    groups: Iterable[str],
    mappings: Iterable[GroupMapping],
    *,
    role_scope_sets: Mapping[str, frozenset[str]],
    assignable_role_keys: Iterable[str],
    sod_pairs: Iterable[tuple[str, str]] = (),
    allow_break_glass: bool = True,
) -> GroupMappingResult:
    """Map IdP groups → tenant roles, bounded by RBAC assignability and SoD (DM-SSO-03/RULE-SSO-05)."""
    group_list = tuple(dict.fromkeys(str(group) for group in groups))
    assignable = set(assignable_role_keys)
    ordered = sorted(
        (mapping for mapping in mappings if mapping.is_active),
        key=lambda mapping: (mapping.priority, mapping.mapping_id or "", mapping.role_key),
    )
    role_keys: list[str] = []
    matched_ids: list[str] = []
    for mapping in ordered:
        if not mapping.matches(group_list):
            continue
        if mapping.role_key not in assignable:
            raise SsoGroupMappingInvalid(f"group mapping role {mapping.role_key!r} is not assignable")
        if mapping.role_key not in role_keys:
            role_keys.append(mapping.role_key)
        matched_ids.append(mapping.mapping_id or f"{mapping.match_type}:{mapping.group_value}->{mapping.role_key}")
    scopes: set[str] = set()
    for role_key in role_keys:
        scopes.update(role_scope_sets.get(role_key, frozenset()))
    break_glass = ADMIN_BREAK_GLASS_ROLE in role_keys
    if not break_glass:
        for first, second in sod_pairs:
            if first in scopes and second in scopes:
                raise SodConflict(
                    "group mapping would grant a separation-of-duties pair",
                    details={"roles": role_keys, "pair": [first, second]},
                )
    return GroupMappingResult(
        role_keys=tuple(role_keys),
        scopes=frozenset(scopes),
        matched_mapping_ids=tuple(matched_ids),
        break_glass=break_glass,
    )


def evaluate_group_mappings_with_registry(
    groups: Iterable[str],
    mappings: Iterable[GroupMapping],
    *,
    registry: ScopeRegistry,
    custom_roles: Mapping[str, Sequence[str]] | None = None,
    allow_break_glass: bool = True,
) -> GroupMappingResult:
    """Convenience wrapper that derives role scopes/assignability from the RBAC catalogue."""
    templates = registry.catalogue.templates_by_key
    role_scope_sets = {key: template.scopes for key, template in templates.items()}
    for role_key, scopes in (custom_roles or {}).items():
        role_scope_sets[role_key] = frozenset(registry.catalogue.canonical(scope) for scope in scopes)
    assignable_roles = tuple(role_scope_sets.keys())
    validated = validate_group_mappings(mappings, known_role_keys=role_scope_sets, assignable_role_keys=assignable_roles)
    result = evaluate_group_role_mappings(
        groups,
        validated,
        role_scope_sets=role_scope_sets,
        assignable_role_keys=assignable_roles,
        sod_pairs=((pair.a, pair.b) for pair in registry.catalogue.sod_pairs),
        allow_break_glass=allow_break_glass,
    )
    if not result.break_glass and result.scopes:
        registry.require_no_sod_conflict(
            result.scopes,
            context=SodContext.ASSIGNMENT,
            role_keys=result.role_keys,
            allow_break_glass=allow_break_glass,
        )
    return result


# --------------------------------------------------------------------------- ports -------------


class SsoConnectionLookup(Protocol):
    """Connection lookup port (DB adapter lives in the repository layer; this module stays pure)."""

    def by_id(self, connection_id: str) -> SsoConnectionView | None: ...

    def by_tenant_code(self, tenant_code: str) -> tuple[SsoConnectionView, ...]: ...

    def by_email_domain(self, domain: str) -> tuple[SsoConnectionView, ...]: ...


class DexMetadataProvider(Protocol):
    """Dex OIDC metadata/JWKS port (network I/O is implemented outside this pure module)."""

    def discovery_url(self, connection: SsoConnectionView) -> str: ...

    def jwks(self, connection: SsoConnectionView) -> Mapping[str, Any]: ...


class InMemoryConnectionLookup:
    """Deterministic lookup implementation for unit tests and route-handler fakes."""

    def __init__(self, connections: Iterable[SsoConnectionView] = ()) -> None:
        self._connections = tuple(connections)

    def by_id(self, connection_id: str) -> SsoConnectionView | None:
        return next((connection for connection in self._connections if connection.id == connection_id), None)

    def by_tenant_code(self, tenant_code: str) -> tuple[SsoConnectionView, ...]:
        code = tenant_code.strip().lower()
        return tuple(connection for connection in self._connections if connection.tenant_code.lower() == code)

    def by_email_domain(self, domain: str) -> tuple[SsoConnectionView, ...]:
        candidate = domain.strip().lower()
        return tuple(connection for connection in self._connections if candidate in connection.email_domains)

    @property
    def connections(self) -> tuple[SsoConnectionView, ...]:
        return self._connections


def discovery_url(connection: SsoConnectionView, *, metadata_provider: DexMetadataProvider | None = None) -> str:
    if metadata_provider is not None:
        return metadata_provider.discovery_url(connection)
    return connection.dex_issuer_url.rstrip("/") + "/.well-known/openid-configuration"


__all__ = [
    "AuthorizationRequest",
    "DexMetadataProvider",
    "GroupMapping",
    "GroupMappingResult",
    "ID_TOKEN_LEEWAY_SECONDS",
    "IdTokenClaims",
    "IdentityDenialReason",
    "IdentityResolution",
    "IdentityResolutionKind",
    "InMemoryConnectionLookup",
    "LoginMethod",
    "LoginRouting",
    "PKCE_VERIFIER_MAX_LENGTH",
    "PKCE_VERIFIER_MIN_LENGTH",
    "SSO_STATE_TTL_SECONDS",
    "SsoConnectionLookup",
    "SsoConnectionStatus",
    "SsoConnectionView",
    "SsoProtocol",
    "authorization_url",
    "create_authorization_request",
    "decide_identity_resolution",
    "discovery_url",
    "domain_allowed",
    "email_domain",
    "enforce_local_login_allowed",
    "ensure_email_domain_allowed",
    "evaluate_group_mappings_with_registry",
    "evaluate_group_role_mappings",
    "find_connections",
    "generate_pkce_verifier",
    "local_login_allowed",
    "normalize_email",
    "normalize_email_domains",
    "resolve_connection_for_start",
    "resolve_login_routing",
    "require_identity_resolution_allowed",
    "s256_code_challenge",
    "validate_callback_state",
    "validate_connection_usable",
    "validate_group_mappings",
    "validate_id_token_claims",
]
