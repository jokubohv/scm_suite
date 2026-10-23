"""SSO authentication policy tests (pure; Dex broker decisions, no network)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.core.security.authn import (
    GroupMapping,
    IdentityDenialReason,
    IdentityResolutionKind,
    InMemoryConnectionLookup,
    SsoConnectionStatus,
    authorization_url,
    create_authorization_request,
    decide_identity_resolution,
    email_domain,
    enforce_local_login_allowed,
    evaluate_group_mappings_with_registry,
    evaluate_group_role_mappings,
    find_connections,
    local_login_allowed,
    normalize_email,
    require_identity_resolution_allowed,
    resolve_connection_for_start,
    resolve_login_routing,
    s256_code_challenge,
    validate_callback_state,
    validate_connection_usable,
    validate_group_mappings,
    validate_id_token_claims,
)
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
from app.tests.unit.platform_security.helpers import (
    CATALOGUE,
    REGISTRY,
    StepClock,
    assignable_role_keys,
    make_connection,
)


def test_connection_view_normalises_domains_and_public_description() -> None:
    connection = make_connection(email_domains=("Example.COM", "example.com", " acme.io "))
    assert connection.email_domains == ("acme.io", "example.com")
    assert connection.is_active is True
    assert connection.describes()["login_url"].endswith("/auth")
    assert "client_id" not in connection.describes()  # public routing info only
    with pytest.raises(SsoConnectionInvalid):
        make_connection(issuer="ftp://bad")


def test_email_helpers_and_domain_policy() -> None:
    assert normalize_email("  User@Example.com ") == "user@example.com"
    assert email_domain("user@example.com") == "example.com"
    connection = make_connection(email_domains=("example.com", "example.com"))
    from app.core.security.authn import domain_allowed, ensure_email_domain_allowed

    assert domain_allowed("user@example.com", connection) is True
    assert domain_allowed("user@other.com", connection) is False
    with pytest.raises(SsoEmailDomainNotAllowed):
        ensure_email_domain_allowed("user@other.com", connection)
    with pytest.raises(SsoConnectionInvalid):
        normalize_email("not-an-email")


def test_connection_usable_only_when_active() -> None:
    active = make_connection()
    validate_connection_usable(active)
    for status in (SsoConnectionStatus.DRAFT, SsoConnectionStatus.TESTING, SsoConnectionStatus.DISABLED, SsoConnectionStatus.ERROR):
        with pytest.raises(SsoDisabled):
            validate_connection_usable(make_connection(status=status))


def test_enforced_sso_local_fallback_and_break_glass() -> None:
    plain = make_connection(is_enforced=False)
    enforced = make_connection(is_enforced=True)
    assert local_login_allowed(None) is True
    assert local_login_allowed(plain) is True
    assert local_login_allowed(enforced) is False
    assert local_login_allowed(enforced, is_platform_admin=True) is True
    assert local_login_allowed(enforced, is_platform_admin=True, break_glass_enabled=False) is False
    enforce_local_login_allowed(plain)
    with pytest.raises(SsoEnforcedLocalDenied):
        enforce_local_login_allowed(enforced)


def test_discovery_routing_single_multi_none() -> None:
    connection = make_connection()
    assert find_connections("ten0001", (connection,)) == (connection,)
    assert find_connections("user@example.com", (connection,)) == (connection,)
    assert find_connections("user@other.com", (connection,)) == ()

    none = resolve_login_routing("user@other.com", connections=(connection,))
    assert none.local_allowed is True and none.sso_choices == ()

    single = resolve_login_routing("TEN0001", connections=(connection,))
    assert single.local_allowed is True
    assert single.selected_connection_id == connection.id
    assert single.requires_tenant_choice is False
    assert single.methods == ("LOCAL", "SSO")

    second = make_connection(connection_id="conn-2", tenant_id="tenant-2", tenant_code="TEN0002", email_domains=("example.com",))
    multi = resolve_login_routing("user@example.com", connections=(connection, second))
    assert multi.requires_tenant_choice is True
    assert multi.local_allowed is False
    assert len(multi.sso_choices) == 2

    enforced = resolve_login_routing("TEN0001", connections=(make_connection(is_enforced=True),))
    assert enforced.local_allowed is False
    break_glass = resolve_login_routing(
        "TEN0001", connections=(make_connection(is_enforced=True),), platform_admin_break_glass=True
    )
    assert break_glass.local_allowed is True


def test_resolve_connection_for_start_and_ambiguity() -> None:
    first = make_connection()
    second = make_connection(connection_id="conn-2", tenant_id="tenant-2", tenant_code="TEN0002", email_domains=("example.com",))
    assert resolve_connection_for_start("TEN0001", connections=(first, second)) is first
    assert resolve_connection_for_start("user@example.com", connections=(first, second), requested_tenant_id="tenant-2") is second
    with pytest.raises(SsoProviderNotFound):
        resolve_connection_for_start("user@unknown.com", connections=(first, second))
    with pytest.raises(SsoConnectionInvalid):
        resolve_connection_for_start("user@example.com", connections=(first, second))
    lookup = InMemoryConnectionLookup((first, second))
    assert lookup.by_id("conn-2") is second
    assert lookup.by_tenant_code("ten0001") == (first,)
    assert lookup.by_email_domain("example.com") == (first, second)


def test_identity_resolution_link_by_email_jit_and_denials() -> None:
    linked = decide_identity_resolution(
        existing_link_identity_id="identity-1",
        email_verified=True,
        domain_allowed=True,
        jit_provisioning=False,
    )
    assert linked.kind is IdentityResolutionKind.LINKED and linked.link_required is False

    unverified = decide_identity_resolution(email_verified=False, domain_allowed=True, jit_provisioning=True)
    assert unverified.kind is IdentityResolutionKind.DENIED
    assert unverified.denial_reason is IdentityDenialReason.EMAIL_NOT_VERIFIED

    wrong_domain = decide_identity_resolution(email_verified=True, domain_allowed=False, jit_provisioning=True)
    assert wrong_domain.denial_reason is IdentityDenialReason.EMAIL_DOMAIN_NOT_ALLOWED

    by_email = decide_identity_resolution(
        linked_member_identity_id="identity-2",
        identity_exists=True,
        email_verified=True,
        domain_allowed=True,
        jit_provisioning=False,
    )
    assert by_email.kind is IdentityResolutionKind.LINK_BY_VERIFIED_EMAIL

    jit = decide_identity_resolution(
        identity_exists=False, email_verified=True, domain_allowed=True, jit_provisioning=True
    )
    assert jit.kind is IdentityResolutionKind.JIT_PROVISION
    assert jit.create_identity is True and jit.create_membership is True

    disabled = decide_identity_resolution(email_verified=True, domain_allowed=True, jit_provisioning=False)
    assert disabled.denial_reason is IdentityDenialReason.JIT_PROVISIONING_DISABLED
    require_identity_resolution_allowed(linked)
    with pytest.raises(JitProvisioningDisabled):
        require_identity_resolution_allowed(disabled)


def test_pkce_authorization_request_and_single_use_state(clock: StepClock) -> None:
    connection = make_connection()
    redirect = "https://app.example.com/auth/callback"
    request = create_authorization_request(
        connection, redirect_uri=redirect, redirect_allowlist=(redirect,), now=clock.now()
    )
    verifier_challenge = base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert request.code_challenge == verifier_challenge == s256_code_challenge(request.code_verifier)
    assert len(request.state) >= 16 and len(request.nonce) >= 16
    url = authorization_url(request, connection)
    assert url.startswith(connection.dex_issuer_url + "/auth?")
    assert "code_challenge_method=S256" in url and "state=" in url and "nonce=" in url

    with pytest.raises(SsoConnectionInvalid):
        create_authorization_request(connection, redirect_uri="https://evil.example.com/cb", redirect_allowlist=(redirect,), now=clock.now())
    with pytest.raises(SsoConnectionInvalid):
        s256_code_challenge("too-short")

    consumed = validate_callback_state(request, state=request.state, now=clock.now())
    assert consumed.used is True
    with pytest.raises(SsoStateInvalid):
        validate_callback_state(consumed, state=request.state, now=clock.now())  # replay
    with pytest.raises(SsoStateInvalid):
        validate_callback_state(request, state="wrong-state", now=clock.now())
    clock.advance(301)
    with pytest.raises(SsoStateInvalid):
        validate_callback_state(request, state=request.state, now=clock.now())


def test_id_token_claim_validation(clock: StepClock) -> None:
    connection = make_connection()
    issued_at = clock.now()
    claims = {
        "iss": connection.dex_issuer_url,
        "aud": connection.client_id,
        "sub": "subject-1",
        "email": "user@example.com",
        "email_verified": True,
        "iat": int(issued_at.timestamp()),
        "exp": int(issued_at.timestamp()) + 300,
        "nonce": "nonce-value",
        "groups": ["finance", "inventory-ops"],
    }
    validated = validate_id_token_claims(
        claims,
        expected_issuer=connection.dex_issuer_url,
        client_id=connection.client_id,
        expected_nonce="nonce-value",
        now=clock.now(),
    )
    assert validated.subject == "subject-1" and validated.email == "user@example.com"
    assert validated.groups == ("finance", "inventory-ops")

    def expect(reason: str, **overrides: object) -> None:
        with pytest.raises(SsoIdTokenInvalid) as error:
            validate_id_token_claims(
                {**claims, **overrides},
                expected_issuer=connection.dex_issuer_url,
                client_id=connection.client_id,
                expected_nonce="nonce-value",
                now=clock.now(),
            )
        assert error.value.details.get("reason") == reason

    expect("ISSUER_MISMATCH", iss="https://evil.example.com")
    expect("AUDIENCE_MISMATCH", aud="other-client")
    expect("NONCE_MISMATCH", nonce="other-nonce")
    expect("EMAIL_NOT_VERIFIED", email_verified=False)
    expect("TOKEN_EXPIRED", exp=int(issued_at.timestamp()) - 60)
    expect("IAT_IN_FUTURE", iat=int(issued_at.timestamp()) + 600)
    with pytest.raises(SsoIdTokenInvalid):
        validate_id_token_claims(
            {**claims, "email": ""},
            expected_issuer=connection.dex_issuer_url,
            client_id=connection.client_id,
            expected_nonce="nonce-value",
            now=clock.now(),
        )


def test_group_mapping_validation_and_bounds() -> None:
    role_sets = {
        "readonly": frozenset({"items.read"}),
        "planner": frozenset({"items.read", "planning.run"}),
        "service_role": frozenset({"platform.internal"}),
    }
    assignable = {"readonly", "planner"}
    mappings = [
        GroupMapping("EXACT", "finance", "readonly", priority=10),
        GroupMapping("REGEX", r"^inv-.*", "planner", priority=20),
        GroupMapping("EXACT", "ignored", "planner", priority=30, is_active=False),
    ]
    assert len(validate_group_mappings(mappings, known_role_keys=role_sets, assignable_role_keys=assignable)) == 3
    with pytest.raises(SsoGroupMappingInvalid):
        validate_group_mappings(
            [GroupMapping("EXACT", "g", "unknown", 1)], known_role_keys=role_sets, assignable_role_keys=assignable
        )
    with pytest.raises(SsoGroupMappingInvalid):
        validate_group_mappings(
            [GroupMapping("EXACT", "g", "service_role", 1)], known_role_keys=role_sets, assignable_role_keys=assignable
        )
    with pytest.raises(SsoGroupMappingInvalid):
        GroupMapping("REGEX", "[", "readonly")

    result = evaluate_group_role_mappings(
        ("finance", "inv-ops"),
        mappings,
        role_scope_sets=role_sets,
        assignable_role_keys=assignable,
    )
    assert result.role_keys == ("readonly", "planner")
    assert result.scopes == frozenset({"items.read", "planning.run"})


def test_group_mapping_sod_reject_and_admin_break_glass() -> None:
    role_sets = {
        "poster": frozenset({"inventory.adjustment.post"}),
        "approver": frozenset({"inventory.adjustment.approve"}),
        "admin": CATALOGUE.template("admin").scopes,
    }
    assignable = set(role_sets)
    pair = (("inventory.adjustment.post", "inventory.adjustment.approve"),)
    mappings = [
        GroupMapping("EXACT", "posters", "poster", priority=1),
        GroupMapping("EXACT", "approvers", "approver", priority=2),
    ]
    with pytest.raises(SodConflict):
        evaluate_group_role_mappings(
            ("posters", "approvers"),
            mappings,
            role_scope_sets=role_sets,
            assignable_role_keys=assignable,
            sod_pairs=pair,
        )
    admin_mapping = [GroupMapping("EXACT", "platform-admins", "admin")]
    result = evaluate_group_role_mappings(
        ("platform-admins",),
        admin_mapping,
        role_scope_sets=role_sets,
        assignable_role_keys=assignable,
        sod_pairs=pair,
    )
    assert result.break_glass is True and "admin" in result.role_keys


def test_group_mapping_with_registry_uses_catalogue() -> None:
    mappings = [
        GroupMapping("EXACT", "readers", "readonly", priority=1),
        GroupMapping("EXACT", "planners", "planner", priority=2),
    ]
    result = evaluate_group_mappings_with_registry(
        ("readers", "planners"),
        mappings,
        registry=REGISTRY,
    )
    assert set(result.role_keys) == {"readonly", "planner"}
    assert result.scopes == CATALOGUE.template("readonly").scopes | CATALOGUE.template("planner").scopes
    assert result.scopes <= CATALOGUE.assignable_scopes()
    assert assignable_role_keys() == tuple(CATALOGUE.templates_by_key.keys())
