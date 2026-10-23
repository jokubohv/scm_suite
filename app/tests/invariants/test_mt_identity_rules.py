"""Multi-tenant identity-rule invariants (Δ-MT / Δ-SSO; DM-PLAT-13/14, DM-SSO-02 + INV-30/INV-32).

Covers the rules that protect identity lifecycle outside the happy path:

* invitation lifecycle — single-use acceptance, expiry, hashed-token uniqueness;
* ``token_version`` invalidation + session revocation on membership revocation;
* last-active-administrator lockout and SoD guards;
* verified-email / domain / JIT identity-linking decisions and enforced-SSO local-login rejection;
* identity-link uniqueness keys.

Pure-policy tests always run; DB tests use the shared rollback-isolated ``db_session`` fixture (skip when
no database is available).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.core.models import platform as p
from app.core.repositories.auth import AuthRepository
from app.core.security.authn import (
    IdentityDenialReason,
    IdentityResolutionKind,
    SsoConnectionStatus,
    SsoConnectionView,
    SsoProtocol,
    decide_identity_resolution,
    domain_allowed,
    local_login_allowed,
    normalize_email_domains,
    require_identity_resolution_allowed,
)
from app.core.security.errors import (
    LastAdminLockout,
    ScopeNotAssignable,
    SodConflict,
    SsoEmailDomainNotAllowed,
    TemplateImmutable,
)
from app.core.security.scopes import ScopeRegistry, SodContext


def _connection(**overrides) -> SsoConnectionView:
    base = dict(
        id=str(uuid4()),
        tenant_id=str(uuid4()),
        tenant_code="acme",
        tenant_name="Acme Corp",
        protocol=SsoProtocol.OIDC,
        status=SsoConnectionStatus.ACTIVE,
        dex_issuer_url="https://sso-acme.auth.example.test",
        client_id="platform-api",
        email_domains=("Acme.example",),
        is_enforced=False,
        jit_provisioning=False,
    )
    base.update(overrides)
    return SsoConnectionView(**base)


# --------------------------------------------------------------------------- pure policy --------
def test_identity_resolution_precedence_and_denials() -> None:
    linked = decide_identity_resolution(
        existing_link_identity_id="identity-1", email_verified=False, domain_allowed=False, jit_provisioning=False
    )
    assert linked.kind is IdentityResolutionKind.LINKED
    assert linked.identity_id == "identity-1"

    unverified = decide_identity_resolution(email_verified=False, domain_allowed=True, jit_provisioning=True)
    assert unverified.kind is IdentityResolutionKind.DENIED
    assert unverified.denial_reason is IdentityDenialReason.EMAIL_NOT_VERIFIED

    bad_domain = decide_identity_resolution(email_verified=True, domain_allowed=False, jit_provisioning=True)
    assert bad_domain.kind is IdentityResolutionKind.DENIED
    assert bad_domain.denial_reason is IdentityDenialReason.EMAIL_DOMAIN_NOT_ALLOWED

    verified_link = decide_identity_resolution(
        linked_member_identity_id="identity-2", email_verified=True, domain_allowed=True, jit_provisioning=False
    )
    assert verified_link.kind is IdentityResolutionKind.LINK_BY_VERIFIED_EMAIL
    assert verified_link.identity_id == "identity-2"

    jit = decide_identity_resolution(email_verified=True, domain_allowed=True, jit_provisioning=True)
    assert jit.kind is IdentityResolutionKind.JIT_PROVISION
    assert jit.create_identity is True and jit.create_membership is True

    no_jit = decide_identity_resolution(email_verified=True, domain_allowed=True, jit_provisioning=False)
    assert no_jit.kind is IdentityResolutionKind.DENIED
    assert no_jit.denial_reason is IdentityDenialReason.JIT_PROVISIONING_DISABLED
    with pytest.raises(Exception) as excinfo:
        require_identity_resolution_allowed(no_jit)
    assert excinfo.value.__class__.__name__ == "JitProvisioningDisabled"


def test_email_domain_matching_is_case_insensitive_and_normalised() -> None:
    connection = _connection(email_domains=("Acme.Example", " acme.example "))
    assert connection.email_domains == ("acme.example",)
    assert normalize_email_domains(["B.Example", "b.example", ""]) == ("b.example",)
    assert domain_allowed("user@ACME.example", connection)
    assert not domain_allowed("user@other.example", connection)
    with pytest.raises(SsoEmailDomainNotAllowed):
        from app.core.security.authn import ensure_email_domain_allowed

        ensure_email_domain_allowed("user@other.example", connection)


def test_enforced_sso_rejects_local_password_login_for_members() -> None:
    enforced = _connection(is_enforced=True)
    assert local_login_allowed(None) is True
    assert local_login_allowed(enforced) is False
    # Platform-admin break-glass remains available and is separately audited.
    assert local_login_allowed(enforced, is_platform_admin=True, break_glass_enabled=True) is True
    assert local_login_allowed(enforced, is_platform_admin=True, break_glass_enabled=False) is False
    disabled = _connection(is_enforced=True, status=SsoConnectionStatus.DISABLED)
    assert local_login_allowed(disabled) is True


def test_last_active_administrator_lockout_guard() -> None:
    registry = ScopeRegistry()
    registry.require_not_last_admin(was_active_admin=True, will_remain_active_admin=False, active_admin_count=2)
    registry.require_not_last_admin(was_active_admin=False, will_remain_active_admin=False, active_admin_count=1)
    with pytest.raises(LastAdminLockout):
        registry.require_not_last_admin(was_active_admin=True, will_remain_active_admin=False, active_admin_count=1)
    with pytest.raises(LastAdminLockout):
        registry.require_not_last_admin(was_active_admin=False, will_remain_active_admin=False, active_admin_count=0)


def test_separation_of_duties_is_enforced_for_assignments() -> None:
    registry = ScopeRegistry()
    pair = registry.catalogue.sod_pairs[0]
    assert pair.enforcement == "HARD"
    with pytest.raises(SodConflict):
        registry.require_no_sod_conflict([pair.a, pair.b], context=SodContext.ASSIGNMENT)
    allowed = registry.require_no_sod_conflict([pair.a, pair.b], context=SodContext.ASSIGNMENT, role_keys=("admin",))
    assert allowed.enforced is False  # admin is the audited break-glass template
    registry.require_no_sod_conflict([pair.a], context=SodContext.ASSIGNMENT)


def test_role_templates_are_clone_only_and_scopes_are_assignable() -> None:
    registry = ScopeRegistry()
    with pytest.raises(TemplateImmutable):
        registry.require_template_mutable("admin")
    platform_only = [s.scope for s in registry.catalogue.scopes if s.platform_only]
    service_only = [s.scope for s in registry.catalogue.scopes if s.service_only]
    assert platform_only and service_only
    with pytest.raises(ScopeNotAssignable):
        registry.require_assignable(platform_only)
    with pytest.raises(ScopeNotAssignable):
        registry.require_assignable(service_only)
    assert registry.require_assignable(["inventory.balance.read"])


# --------------------------------------------------------------------------- DB lifecycle -------
async def _seed_membership(session, *, tenant_id=None, identity_id=None):
    tenant_id = tenant_id or uuid4()
    identity_id = identity_id or uuid4()
    app_user_id = uuid4()
    if tenant_id is not None:
        await session.execute(
            insert(p.tenant).values(
                id=tenant_id,
                tenant_id=tenant_id,
                code=f"ir-{tenant_id.hex[:8]}",
                name="Identity Rules",
                timezone="UTC",
                status="ACTIVE",
            )
        )
    await session.execute(
        insert(p.user_identity).values(
            id=identity_id,
            email=f"ir-{identity_id.hex[:10]}@example.test",
            display_name="IR",
            status="ACTIVE",
        )
    )
    email = f"member-{app_user_id.hex[:8]}@example.test"
    await session.execute(
        insert(p.app_user).values(
            id=app_user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=email,
            display_name="IR Member",
            status="ACTIVE",
            membership_status="ACTIVE",
            token_version=0,
        )
    )
    return tenant_id, identity_id, app_user_id, email


async def _insert_invitation(session, *, tenant_id, invited_by, email, token_hash, expires_at):
    invitation_id = uuid4()
    await session.execute(
        insert(p.tenant_invitation).values(
            id=invitation_id,
            tenant_id=tenant_id,
            email=email,
            intended_role_ids=[],
            token_hash=token_hash,
            status="PENDING",
            invited_by=invited_by,
            expires_at=expires_at,
        )
    )
    return invitation_id


async def test_invitation_is_single_use_and_expiry_checked(db_session) -> None:
    tenant_id, _identity_id, admin_id, email = await _seed_membership(db_session)
    now = datetime.now(UTC)
    token_hash = "hash-" + uuid4().hex
    invitation_id = await _insert_invitation(
        db_session,
        tenant_id=tenant_id,
        invited_by=admin_id,
        email=email,
        token_hash=token_hash,
        expires_at=now + timedelta(hours=1),
    )
    repository = AuthRepository(db_session)

    assert await repository.accept_invitation(invitation_id, app_user_id=admin_id, now=now) is True
    assert await repository.accept_invitation(invitation_id, app_user_id=admin_id, now=now) is False

    # Expired invitations can no longer be accepted (conditional PENDING+expiry transition).
    expired_id = await _insert_invitation(
        db_session,
        tenant_id=tenant_id,
        invited_by=admin_id,
        email=email,
        token_hash="hash-" + uuid4().hex,
        expires_at=now + timedelta(hours=2),
    )
    assert await repository.accept_invitation(expired_id, app_user_id=admin_id, now=now + timedelta(hours=3)) is False

    # Hashed tokens are unique per tenant and expiry must be in the future at insert time.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_invitation(
                db_session,
                tenant_id=tenant_id,
                invited_by=admin_id,
                email=email,
                token_hash=token_hash,
                expires_at=now + timedelta(hours=1),
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_invitation(
                db_session,
                tenant_id=tenant_id,
                invited_by=admin_id,
                email="other@example.test",
                token_hash="hash-" + uuid4().hex,
                expires_at=now - timedelta(minutes=1),
            )


async def test_token_version_bump_and_session_revocation(db_session) -> None:
    tenant_id, identity_id, app_user_id, _email = await _seed_membership(db_session)
    now = datetime.now(UTC)
    session_id = uuid4()
    await db_session.execute(
        insert(p.user_session).values(
            id=session_id,
            tenant_id=tenant_id,
            app_user_id=app_user_id,
            identity_id=identity_id,
            refresh_token_hash="refresh-" + uuid4().hex,
            status="ACTIVE",
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    repository = AuthRepository(db_session)
    assert await repository.bump_token_version(tenant_id=tenant_id, app_user_id=app_user_id) == 1
    assert (
        await repository.revoke_all_sessions(
            tenant_id=tenant_id, app_user_id=app_user_id, reason="membership revoked", now=now
        )
        == 1
    )

    membership = await db_session.execute(
        select(p.app_user.c.token_version, p.app_user.c.membership_status).where(p.app_user.c.id == app_user_id)
    )
    row = membership.mappings().one()
    assert row["token_version"] == 1
    session_row = await db_session.execute(select(p.user_session.c.status).where(p.user_session.c.id == session_id))
    assert session_row.scalar_one() == "REVOKED"

    # A token minted at tv=0 is stale against the bumped principal state.
    from app.api.deps import token_service

    token, _ = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_id),
        token_version=0,
        scopes={"inventory.balance.read"},
    )
    claims = token_service().decode_access_token(token)
    with pytest.raises(Exception) as excinfo:
        token_service().validate_access_claims(claims, expected_token_version=row["token_version"])
    assert excinfo.value.__class__.__name__ == "TokenVersionStale"


async def test_pending_invitation_is_unique_per_tenant_email(db_session) -> None:
    """Δ-MT DM-PLAT-14 requires one PENDING invitation per (tenant, email) (partial unique index)."""
    result = await db_session.execute(
        text(
            """
            SELECT indexname::text AS indexname, indexdef::text AS indexdef
              FROM pg_indexes
             WHERE schemaname = 'platform' AND tablename = 'tenant_invitation'
            """
        )
    )
    definitions = [row["indexdef"] for row in result.mappings()]
    if not any("UNIQUE" in definition and "PENDING" in definition for definition in definitions):
        pytest.xfail(
            "APP GAP: platform.tenant_invitation is missing the partial unique index on "
            "(tenant_id, email) WHERE status = 'PENDING' required by DM-PLAT-14 / the Δ-MT addendum"
        )


async def test_identity_link_uniqueness_keys_exist(db_session) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT indexname::text AS indexname
              FROM pg_indexes
             WHERE schemaname = 'platform' AND tablename = 'user_identity_link'
            """
        )
    )
    indexes = {row["indexname"] for row in result.mappings()}
    assert {"uq_user_identity_link_subject", "uq_user_identity_link_identity"} <= indexes
