"""Tenant context / isolation / selection tests (pure; Δ-MT)."""

from __future__ import annotations

import pytest

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
from app.core.security.tenant_context import (
    SYSTEM_TENANT_ID,
    AdminClass,
    MembershipStatus,
    MembershipView,
    TenantStatus,
    TenantView,
    classify_admin,
    effective_data_scopes,
    ensure_can_write,
    ensure_same_tenant,
    require_platform_admin,
    require_tenant_admin,
    resolve_selection_target,
    resolve_tenant_context,
    select_tenant,
)

TENANT = TenantView(id="tenant-1", code="TEN0001", name="Tenant One")
SYSTEM_TENANT = TenantView(id=str(SYSTEM_TENANT_ID), code="SYSTEM", name="System")


def membership(**overrides: object) -> MembershipView:
    data: dict[str, object] = {
        "app_user_id": "app-user-1",
        "identity_id": "identity-1",
        "tenant_id": "tenant-1",
        "status": MembershipStatus.ACTIVE,
        "token_version": 2,
        "roles": ("admin",),
        "scopes": frozenset({"platform.admin", "items.read"}),
    }
    data.update(overrides)
    return MembershipView(**data)  # type: ignore[arg-type]


def resolve(**overrides: object):
    arguments: dict[str, object] = {
        "tenant": TENANT,
        "membership": membership(),
        "token_tenant_id": "tenant-1",
        "token_app_user_id": "app-user-1",
        "token_identity_id": "identity-1",
        "token_version": 2,
    }
    arguments.update(overrides)
    return resolve_tenant_context(**arguments)  # type: ignore[arg-type]


def test_resolve_context_success_and_admin_class() -> None:
    context = resolve()
    assert context.tenant_id == "tenant-1"
    assert context.admin_class is AdminClass.TENANT_ADMIN
    assert context.is_tenant_admin() is True and context.is_platform_admin() is False
    assert context.can_write() is True
    assert context.describe()["membership_status"] == "ACTIVE"


def test_cross_tenant_and_claim_mismatch_denied() -> None:
    with pytest.raises(CrossTenantAccessDenied):
        resolve(token_tenant_id="tenant-2")
    with pytest.raises(CrossTenantAccessDenied):
        resolve(membership=membership(tenant_id="tenant-2"))
    with pytest.raises(TokenInvalid):
        resolve(token_app_user_id="other-user")
    with pytest.raises(TokenInvalid):
        resolve(token_identity_id="other-identity")
    with pytest.raises(TokenVersionStale):
        resolve(token_version=1)


def test_membership_and_tenant_status_enforcement() -> None:
    with pytest.raises(MembershipInactive):
        resolve(membership=membership(status=MembershipStatus.SUSPENDED))
    suspended = TenantView(id="tenant-1", code="TEN0001", status=TenantStatus.SUSPENDED)
    # Reads are allowed while suspended; writes are blocked (DM-PLAT-09).
    assert resolve(tenant=suspended).can_write() is False
    with pytest.raises(TenantSuspended):
        resolve(tenant=suspended, write=True)
    offboarding = TenantView(id="tenant-1", code="TEN0001", status=TenantStatus.OFFBOARDING)
    with pytest.raises(TenantSuspended):
        resolve(tenant=offboarding)
    assert resolve(tenant=offboarding, allow_read_only_export=True).tenant.status is TenantStatus.OFFBOARDING
    context = resolve(tenant=suspended)
    with pytest.raises(TenantSuspended):
        ensure_can_write(context)


def test_cross_tenant_resource_guard_is_404_like() -> None:
    context = resolve()
    ensure_same_tenant(context, "tenant-1")
    ensure_same_tenant(context, None)
    with pytest.raises(CrossTenantAccessDenied) as error:
        ensure_same_tenant(context, "tenant-2")
    assert error.value.http_status == 404


def test_platform_admin_requires_flag_system_tenant_and_scope() -> None:
    platform_membership = MembershipView(
        app_user_id="sys-user",
        identity_id="sys-identity",
        tenant_id=str(SYSTEM_TENANT_ID),
        roles=("admin",),
        scopes=frozenset({"platform.tenant.manage"}),
        is_platform_admin=True,
    )
    platform_context = resolve_tenant_context(
        tenant=SYSTEM_TENANT,
        membership=platform_membership,
        token_tenant_id=str(SYSTEM_TENANT_ID),
        token_app_user_id="sys-user",
        token_identity_id="sys-identity",
        token_version=0,
    )
    assert platform_context.admin_class is AdminClass.PLATFORM_ADMIN
    require_platform_admin(platform_context)

    with pytest.raises(PlatformAdminRequired):
        require_platform_admin(resolve())  # tenant admin is not a platform admin
    missing_scope = platform_context.__class__(
        tenant=platform_context.tenant,
        membership=MembershipView(
            app_user_id="sys-user",
            identity_id="sys-identity",
            tenant_id=str(SYSTEM_TENANT_ID),
            roles=("admin",),
            scopes=frozenset({"platform.admin"}),
            is_platform_admin=True,
        ),
        admin_class=AdminClass.PLATFORM_ADMIN,
    )
    with pytest.raises(PlatformAdminRequired):
        require_platform_admin(missing_scope)
    with pytest.raises(TenantAccessDenied):
        require_tenant_admin(platform_context)


def test_classify_admin_variants_and_data_scopes() -> None:
    assert classify_admin(membership(is_service_account=True), TENANT) is AdminClass.SERVICE
    assert classify_admin(membership(roles=("readonly",), scopes=frozenset({"items.read"})), TENANT) is AdminClass.BUSINESS
    merged = effective_data_scopes({"LOCATION": ("LOC-1", "LOC-2")}, {"LOCATION": ("LOC-3",)})
    assert merged["LOCATION"] == ("LOC-3",)
    with pytest.raises(ValueError):
        effective_data_scopes({"PLANET": ("x",)})


def test_select_tenant_zero_one_many_and_requested() -> None:
    memberships = [
        membership(app_user_id="u1", tenant_id="tenant-1"),
        membership(app_user_id="u2", tenant_id="tenant-2"),
    ]
    tenants = {
        "tenant-1": TENANT,
        "tenant-2": TenantView(id="tenant-2", code="TEN0002", name="Tenant Two"),
    }
    single = select_tenant(identity_id="identity-1", memberships=memberships[:1], tenants_by_id=tenants)
    assert single.selected_tenant_id == "tenant-1" and single.selection_required is False

    many = select_tenant(identity_id="identity-1", memberships=memberships, tenants_by_id=tenants)
    assert many.selection_required is True and many.selected_tenant_id is None
    assert [choice.tenant_code for choice in many.choices] == ["TEN0001", "TEN0002"]

    requested = select_tenant(
        identity_id="identity-1", memberships=memberships, tenants_by_id=tenants, requested_tenant_id="tenant-2"
    )
    assert requested.selected_tenant_id == "tenant-2"
    with pytest.raises(CrossTenantAccessDenied):
        select_tenant(identity_id="identity-1", memberships=memberships, tenants_by_id=tenants, requested_tenant_id="tenant-3")
    with pytest.raises(NoActiveMembership):
        select_tenant(identity_id="unknown", memberships=memberships, tenants_by_id=tenants)

    # Suspended membership/tenant are not eligible.
    suspended_memberships = [membership(status=MembershipStatus.SUSPENDED)]
    with pytest.raises(NoActiveMembership):
        select_tenant(identity_id="identity-1", memberships=suspended_memberships, tenants_by_id=tenants)

    selected = resolve_selection_target(
        selection_token_identity_id="identity-1",
        selected_identity_id="identity-1",
        memberships=memberships,
        tenants_by_id=tenants,
        tenant_id="tenant-1",
    )
    assert selected.selected_tenant_id == "tenant-1"
    with pytest.raises(CrossTenantAccessDenied):
        resolve_selection_target(
            selection_token_identity_id="identity-2",
            selected_identity_id="identity-1",
            memberships=memberships,
            tenants_by_id=tenants,
            tenant_id="tenant-1",
        )
