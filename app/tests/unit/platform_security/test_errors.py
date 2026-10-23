"""Typed security error catalogue tests (pure)."""

from __future__ import annotations

from app.core.security import errors


def test_security_error_problem_envelope() -> None:
    error = errors.ForbiddenScope("missing scope", details={"required_scope": "items.read"})
    assert error.code == "FORBIDDEN_SCOPE"
    assert error.http_status == 403
    problem = error.to_problem()
    assert problem["code"] == "FORBIDDEN_SCOPE"
    assert problem["status"] == 403
    assert problem["detail"] == "missing scope"
    assert problem["details"] == {"required_scope": "items.read"}
    assert problem["type"].endswith("/forbidden_scope")


def test_error_hierarchy_and_codes_are_stable() -> None:
    cases = {
        errors.TokenExpired: ("TOKEN_EXPIRED", 401),
        errors.TokenVersionStale: ("TOKEN_VERSION_STALE", 401),
        errors.SessionRevoked: ("SESSION_REVOKED", 401),
        errors.SodConflict: ("SOD_CONFLICT", 409),
        errors.LastAdminLockout: ("LAST_TENANT_ADMIN", 409),
        errors.ScopeNotAssignable: ("SCOPE_NOT_ASSIGNABLE", 422),
        errors.CrossTenantAccessDenied: ("TENANT_ISOLATION", 404),
        errors.TenantSuspended: ("TENANT_SUSPENDED", 403),
        errors.SsoEnforcedLocalDenied: ("SSO_ENFORCED_LOCAL_DENIED", 403),
        errors.IdempotencyConflict: ("IDEMPOTENCY_KEY_REUSED", 409),
    }
    for error_class, (code, status) in cases.items():
        raised = error_class("detail")
        assert raised.code == code
        assert raised.http_status == status
        assert raised.message == "detail"
    assert issubclass(errors.ForbiddenScope, errors.AuthorizationError)
    assert issubclass(errors.AuthenticationError, errors.SecurityError)
