"""FS-00 local authentication/session endpoints (Δ-MT + Δ-SSO discovery).

Public/self-service surface implemented here (wired by the integrator through ``router``):

* ``POST /api/v1/auth/login`` — local password login (Argon2id, rehash-on-login); single membership →
  token pair, several → ``TENANT_SELECTION_REQUIRED`` + identity-scoped selection token, none →
  ``NO_ACTIVE_MEMBERSHIP``. Optional ``tenant`` selects a tenant id/code explicitly.
* ``POST /api/v1/auth/select-tenant`` — exchange selection token + tenant for a token pair.
* ``POST /api/v1/auth/refresh`` — rotate the refresh token; replay revokes the session.
* ``POST /api/v1/auth/logout`` — revoke the presented session (and optionally all sessions of the principal).
* ``GET /api/v1/auth/providers`` — discover LOCAL/SSO methods for an email/tenant (no secrets).
* ``GET /api/v1/auth/me`` — current identity + membership + live effective scopes/data scopes.

Security notes: tokens are issued by :class:`app.core.security.tokens.TokenService` only; the live
membership/token_version is revalidated per request; refresh digests are stored (never plaintext); replay
is detected through the conditional rotation update and revokes the session. Rate limiting and
Idempotency-Key enforcement are deliberately deferred to the integration window (documented in the
report). Errors use the existing ``problem+json`` handlers (``app.api.errors``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import DbDep, Principal, PrincipalDep, scope_registry, token_service
from app.core.repositories.auth import (
    AuthRepository,
    MembershipRecord,
    SsoConnectionRecord,
    hash_refresh_token,
)
from app.core.security.errors import (
    InvalidCredentials,
    MembershipInactive,
    NoActiveMembership,
    SecurityError,
    SessionRevoked,
    TenantSuspended,
    TokenExpired,
    TokenInvalid,
    TokenVersionStale,
)
from app.core.security.passwords import PasswordService
from app.core.security.scopes import ScopeRegistry
from app.core.security.tokens import TokenPair, TokenService, constant_time_compare

router = APIRouter(tags=["auth"])


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- dependencies -----


async def get_auth_repository(db: DbDep) -> AuthRepository:
    return AuthRepository(db)


@lru_cache(maxsize=1)
def _default_password_service() -> PasswordService:
    return PasswordService()


def get_password_service() -> PasswordService:
    return _default_password_service()


async def get_optional_principal(
    tokens: Annotated[TokenService, Depends(token_service)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Best-effort bearer principal (used by logout when the access token is still valid)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        claims = tokens.decode_access_token(token)
    except SecurityError:
        return None
    return Principal(
        identity_id=claims.sub,
        app_user_id=claims.uid,
        tenant_id=claims.tid,
        token_version=claims.tv,
        scopes=frozenset(claims.scopes),
    )


RepoDep = Annotated[AuthRepository, Depends(get_auth_repository)]
TokenSvcDep = Annotated[TokenService, Depends(token_service)]
ScopeRegistryDep = Annotated[ScopeRegistry, Depends(scope_registry)]
PasswordSvcDep = Annotated[PasswordService, Depends(get_password_service)]
OptionalPrincipalDep = Annotated[Principal | None, Depends(get_optional_principal)]


# --------------------------------------------------------------------------- schemas ----------


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    tenant: str | None = Field(default=None, description="Optional tenant id or code")


class SelectTenantRequest(BaseModel):
    selection_token: str = Field(min_length=8)
    tenant_id: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(default=None, description="Session refresh token to revoke")
    all_sessions: bool = Field(default=False, description="Revoke every session of the current principal")


class TenantInfo(BaseModel):
    id: str
    code: str
    name: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    tenant: TenantInfo


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class TenantChoice(BaseModel):
    tenant_id: str
    code: str
    name: str
    role_summary: list[str]


class SelectionRequiredResponse(BaseModel):
    status: Literal["TENANT_SELECTION_REQUIRED"] = "TENANT_SELECTION_REQUIRED"
    selection_token: str
    expires_in: int
    tenant_choices: list[TenantChoice]


class SsoConnectionInfo(BaseModel):
    connection_id: str
    tenant_id: str
    tenant_code: str
    tenant_name: str
    protocol: str
    login_url: str


class ProvidersResponse(BaseModel):
    methods: list[Literal["LOCAL", "SSO"]]
    local_allowed: bool
    connections: list[SsoConnectionInfo]


class MeIdentity(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    is_platform_admin: bool = False
    mfa_enabled: bool = False


class MeTenant(BaseModel):
    id: str
    code: str
    name: str
    status: str


class MeMembership(BaseModel):
    app_user_id: str
    status: str
    membership_status: str
    token_version: int
    roles: list[str]
    joined_at: datetime | None = None
    last_login_at: datetime | None = None


class MePermissions(BaseModel):
    scopes: list[str]
    data_scopes: dict[str, list[str]]


class MeResponse(BaseModel):
    identity: MeIdentity
    tenant: MeTenant
    membership: MeMembership
    permissions: MePermissions


class InvitationAcceptRequest(BaseModel):
    password: str | None = Field(default=None, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)


class InvitationAcceptResponse(BaseModel):
    app_user_id: str
    identity_id: str
    tenant_id: str
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int




@router.post(
    "/api/v1/admin/invitations/{token}/accept",
    response_model=InvitationAcceptResponse,
    status_code=201,
    summary="Accept an invitation (public; single-use token, creates identity/membership/roles atomically)",
)
async def accept_invitation(
    token: str,
    payload: InvitationAcceptRequest,
    request: Request,
    repository: RepoDep,
    tokens: TokenSvcDep,
    passwords: PasswordSvcDep,
) -> InvitationAcceptResponse:
    invitation = await repository.get_invitation_by_token_hash(_hash_invitation_token(token))
    if invitation is None:
        raise _request_error("invitation token is invalid", code="INVITATION_TOKEN_INVALID", status=400)
    if invitation.status == "ACCEPTED":
        raise _request_error("invitation was already used", code="INVITATION_ALREADY_USED", status=409)
    if invitation.status == "REVOKED":
        raise _request_error("invitation was revoked", code="INVITATION_REVOKED", status=409)
    now = utc_now()
    if invitation.status == "EXPIRED" or invitation.expires_at <= now:
        await repository.expire_invitation(invitation.id, now=now)
        raise _request_error("invitation has expired", code="INVITATION_EXPIRED", status=410)

    tenant = await repository.get_tenant(invitation.tenant_id)
    if tenant is None:
        raise NoActiveMembership("invitation tenant no longer exists")
    if tenant.status != "ACTIVE":
        raise TenantSuspended("tenant is not active")

    identity = await repository.get_identity_by_email(invitation.email)
    if identity is not None and identity.status != "ACTIVE":
        raise MembershipInactive("identity is not active; contact the tenant administrator")
    if identity is None:
        if not payload.password:
            raise _request_error("password is required to create the invited identity", code="INVITATION_PASSWORD_REQUIRED", status=422)
        try:
            password_hash = passwords.hash(payload.password)
        except InvalidCredentials as exc:
            raise _request_error("password does not meet the policy", code="INVITATION_PASSWORD_INVALID", status=422) from exc
        identity_id = await repository.create_identity(
            email=invitation.email,
            display_name=payload.display_name,
            password_hash=password_hash,
        )
    else:
        identity_id = identity.id
        if payload.display_name:
            await repository.update_identity_display_name(identity_id, payload.display_name)
        if payload.password and identity.password_hash is None:
            try:
                await repository.update_identity_password_hash(identity_id, passwords.hash(payload.password))
            except InvalidCredentials as exc:
                raise _request_error("password does not meet the policy", code="INVITATION_PASSWORD_INVALID", status=422) from exc

    role_ids = list(dict.fromkeys(invitation.intended_role_ids))
    roles = await repository.get_roles_by_ids(tenant.id, role_ids)
    if len(roles) != len(role_ids):
        raise _request_error("invitation contains roles that are not assignable", code="INVITATION_ROLE_INVALID", status=422)
    app_user_id = await repository.ensure_membership_for_invitation(
        tenant_id=tenant.id,
        identity_id=identity_id,
        email=invitation.email,
        display_name=payload.display_name,
        role_ids=role_ids,
    )
    accepted = await repository.accept_invitation(invitation.id, app_user_id=app_user_id, now=now)
    if not accepted:
        raise _request_error("invitation is no longer available", code="INVITATION_ALREADY_USED", status=409)
    membership = await repository.get_membership(tenant.id, app_user_id)
    if membership is None:
        raise _request_error("membership could not be resolved", code="INVITATION_ACCEPT_FAILED", status=500)
    pair = await _issue_token_pair(
        repository,
        tokens,
        identity_id=identity_id,
        membership=membership,
        request=request,
        touch_last_login=True,
        now=now,
    )
    return InvitationAcceptResponse(
        app_user_id=str(app_user_id),
        identity_id=str(identity_id),
        tenant_id=str(tenant.id),
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


# --------------------------------------------------------------------------- helpers ----------


def _hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_error(message: str, *, code: str, status: int) -> SecurityError:
    error = SecurityError(message, code=code)
    error.http_status = status
    return error


def _as_uuid(value: str | UUID, *, field: str = "id") -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _request_error(f"invalid {field}", code=f"INVALID_{field.upper()}", status=422) from exc


def _tenant_info(membership: MembershipRecord) -> TenantInfo:
    return TenantInfo(id=str(membership.tenant_id), code=membership.tenant_code, name=membership.tenant_name)


def _check_membership_loginable(membership: MembershipRecord) -> None:
    """Ordered status failures for a chosen tenant (no information about other tenants)."""
    if membership.status != "ACTIVE" or membership.membership_status != "ACTIVE":
        raise MembershipInactive("membership is not active")
    if membership.tenant_status != "ACTIVE":
        raise TenantSuspended("tenant is not active")


def _find_membership(memberships: Iterable[MembershipRecord], selector: str) -> MembershipRecord | None:
    candidate = selector.strip()
    lowered = candidate.lower()
    for membership in memberships:
        if str(membership.tenant_id) == candidate or membership.tenant_code.lower() == lowered:
            return membership
    return None


def _raise_no_eligible_membership(memberships: Iterable[MembershipRecord]) -> None:
    entries = list(memberships)
    if any(entry.status != "ACTIVE" or entry.membership_status != "ACTIVE" for entry in entries):
        raise MembershipInactive("membership is not active")
    if any(entry.tenant_status != "ACTIVE" for entry in entries):
        raise TenantSuspended("tenant is not active")
    raise NoActiveMembership("identity has no active tenant membership")


def _request_metadata(request: Request) -> tuple[str | None, str | None]:
    client = request.client
    ip = client.host if client else None
    return ip, request.headers.get("user-agent")


async def _issue_token_pair(
    repository: AuthRepository,
    tokens: TokenService,
    *,
    identity_id: UUID,
    membership: MembershipRecord,
    request: Request,
    touch_last_login: bool = True,
    persist_session: bool = True,
    session_id: UUID | None = None,
    now: datetime | None = None,
) -> TokenPair:
    issued_at = now or utc_now()
    sid = session_id or uuid.uuid4()
    pair = tokens.issue_token_pair(
        identity_id=str(identity_id),
        app_user_id=str(membership.app_user_id),
        tenant_id=str(membership.tenant_id),
        token_version=membership.token_version,
        scopes=membership.scopes,
        session_id=str(sid),
        now=issued_at,
    )
    ip, user_agent = _request_metadata(request)
    if persist_session:
        await repository.create_session(
            session_id=sid,
            tenant_id=membership.tenant_id,
            app_user_id=membership.app_user_id,
            identity_id=identity_id,
            refresh_token_hash=hash_refresh_token(pair.refresh_token),
            issued_at=pair.refresh_claims.iat,
            expires_at=pair.refresh_claims.exp,
            ip=ip,
            user_agent=user_agent,
        )
    if touch_last_login:
        await repository.touch_last_login(
            identity_id=identity_id,
            tenant_id=membership.tenant_id,
            app_user_id=membership.app_user_id,
            now=issued_at,
        )
    return pair


def _pair_response(pair: TokenPair, membership: MembershipRecord) -> TokenPairResponse:
    return TokenPairResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        tenant=_tenant_info(membership),
    )


def _refresh_response(pair: TokenPair) -> RefreshResponse:
    return RefreshResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


# --------------------------------------------------------------------------- endpoints --------


@router.post(
    "/api/v1/auth/login",
    response_model=TokenPairResponse | SelectionRequiredResponse,
    summary="Local password login (tenant selection when multi-membership)",
)
async def login(
    payload: LoginRequest,
    request: Request,
    repository: RepoDep,
    tokens: TokenSvcDep,
    passwords: PasswordSvcDep,
) -> TokenPairResponse | SelectionRequiredResponse:
    identity = await repository.get_identity_by_email(payload.email)
    if identity is None or identity.status != "ACTIVE":
        # Equalise timing for unknown/disabled identities before the generic failure.
        passwords.verify(payload.password, None)
        raise InvalidCredentials("invalid email or password")
    verification = passwords.verify_and_update(payload.password, identity.password_hash)
    if not verification.valid:
        raise InvalidCredentials("invalid email or password")
    if verification.new_hash:
        await repository.update_identity_password_hash(identity.id, verification.new_hash)

    memberships = await repository.list_memberships(identity.id)
    if payload.tenant:
        selected = _find_membership(memberships, payload.tenant)
        if selected is None:
            raise NoActiveMembership("identity has no membership for the requested tenant")
        _check_membership_loginable(selected)
        pair = await _issue_token_pair(repository, tokens, identity_id=identity.id, membership=selected, request=request)
        return _pair_response(pair, selected)

    eligible = [membership for membership in memberships if membership.is_active]
    if not eligible:
        _raise_no_eligible_membership(memberships)
    if len(eligible) == 1:
        pair = await _issue_token_pair(repository, tokens, identity_id=identity.id, membership=eligible[0], request=request)
        return _pair_response(pair, eligible[0])

    selection_token, claims = tokens.issue_selection_token(identity_id=str(identity.id), now=utc_now())
    choices = [
        TenantChoice(
            tenant_id=str(membership.tenant_id),
            code=membership.tenant_code,
            name=membership.tenant_name,
            role_summary=list(membership.roles),
        )
        for membership in eligible
    ]
    del claims
    return SelectionRequiredResponse(
        selection_token=selection_token,
        expires_in=tokens.config.selection_ttl_seconds,
        tenant_choices=choices,
    )


@router.post(
    "/api/v1/auth/select-tenant",
    response_model=TokenPairResponse,
    summary="Exchange a single-use selection token + tenant for a token pair",
)
async def select_tenant(
    payload: SelectTenantRequest,
    request: Request,
    repository: RepoDep,
    tokens: TokenSvcDep,
) -> TokenPairResponse:
    try:
        claims = tokens.decode_selection_token(payload.selection_token)
    except TokenExpired as exc:
        raise TokenExpired("selection token has expired; sign in again", code="SELECTION_TOKEN_EXPIRED") from exc
    except SecurityError as exc:
        raise TokenInvalid("selection token is invalid; sign in again", code="SELECTION_TOKEN_INVALID") from exc
    try:
        identity_id = _as_uuid(claims.sub, field="identity_id")
        tenant_id = _as_uuid(payload.tenant_id, field="tenant_id")
    except SecurityError as exc:
        raise TokenInvalid("selection token is invalid; sign in again", code="SELECTION_TOKEN_INVALID") from exc
    identity = await repository.get_identity(identity_id)
    if identity is None or identity.status != "ACTIVE":
        raise TokenInvalid("selection token is invalid; sign in again", code="SELECTION_TOKEN_INVALID")
    memberships = await repository.list_memberships(identity.id)
    selected = next((membership for membership in memberships if membership.tenant_id == tenant_id), None)
    if selected is None:
        raise NoActiveMembership("identity has no membership for the requested tenant")
    _check_membership_loginable(selected)
    pair = await _issue_token_pair(repository, tokens, identity_id=identity.id, membership=selected, request=request)
    return _pair_response(pair, selected)


@router.post(
    "/api/v1/auth/refresh",
    response_model=RefreshResponse,
    summary="Rotate the refresh token (replay revokes the session)",
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    repository: RepoDep,
    tokens: TokenSvcDep,
) -> RefreshResponse:
    try:
        claims = tokens.decode_refresh_token(payload.refresh_token)
    except TokenExpired:
        raise
    except SecurityError as exc:
        raise TokenInvalid("refresh token is invalid") from exc
    session = await repository.get_session(_as_uuid(claims.sid, field="session_id"))
    if session is None or session.app_user_id != _as_uuid(claims.uid, field="app_user_id"):
        raise SessionRevoked("session is revoked; sign in again")
    if str(session.tenant_id) != claims.tid or str(session.identity_id) != claims.sub:
        raise TokenInvalid("refresh token does not match its session")
    if not session.is_active():
        await repository.revoke_session(session.id, reason="expired")
        raise SessionRevoked("session is expired or revoked; sign in again")

    presented_hash = hash_refresh_token(payload.refresh_token)
    if not constant_time_compare(presented_hash, session.refresh_token_hash):
        # Reuse of an already-rotated token: revoke the whole session (replay defence).
        await repository.revoke_session(session.id, reason="refresh_replay")
        raise SessionRevoked("refresh token replay detected; session revoked")

    membership = await repository.get_membership(session.tenant_id, session.app_user_id)
    if membership is None:
        await repository.revoke_session(session.id, reason="membership_missing")
        raise SessionRevoked("session is no longer valid; sign in again")
    if membership.token_version != claims.tv:
        raise TokenVersionStale("refresh token version is stale; sign in again")
    _check_membership_loginable(membership)

    pair = await _issue_token_pair(
        repository,
        tokens,
        identity_id=session.identity_id,
        membership=membership,
        request=request,
        touch_last_login=False,
        persist_session=False,
        session_id=session.id,
    )
    rotated = await repository.rotate_session_refresh_hash(
        session.id,
        old_hash=session.refresh_token_hash,
        new_hash=hash_refresh_token(pair.refresh_token),
        issued_at=pair.refresh_claims.iat,
        expires_at=pair.refresh_claims.exp,
    )
    if not rotated:
        await repository.revoke_session(session.id, reason="refresh_replay")
        raise SessionRevoked("refresh token replay detected; session revoked")
    return _refresh_response(pair)


@router.post("/api/v1/auth/logout", status_code=204, summary="Revoke the current session (or all sessions)")
async def logout(
    payload: LogoutRequest,
    repository: RepoDep,
    tokens: TokenSvcDep,
    principal: OptionalPrincipalDep,
) -> Response:
    if not payload.refresh_token and not payload.all_sessions:
        raise _request_error(
            "refresh_token or all_sessions is required",
            code="REFRESH_TOKEN_REQUIRED",
            status=400,
        )
    if payload.refresh_token:
        try:
            claims = tokens.decode_refresh_token(payload.refresh_token)
        except SecurityError:
            claims = None
        if claims is not None:
            session = await repository.get_session(_as_uuid(claims.sid, field="session_id"))
            if session is not None:
                await repository.revoke_session(session.id, reason="logout")
    if payload.all_sessions:
        if principal is None:
            raise TokenInvalid("a valid bearer token is required to revoke all sessions")
        membership = await repository.get_membership(
            _as_uuid(principal.tenant_id, field="tenant_id"),
            _as_uuid(principal.app_user_id, field="app_user_id"),
        )
        if membership is None:
            raise SessionRevoked("session is no longer valid; sign in again")
        if membership.token_version != principal.token_version:
            raise TokenVersionStale("token version is stale; sign in again")
        _check_membership_loginable(membership)
        await repository.revoke_all_sessions(
            tenant_id=membership.tenant_id,
            app_user_id=membership.app_user_id,
            reason="logout_all",
        )
        await repository.bump_token_version(tenant_id=membership.tenant_id, app_user_id=membership.app_user_id)
    return Response(status_code=204)


@router.get(
    "/api/v1/auth/providers",
    response_model=ProvidersResponse,
    summary="Discover enabled authentication methods / SSO routing (public; no secrets)",
)
async def providers(
    repository: RepoDep,
    email_or_tenant: Annotated[str | None, Query(alias="email_or_tenant", max_length=320)] = None,
) -> ProvidersResponse:
    connections = await repository.list_active_sso_connections()
    identifier = (email_or_tenant or "").strip().lower()
    matched: list[SsoConnectionRecord] = []
    if not identifier:
        matched = list(connections)
    else:
        for connection in connections:
            if identifier == connection.tenant_code.lower():
                matched.append(connection)
                continue
            if "@" in identifier:
                domain = identifier.rsplit("@", 1)[1]
                if domain in connection.email_domains:
                    matched.append(connection)
    local_allowed = not any(connection.is_enforced for connection in matched)
    methods: list[Literal["LOCAL", "SSO"]] = []
    if local_allowed:
        methods.append("LOCAL")
    if matched:
        methods.append("SSO")
    return ProvidersResponse(
        methods=methods,
        local_allowed=local_allowed,
        connections=[
            SsoConnectionInfo(
                connection_id=str(connection.id),
                tenant_id=str(connection.tenant_id),
                tenant_code=connection.tenant_code,
                tenant_name=connection.tenant_name,
                protocol=connection.protocol,
                login_url=(connection.dex_issuer_url or "").rstrip("/") + "/auth",
            )
            for connection in matched
        ],
    )


@router.get(
    "/api/v1/auth/me",
    response_model=MeResponse,
    summary="Convenience alias for /api/v1/me (contract path)",
    include_in_schema=False,
)
@router.get("/api/v1/me", response_model=MeResponse, summary="Current identity, membership and effective permissions")
async def me(
    principal: PrincipalDep,
    repository: RepoDep,
) -> MeResponse:
    membership = await _current_membership(repository, principal)
    identity = await repository.get_identity(membership.identity_id)
    if identity is None or identity.status != "ACTIVE":
        raise TokenInvalid("identity is not active; sign in again")
    return MeResponse(
        identity=MeIdentity(
            id=str(identity.id),
            email=identity.email,
            display_name=identity.display_name,
            is_platform_admin=identity.is_platform_admin,
            mfa_enabled=identity.mfa_enabled,
        ),
        tenant=MeTenant(
            id=str(membership.tenant_id),
            code=membership.tenant_code,
            name=membership.tenant_name,
            status=membership.tenant_status,
        ),
        membership=MeMembership(
            app_user_id=str(membership.app_user_id),
            status=membership.status,
            membership_status=membership.membership_status,
            token_version=membership.token_version,
            roles=list(membership.roles),
            joined_at=membership.joined_at,
            last_login_at=membership.last_login_at,
        ),
        permissions=MePermissions(
            scopes=sorted(membership.scopes),
            data_scopes={key: list(values) for key, values in sorted(membership.data_scopes.items())},
        ),
    )




async def _current_membership(repository: AuthRepository, principal: Principal) -> MembershipRecord:
    membership = await repository.get_membership(
        _as_uuid(principal.tenant_id, field="tenant_id"),
        _as_uuid(principal.app_user_id, field="app_user_id"),
    )
    if membership is None:
        raise TokenInvalid("membership no longer exists; sign in again")
    if membership.token_version != principal.token_version:
        raise TokenVersionStale("token version is stale; sign in again")
    if membership.status != "ACTIVE" or membership.membership_status != "ACTIVE":
        raise MembershipInactive("membership is not active")
    if membership.tenant_status != "ACTIVE":
        raise TenantSuspended("tenant is not active")
    return membership


class MeTenantEntry(BaseModel):
    tenant_id: str
    tenant_code: str
    tenant_name: str
    membership_status: str
    roles: list[str]
    is_active: bool


class MeTenantsResponse(BaseModel):
    items: list[MeTenantEntry]


class DataScopeEntry(BaseModel):
    scope_type: str
    values: list[str]


class EffectivePermissionsResponse(BaseModel):
    app_user_id: str
    scopes: list[str]
    data_scopes: list[DataScopeEntry]
    roles: list[str]
    sod_pairs: list[list[str]]


@router.get(
    "/api/v1/me/tenants",
    response_model=MeTenantsResponse,
    summary="Caller's own memberships (tenant code/name, status, role summary)",
)
async def me_tenants(principal: PrincipalDep, repository: RepoDep) -> MeTenantsResponse:
    identity_id = _as_uuid(principal.identity_id, field="identity_id")
    memberships = await repository.list_memberships(identity_id)
    return MeTenantsResponse(
        items=[
            MeTenantEntry(
                tenant_id=str(membership.tenant_id),
                tenant_code=membership.tenant_code,
                tenant_name=membership.tenant_name,
                membership_status=membership.membership_status,
                roles=list(membership.roles),
                is_active=membership.is_active,
            )
            for membership in memberships
        ]
    )


@router.get(
    "/api/v1/me/effective-permissions",
    response_model=EffectivePermissionsResponse,
    summary="Caller's resolved capability/data scopes in the active tenant",
)
async def me_effective_permissions(
    principal: PrincipalDep,
    repository: RepoDep,
    registry: ScopeRegistryDep,
) -> EffectivePermissionsResponse:
    membership = await _current_membership(repository, principal)
    conflicts = registry.sod_conflicts(membership.scopes)
    break_glass = "admin" in membership.roles and len(conflicts) > 0
    return EffectivePermissionsResponse(
        app_user_id=str(membership.app_user_id),
        scopes=sorted(membership.scopes),
        data_scopes=[
            DataScopeEntry(scope_type=scope_type, values=list(values))
            for scope_type, values in sorted(membership.data_scopes.items())
        ],
        roles=list(membership.roles),
        sod_pairs=[] if break_glass else [[pair.a, pair.b] for pair in conflicts],
    )


__all__ = [
    "get_auth_repository",
    "get_password_service",
    "router",
]
