"""RBAC catalogue/scope evaluation tests (pure; catalogue loaded read-only from design/permissions.json)."""

from __future__ import annotations

import pytest

from app.core.security.errors import (
    AuthenticationError,
    ForbiddenScope,
    LastAdminLockout,
    PrivilegeEscalation,
    ScopeNotAssignable,
    SecurityError,
    SodConflict,
    TemplateImmutable,
    UnknownRole,
    UnknownScope,
)
from app.core.security.scopes import (
    ADMIN_BREAK_GLASS_ROLE,
    ROLE_ADMIN_SCOPE,
    USER_ADMIN_SCOPE,
    EffectivePermissions,
    RouteClass,
    ScopeClass,
    ScopeRegistry,
    SodContext,
    load_catalogue,
)
from app.tests.unit.platform_security.helpers import CATALOGUE, REGISTRY

# Design v1.5.0 counts (asserted against the JSON `counts` block + computed values).
EXPECTED_SCOPES = 255
EXPECTED_ASSIGNABLE = 241
EXPECTED_SERVICE_ONLY = 13
EXPECTED_PLATFORM_ONLY = 1
EXPECTED_ROLES = 7
EXPECTED_SOD_PAIRS = 14
EXPECTED_APPROVAL_SCOPES = 18
EXPECTED_PUBLIC_ALLOWLIST = 9
EXPECTED_SELF_SERVICE_ALLOWLIST = 6


def test_catalogue_counts_match_plan_v1_5() -> None:
    counts = CATALOGUE.counts
    assert CATALOGUE.plan_version == "1.5.0"
    assert counts["scopes"] == EXPECTED_SCOPES
    assert counts["assignable_scopes"] == EXPECTED_ASSIGNABLE
    assert counts["service_only"] == EXPECTED_SERVICE_ONLY
    assert counts["platform_only"] == EXPECTED_PLATFORM_ONLY
    assert counts["roles"] == EXPECTED_ROLES
    assert counts["sod_pairs"] == EXPECTED_SOD_PAIRS
    assert counts["requires_approval"] == EXPECTED_APPROVAL_SCOPES
    assert counts["public_allowlist"] == EXPECTED_PUBLIC_ALLOWLIST
    assert counts["self_service_allowlist"] == EXPECTED_SELF_SERVICE_ALLOWLIST
    # ...and the loaded objects agree with the advertised counts (no drift).
    assert len(CATALOGUE.scopes) == EXPECTED_SCOPES
    assert len(CATALOGUE.assignable_scopes()) == EXPECTED_ASSIGNABLE
    assert sum(1 for scope in CATALOGUE.scopes if scope.service_only) == EXPECTED_SERVICE_ONLY
    assert sum(1 for scope in CATALOGUE.scopes if scope.platform_only) == EXPECTED_PLATFORM_ONLY
    assert len(CATALOGUE.role_templates) == EXPECTED_ROLES
    assert len(CATALOGUE.sod_pairs) == EXPECTED_SOD_PAIRS
    assert len(CATALOGUE.approval_scopes()) == EXPECTED_APPROVAL_SCOPES
    assert len(CATALOGUE.public_allowlist) == EXPECTED_PUBLIC_ALLOWLIST
    assert len(CATALOGUE.self_service_allowlist) == EXPECTED_SELF_SERVICE_ALLOWLIST
    assert CATALOGUE.aliases == {"platform.user.manage": "platform.users.manage"}


def test_catalogue_validates_and_templates_are_coherent() -> None:
    CATALOGUE.validate()  # idempotent; raises on any structural inconsistency
    assert CATALOGUE.template("admin").scopes == CATALOGUE.assignable_scopes()
    assert CATALOGUE.template("admin").system is True
    assert CATALOGUE.template("admin").editable is False
    assert len(CATALOGUE.template("readonly").scopes) == 44
    # Non-admin templates may not combine any SoD pair (validated at load); spot-check the documented ones.
    assert not any(
        scope.service_only or scope.platform_only for scope in CATALOGUE.scopes if scope.assignable
    )


def test_scope_classes_alias_and_unknown() -> None:
    assert CATALOGUE.scope_class("platform.internal") is ScopeClass.SERVICE_ONLY
    assert CATALOGUE.scope_class("platform.tenant.manage") is ScopeClass.PLATFORM_ONLY
    assert CATALOGUE.scope_class("items.read") is ScopeClass.ASSIGNABLE
    assert CATALOGUE.scope_class("platform.user.manage") is ScopeClass.ALIAS
    assert CATALOGUE.canonical("platform.user.manage") == "platform.users.manage"
    with pytest.raises(UnknownScope):
        CATALOGUE.canonical("not.a.scope")
    assert CATALOGUE.scope("items.read") is not None


def test_expand_roles_and_effective_permissions() -> None:
    readonly = REGISTRY.expand_roles(("readonly",))
    assert readonly == CATALOGUE.template("readonly").scopes
    assert "items.read" in readonly
    union = REGISTRY.expand_roles(("readonly", "planner"))
    assert union > readonly
    with pytest.raises(UnknownRole):
        REGISTRY.expand_roles(("no-such-role",))

    custom = {"custom_reader": ("items.read", "items.create")}
    effective = REGISTRY.effective_permissions(
        ("readonly",),
        custom_roles=custom,
        api_key_scopes=("platform.internal",),
        data_scopes={"LOCATION": ("LOC-1", "LOC-1", "LOC-2")},
    )
    assert isinstance(effective, EffectivePermissions)
    assert effective.has("items.read")
    assert effective.has("platform.internal")  # service-only is allowed for machine API keys, never roles
    assert effective.data_scopes["LOCATION"] == ("LOC-1", "LOC-2")
    with pytest.raises(ForbiddenScope):
        effective.require("inventory.adjustment.post")


def test_require_scope_and_role_admin_guards() -> None:
    readonly = REGISTRY.effective_permissions(("readonly",))
    REGISTRY.require_scope(readonly, "items.read")
    with pytest.raises(ForbiddenScope):
        REGISTRY.require_scope(readonly, "items.create")
    admin = REGISTRY.effective_permissions(("admin",))
    REGISTRY.require_scope(admin, "items.create")
    REGISTRY.require_role_admin(admin)
    REGISTRY.require_user_admin(admin)
    with pytest.raises(ForbiddenScope):
        REGISTRY.require_role_admin(readonly)
    assert ROLE_ADMIN_SCOPE in admin.scopes and USER_ADMIN_SCOPE in admin.scopes


def test_assignability_platform_and_service_scopes_rejected() -> None:
    assert REGISTRY.is_assignable("items.read") is True
    assert REGISTRY.is_assignable("platform.tenant.manage") is False
    assert REGISTRY.is_assignable("platform.internal") is False
    assert REGISTRY.require_assignable(("items.read", "platform.user.manage")) == frozenset(
        {"items.read", "platform.users.manage"}
    )
    with pytest.raises(ScopeNotAssignable):
        REGISTRY.require_assignable(("platform.internal",))
    with pytest.raises(ScopeNotAssignable):
        REGISTRY.require_assignable(("platform.tenant.manage",))
    with pytest.raises(UnknownScope):
        REGISTRY.require_assignable(("does.not.exist",))


def test_system_templates_are_clone_only() -> None:
    with pytest.raises(TemplateImmutable):
        REGISTRY.require_template_mutable("admin")
    with pytest.raises(UnknownRole):
        REGISTRY.require_template_mutable("custom_reader")  # custom roles are not catalogue templates


def test_route_guard_public_and_self_service_allowlists() -> None:
    assert REGISTRY.classify_route("GET", "/healthz") is RouteClass.PUBLIC
    assert REGISTRY.classify_route("GET", "/openapi.json") is RouteClass.PUBLIC
    assert REGISTRY.classify_route("POST", "/api/v1/auth/login") is RouteClass.PUBLIC
    assert REGISTRY.classify_route("GET", "/api/v1/auth/sso/callback") is RouteClass.PUBLIC
    # Templated invitation path (single path segment) matches the allowlist entry.
    assert REGISTRY.classify_route("POST", "/api/v1/admin/invitations/abc123token/accept") is RouteClass.PUBLIC
    # Method matters for method-scoped allowlist entries.
    assert REGISTRY.classify_route("GET", "/api/v1/auth/login") is RouteClass.SCOPED
    assert REGISTRY.classify_route("GET", "/api/v1/me") is RouteClass.SELF_SERVICE
    assert REGISTRY.classify_route("GET", "/api/v1/me/effective-permissions") is RouteClass.SELF_SERVICE
    assert REGISTRY.classify_route("GET", "/api/v1/items") is RouteClass.SCOPED

    anonymous_public = REGISTRY.guard_route("GET", "/healthz", None, None)
    assert anonymous_public.allowed is True
    anonymous_me = REGISTRY.guard_route("GET", "/api/v1/me", None, None)
    assert anonymous_me.allowed is False and anonymous_me.reason == "AUTHENTICATION_REQUIRED"
    me = REGISTRY.guard_route("GET", "/api/v1/me", None, EffectivePermissions(scopes=frozenset()))
    assert me.allowed is True


def test_route_guard_deny_by_default() -> None:
    anonymous = REGISTRY.guard_route("GET", "/api/v1/items", "items.read", None)
    assert anonymous.allowed is False and anonymous.reason == "AUTHENTICATION_REQUIRED"
    readonly = REGISTRY.effective_permissions(("readonly",))
    allowed = REGISTRY.guard_route("GET", "/api/v1/items", "items.read", readonly)
    assert allowed.allowed is True and allowed.route_class is RouteClass.SCOPED
    denied = REGISTRY.guard_route("POST", "/api/v1/items", "items.create", readonly)
    assert denied.allowed is False and denied.reason == "FORBIDDEN_SCOPE"

    missing = REGISTRY.guard_route("GET", "/api/v1/items", None, readonly)
    assert missing.allowed is False and missing.reason == "MISSING_SCOPE_DECLARATION"
    unknown = REGISTRY.guard_route("GET", "/api/v1/items", "not.catalogued", readonly)
    assert unknown.allowed is False and unknown.reason == "UNKNOWN_SCOPE"

    with pytest.raises(AuthenticationError):
        REGISTRY.enforce_route("GET", "/api/v1/items", "items.read", None)
    with pytest.raises(ForbiddenScope):
        REGISTRY.enforce_route("POST", "/api/v1/items", "items.create", readonly)
    with pytest.raises(SecurityError) as missing_error:
        REGISTRY.enforce_route("GET", "/api/v1/items", None, readonly)
    assert missing_error.value.code == "SCOPE_CATALOGUE_INVALID"


def test_privilege_escalation_guard() -> None:
    actor = REGISTRY.effective_permissions(("readonly",))
    REGISTRY.require_can_grant(actor, ("items.read",))
    with pytest.raises(PrivilegeEscalation):
        REGISTRY.require_can_grant(actor, ("items.create",))
    REGISTRY.require_can_grant(actor, ("platform.internal",), is_platform_admin=True)  # platform admin bypass
    with pytest.raises(ForbiddenScope):
        REGISTRY.require_can_manage_roles(actor)
    with pytest.raises(ForbiddenScope):
        REGISTRY.require_can_assign_roles(actor)


def test_last_admin_lockout_guard() -> None:
    REGISTRY.require_not_last_admin(was_active_admin=True, will_remain_active_admin=True, active_admin_count=1)
    REGISTRY.require_not_last_admin(was_active_admin=True, will_remain_active_admin=False, active_admin_count=2)
    with pytest.raises(LastAdminLockout):
        REGISTRY.require_not_last_admin(was_active_admin=True, will_remain_active_admin=False, active_admin_count=1)
    with pytest.raises(LastAdminLockout):
        REGISTRY.require_not_last_admin(was_active_admin=False, will_remain_active_admin=False, active_admin_count=0)


def test_sod_pairs_hard_warn_and_break_glass() -> None:
    assert len(CATALOGUE.sod_pairs) == EXPECTED_SOD_PAIRS
    pair = next(pair for pair in CATALOGUE.sod_pairs if pair.enforcement == "HARD")
    conflicting = {pair.a, pair.b}
    with pytest.raises(SodConflict):
        REGISTRY.require_no_sod_conflict(conflicting)
    # Admin is the only break-glass role allowed to combine a pair (audited by callers).
    evaluation = REGISTRY.require_no_sod_conflict(conflicting, role_keys=(ADMIN_BREAK_GLASS_ROLE,))
    assert evaluation.ok and evaluation.enforced is False
    # WARN downgrades only custom-role creation warnings; assignments remain HARD.
    warn_registry = ScopeRegistry(CATALOGUE, sod_mode="WARN")
    warning = warn_registry.require_no_sod_conflict(conflicting, context=SodContext.CUSTOM_ROLE_CREATION)
    assert warning.ok and warning.warning is True
    with pytest.raises(SodConflict):
        warn_registry.require_no_sod_conflict(conflicting, context=SodContext.ASSIGNMENT)


def test_catalogue_loader_is_shared_read_only() -> None:
    again = load_catalogue()
    assert again is CATALOGUE  # cached snapshot; no mutation surface
    assert REGISTRY.describe()["plan_version"] == "1.5.0"
    assert REGISTRY.describe()["scopes"] == EXPECTED_SCOPES
