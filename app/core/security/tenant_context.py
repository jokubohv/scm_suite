"""Tenant context resolution and isolation helpers (pure; FS-00 Δ-MT/Δ-SSO).

The **only** source of the active tenant is the verified token claim ``tid`` (RULE-PLAT-12). This module
turns token claims + membership/tenant projections into a :class:`TenantContext`, enforces membership and
tenant status, distinguishes platform-admin vs tenant-admin vs business contexts, resolves multi-membership
login selection, and provides cross-tenant denial helpers that behave like "resource not found" (404).

No DB/network imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.security.errors import (
    CrossTenantAccessDenied,
    MembershipInactive,
    NoActiveMembership,
    PlatformAdminRequired,
    TenantAccessDenied,
    TenantSuspended,
    TokenInvalid,
    TokenVersionStale,
)

SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")
SYSTEM_TENANT_ID_TEXT = str(SYSTEM_TENANT_ID)
PLATFORM_TENANT_ADMIN_SCOPE = "platform.tenant.manage"
TENANT_ADMIN_SCOPE = "platform.admin"
DATA_SCOPE_TYPES = frozenset({"LOCATION", "CLASS", "DEPARTMENT", "SUBSIDIARY"})


class TenantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    OFFBOARDING = "OFFBOARDING"


class MembershipStatus(StrEnum):
    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class AdminClass(StrEnum):
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    TENANT_ADMIN = "TENANT_ADMIN"
    BUSINESS = "BUSINESS"
    SERVICE = "SERVICE"


@dataclass(frozen=True, slots=True)
class TenantView:
    id: str
    code: str
    status: TenantStatus = TenantStatus.ACTIVE
    name: str = ""

    def __post_init__(self) -> None:
        if not str(self.id):
            raise ValueError("tenant id is required")
        if self.code != self.code.strip() or not self.code:
            raise ValueError("tenant code is required and must not contain surrounding whitespace")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TenantView:
        return cls(
            id=str(data["id"]),
            code=str(data["code"]),
            status=TenantStatus(str(data.get("status", TenantStatus.ACTIVE))),
            name=str(data.get("name", "")),
        )


@dataclass(frozen=True, slots=True)
class MembershipView:
    app_user_id: str
    identity_id: str
    tenant_id: str
    status: MembershipStatus = MembershipStatus.ACTIVE
    token_version: int = 0
    roles: tuple[str, ...] = ()
    scopes: frozenset[str] = frozenset()
    is_platform_admin: bool = False
    is_service_account: bool = False

    def __post_init__(self) -> None:
        if self.token_version < 0:
            raise ValueError("token_version must be >= 0")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> MembershipView:
        return cls(
            app_user_id=str(data["app_user_id"]),
            identity_id=str(data["identity_id"]),
            tenant_id=str(data["tenant_id"]),
            status=MembershipStatus(str(data.get("status", MembershipStatus.ACTIVE))),
            token_version=int(data.get("token_version", 0)),
            roles=tuple(str(role) for role in data.get("roles", ())),
            scopes=frozenset(str(scope) for scope in data.get("scopes", ())),
            is_platform_admin=bool(data.get("is_platform_admin", False)),
            is_service_account=bool(data.get("is_service_account", False)),
        )


def _same_id(left: str | UUID, right: str | UUID) -> bool:
    return str(left) == str(right)


def classify_admin(membership: MembershipView, tenant: TenantView) -> AdminClass:
    """Classify the request principal; platform admin only inside the SYSTEM tenant context."""
    if membership.is_platform_admin and _same_id(tenant.id, SYSTEM_TENANT_ID):
        return AdminClass.PLATFORM_ADMIN
    if membership.is_service_account:
        return AdminClass.SERVICE
    if TENANT_ADMIN_SCOPE in membership.scopes or any(role == "admin" for role in membership.roles):
        return AdminClass.TENANT_ADMIN
    return AdminClass.BUSINESS


def effective_data_scopes(
    role_data_scopes: Mapping[str, Sequence[str]] | None = None,
    user_data_scopes: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Merge role-level and membership-level data scopes (user-level wins per dimension)."""
    merged: dict[str, list[str]] = {}
    for source in (role_data_scopes or {}, user_data_scopes or {}):
        for scope_type, values in source.items():
            normalised_type = str(scope_type).upper()
            if normalised_type not in DATA_SCOPE_TYPES:
                raise ValueError(f"unknown data-scope type {scope_type!r}")
            merged[normalised_type] = [str(value) for value in values]
    return {scope_type: tuple(dict.fromkeys(values)) for scope_type, values in merged.items()}


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Resolved request context (tenant + membership + admin class + data scopes)."""

    tenant: TenantView
    membership: MembershipView
    admin_class: AdminClass
    data_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def tenant_id(self) -> str:
        return self.tenant.id

    @property
    def app_user_id(self) -> str:
        return self.membership.app_user_id

    @property
    def identity_id(self) -> str:
        return self.membership.identity_id

    def is_platform_admin(self) -> bool:
        return self.admin_class is AdminClass.PLATFORM_ADMIN

    def is_tenant_admin(self) -> bool:
        return self.admin_class is AdminClass.TENANT_ADMIN

    def can_write(self) -> bool:
        return self.membership.status is MembershipStatus.ACTIVE and self.tenant.status is TenantStatus.ACTIVE

    def describe(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant.id,
            "tenant_code": self.tenant.code,
            "tenant_status": self.tenant.status.value,
            "app_user_id": self.membership.app_user_id,
            "identity_id": self.membership.identity_id,
            "membership_status": self.membership.status.value,
            "admin_class": self.admin_class.value,
            "roles": list(self.membership.roles),
            "data_scopes": {key: list(values) for key, values in self.data_scopes.items()},
        }


def resolve_tenant_context(
    *,
    tenant: TenantView,
    membership: MembershipView,
    token_tenant_id: str | UUID,
    token_app_user_id: str | UUID,
    token_identity_id: str | UUID,
    token_version: int,
    write: bool = False,
    role_data_scopes: Mapping[str, Sequence[str]] | None = None,
    user_data_scopes: Mapping[str, Sequence[str]] | None = None,
    allow_read_only_export: bool = False,
) -> TenantContext:
    """Validate token claims against the current membership/tenant state and build the context.

    Order (fail closed): tenant match → principal match → token version → membership status → tenant status
    (writes require ACTIVE; reads allowed on SUSPENDED only; OFFBOARDING is denied unless explicitly
    continuing an export) → admin class.
    """
    if not _same_id(membership.tenant_id, tenant.id) or not _same_id(token_tenant_id, tenant.id):
        raise CrossTenantAccessDenied("token tenant does not match the resolved membership/tenant")
    if not _same_id(membership.app_user_id, token_app_user_id):
        raise TokenInvalid("token app_user does not match the resolved membership")
    if not _same_id(membership.identity_id, token_identity_id):
        raise TokenInvalid("token identity does not match the resolved membership")
    if int(membership.token_version) != int(token_version):
        raise TokenVersionStale("token version is stale; sign in again")
    if membership.status is not MembershipStatus.ACTIVE:
        raise MembershipInactive(f"membership is {membership.status.value.lower()}")
    if tenant.status is not TenantStatus.ACTIVE:
        if write or (tenant.status is TenantStatus.OFFBOARDING and not allow_read_only_export):
            raise TenantSuspended(f"tenant is {tenant.status.value.lower()}")
    return TenantContext(
        tenant=tenant,
        membership=membership,
        admin_class=classify_admin(membership, tenant),
        data_scopes=effective_data_scopes(role_data_scopes, user_data_scopes),
    )


def ensure_same_tenant(context: TenantContext, resource_tenant_id: str | UUID | None) -> None:
    """Cross-tenant resource access is indistinguishable from not-found (404, no existence leak)."""
    if resource_tenant_id is None:
        return
    if not _same_id(context.tenant.id, resource_tenant_id):
        raise CrossTenantAccessDenied("resource does not exist in this tenant")


def ensure_can_write(context: TenantContext) -> None:
    if not context.can_write():
        if context.tenant.status is not TenantStatus.ACTIVE:
            raise TenantSuspended("tenant is not active; writes are blocked")
        raise MembershipInactive("membership is not active; writes are blocked")


def require_platform_admin(context: TenantContext) -> None:
    """Platform-admin endpoints require the global flag, SYSTEM tenant context and the global scope."""
    if not context.is_platform_admin():
        raise PlatformAdminRequired("a global platform administrator is required")
    if not _same_id(context.tenant.id, SYSTEM_TENANT_ID):
        raise PlatformAdminRequired("platform-admin operations must run in the system tenant context")
    if PLATFORM_TENANT_ADMIN_SCOPE not in context.membership.scopes:
        raise PlatformAdminRequired(f"missing global scope {PLATFORM_TENANT_ADMIN_SCOPE!r}")


def require_tenant_admin(context: TenantContext) -> None:
    if not context.is_tenant_admin():
        raise TenantAccessDenied("tenant administrator capability is required")


@dataclass(frozen=True, slots=True)
class TenantChoice:
    tenant_id: str
    tenant_code: str
    tenant_name: str = ""
    roles: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "code": self.tenant_code,
            "name": self.tenant_name,
            "roles": list(self.roles),
        }


@dataclass(frozen=True, slots=True)
class TenantSelection:
    """Result of login/selection: either one resolved tenant or a selection challenge."""

    selected_tenant_id: str | None
    choices: tuple[TenantChoice, ...] = ()
    requires_selection: bool = False

    @property
    def selection_required(self) -> bool:
        return self.requires_selection


def select_tenant(
    *,
    identity_id: str,
    memberships: Iterable[MembershipView],
    tenants_by_id: Mapping[str, TenantView],
    requested_tenant_id: str | UUID | None = None,
) -> TenantSelection:
    """Resolve login tenant selection for an identity (0/1/many active memberships).

    * ``requested_tenant_id`` → must be an ACTIVE membership on an ACTIVE tenant (else 404-like denial);
    * zero eligible memberships → ``NO_ACTIVE_MEMBERSHIP``;
    * exactly one → selected; several → ``TENANT_SELECTION_REQUIRED`` with public choices.
    """
    eligible: list[tuple[MembershipView, TenantView]] = []
    for membership in memberships:
        if not _same_id(membership.identity_id, identity_id):
            continue
        if membership.status is not MembershipStatus.ACTIVE:
            continue
        tenant = tenants_by_id.get(str(membership.tenant_id))
        if tenant is None or tenant.status is not TenantStatus.ACTIVE:
            continue
        eligible.append((membership, tenant))
    if requested_tenant_id is not None:
        for _membership, tenant in eligible:
            if _same_id(tenant.id, requested_tenant_id):
                return TenantSelection(selected_tenant_id=str(tenant.id))
        raise CrossTenantAccessDenied("no active membership for the requested tenant")
    if not eligible:
        raise NoActiveMembership("identity has no active tenant membership")
    if len(eligible) == 1:
        return TenantSelection(selected_tenant_id=str(eligible[0][1].id))
    choices = tuple(
        TenantChoice(
            tenant_id=str(tenant.id),
            tenant_code=tenant.code,
            tenant_name=tenant.name,
            roles=membership.roles,
        )
        for membership, tenant in sorted(eligible, key=lambda item: item[1].code)
    )
    return TenantSelection(selected_tenant_id=None, choices=choices, requires_selection=True)


def resolve_selection_target(
    *,
    selection_token_identity_id: str,
    selected_identity_id: str,
    memberships: Iterable[MembershipView],
    tenants_by_id: Mapping[str, TenantView],
    tenant_id: str | UUID,
) -> TenantSelection:
    """Validate a selection token is used by its own identity, then resolve the requested tenant."""
    if not _same_id(selection_token_identity_id, selected_identity_id):
        raise CrossTenantAccessDenied("selection token does not belong to this identity")
    return select_tenant(
        identity_id=selected_identity_id,
        memberships=memberships,
        tenants_by_id=tenants_by_id,
        requested_tenant_id=tenant_id,
    )


def ensure_platform_target(context: TenantContext, target_tenant_id: str | UUID) -> None:
    """Platform-admin operations may target any tenant, but still require a platform-admin actor context."""
    require_platform_admin(context)
    if not str(target_tenant_id):
        raise TokenInvalid("target tenant id is required")


__all__ = [
    "AdminClass",
    "DATA_SCOPE_TYPES",
    "MembershipStatus",
    "MembershipView",
    "PLATFORM_TENANT_ADMIN_SCOPE",
    "SYSTEM_TENANT_ID",
    "SYSTEM_TENANT_ID_TEXT",
    "TENANT_ADMIN_SCOPE",
    "TenantChoice",
    "TenantContext",
    "TenantSelection",
    "TenantStatus",
    "TenantView",
    "classify_admin",
    "effective_data_scopes",
    "ensure_can_write",
    "ensure_platform_target",
    "ensure_same_tenant",
    "require_platform_admin",
    "require_tenant_admin",
    "resolve_selection_target",
    "resolve_tenant_context",
    "select_tenant",
]
