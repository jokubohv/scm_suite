"""Typed, pure security errors shared by the FS-00 security services.

Every error carries a stable machine-readable ``code`` (matching the plan's error catalogue) and an
HTTP status so the API layer can render the RFC 9457 problem envelope without re-mapping. This module
has no DB/network imports — it is usable from unit tests and from request/job contexts alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SecurityError(Exception):
    """Base class for all FS-00 security errors."""

    code: str = "SECURITY_ERROR"
    http_status: int = 400

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})

    def to_problem(self) -> dict[str, Any]:
        """Return the RFC 9457-style problem object (the API layer adds instance/trace ids)."""
        return {
            "type": f"https://inventory-platform.example/errors/{self.code.lower()}",
            "title": self.code,
            "status": self.http_status,
            "code": self.code,
            "detail": self.message,
            "details": self.details,
        }


# --------------------------------------------------------------------------- authentication --


class AuthenticationError(SecurityError):
    http_status = 401


class TokenInvalid(AuthenticationError):
    code = "TOKEN_INVALID"


class TokenExpired(AuthenticationError):
    code = "TOKEN_EXPIRED"


class TokenVersionStale(AuthenticationError):
    code = "TOKEN_VERSION_STALE"


class SessionRevoked(AuthenticationError):
    code = "SESSION_REVOKED"


class InvalidCredentials(AuthenticationError):
    code = "INVALID_CREDENTIALS"


# --------------------------------------------------------------------------- authorization --


class AuthorizationError(SecurityError):
    http_status = 403


class ForbiddenScope(AuthorizationError):
    code = "FORBIDDEN_SCOPE"


class PrivilegeEscalation(AuthorizationError):
    code = "PRIVILEGE_ESCALATION"


class PlatformAdminRequired(AuthorizationError):
    code = "FORBIDDEN_PLATFORM_ADMIN"


class TenantAccessDenied(AuthorizationError):
    code = "FORBIDDEN_TENANT"


# --------------------------------------------------------------------------- catalogue -------


class ScopeCatalogueError(SecurityError):
    code = "SCOPE_CATALOGUE_INVALID"
    http_status = 422


class UnknownScope(ScopeCatalogueError):
    code = "UNKNOWN_SCOPE"


class UnknownRole(ScopeCatalogueError):
    code = "UNKNOWN_ROLE"


class ScopeNotAssignable(ScopeCatalogueError):
    code = "SCOPE_NOT_ASSIGNABLE"


class SodConflict(SecurityError):
    code = "SOD_CONFLICT"
    http_status = 409


class TemplateImmutable(SecurityError):
    code = "TEMPLATE_IMMUTABLE"
    http_status = 409


class RoleInUse(SecurityError):
    code = "ROLE_IN_USE"
    http_status = 409


class LastAdminLockout(SecurityError):
    code = "LAST_TENANT_ADMIN"
    http_status = 409


# --------------------------------------------------------------------------- tenancy ---------


class TenantError(SecurityError):
    http_status = 403


class NoActiveMembership(TenantError):
    code = "NO_ACTIVE_MEMBERSHIP"


class MembershipInactive(TenantError):
    code = "MEMBERSHIP_INACTIVE"


class TenantSuspended(TenantError):
    code = "TENANT_SUSPENDED"


class TenantSelectionRequired(TenantError):
    """Login succeeded but the identity holds several active memberships (not a failure)."""

    code = "TENANT_SELECTION_REQUIRED"
    http_status = 200


class CrossTenantAccessDenied(TenantError):
    """Cross-tenant probes are indistinguishable from missing resources (404, no existence leak)."""

    code = "TENANT_ISOLATION"
    http_status = 404


# --------------------------------------------------------------------------- idempotency -----


class IdempotencyError(SecurityError):
    http_status = 409


class IdempotencyConflict(IdempotencyError):
    code = "IDEMPOTENCY_KEY_REUSED"


class IdempotencyInProgress(IdempotencyError):
    code = "IDEMPOTENCY_IN_PROGRESS"


class InvalidIdempotencyKey(SecurityError):
    code = "INVALID_IDEMPOTENCY_KEY"
    http_status = 422


# --------------------------------------------------------------------------- SSO -------------


class SsoError(SecurityError):
    http_status = 400


class SsoProviderNotFound(SsoError):
    code = "SSO_PROVIDER_NOT_FOUND"
    http_status = 404


class SsoDisabled(SsoError):
    code = "SSO_DISABLED"
    http_status = 403


class SsoEnforcedLocalDenied(SsoError):
    code = "SSO_ENFORCED_LOCAL_DENIED"
    http_status = 403


class SsoStateInvalid(SsoError):
    code = "SSO_STATE_INVALID"
    http_status = 400


class SsoIdpUnavailable(SsoError):
    code = "SSO_IDP_UNAVAILABLE"
    http_status = 503


class SsoEmailDomainNotAllowed(SsoError):
    code = "SSO_EMAIL_DOMAIN_NOT_ALLOWED"
    http_status = 403


class JitProvisioningDisabled(SsoError):
    code = "JIT_PROVISIONING_DISABLED"
    http_status = 403


class IdentityLinkConflict(SsoError):
    code = "IDENTITY_LINK_CONFLICT"
    http_status = 409


class SsoGroupMappingInvalid(SsoError):
    code = "SSO_GROUP_MAPPING_INVALID"
    http_status = 422


class SsoIdTokenInvalid(SsoError):
    code = "SSO_ID_TOKEN_INVALID"
    http_status = 401


class SsoConnectionInvalid(SsoError):
    code = "SSO_CONNECTION_INVALID"
    http_status = 422


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "CrossTenantAccessDenied",
    "ForbiddenScope",
    "IdentityLinkConflict",
    "IdempotencyConflict",
    "IdempotencyError",
    "IdempotencyInProgress",
    "InvalidCredentials",
    "InvalidIdempotencyKey",
    "JitProvisioningDisabled",
    "LastAdminLockout",
    "MembershipInactive",
    "NoActiveMembership",
    "PlatformAdminRequired",
    "PrivilegeEscalation",
    "RoleInUse",
    "ScopeCatalogueError",
    "ScopeNotAssignable",
    "SecurityError",
    "SessionRevoked",
    "SodConflict",
    "SsoConnectionInvalid",
    "SsoDisabled",
    "SsoEmailDomainNotAllowed",
    "SsoEnforcedLocalDenied",
    "SsoError",
    "SsoGroupMappingInvalid",
    "SsoIdTokenInvalid",
    "SsoIdpUnavailable",
    "SsoProviderNotFound",
    "SsoStateInvalid",
    "TemplateImmutable",
    "TenantAccessDenied",
    "TenantError",
    "TenantSelectionRequired",
    "TenantSuspended",
    "TokenExpired",
    "TokenInvalid",
    "TokenVersionStale",
    "UnknownRole",
    "UnknownScope",
]
