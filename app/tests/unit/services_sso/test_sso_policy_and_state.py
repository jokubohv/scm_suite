"""Unit tests: authorization-request state/PKCE, ID-token claim validation, enforcement policy,
group-mapping bounds/SoD and the in-memory single-use state store (no DB/network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.api.routers.platform.sso import InMemorySsoStateStore
from app.core.security import authn
from app.core.security.errors import (
    SsoEmailDomainNotAllowed,
    SsoEnforcedLocalDenied,
    SsoGroupMappingInvalid,
    SsoIdTokenInvalid,
    SsoStateInvalid,
)
from app.core.security.scopes import load_catalogue


def _connection(
    *,
    status: authn.SsoConnectionStatus = authn.SsoConnectionStatus.ACTIVE,
    is_enforced: bool = False,
    jit: bool = False,
) -> authn.SsoConnectionView:
    return authn.SsoConnectionView(
        id="conn-1",
        tenant_id="00000000-0000-0000-0000-000000000001",
        tenant_code="acme",
        tenant_name="Acme Inc",
        protocol=authn.SsoProtocol.OIDC,
        status=status,
        dex_issuer_url="http://localhost:5556/dex",
        client_id="inventory-platform",
        email_domains=("acme.example",),
        is_enforced=is_enforced,
        jit_provisioning=jit,
    )


def test_authorization_request_is_tenant_bound_and_single_use() -> None:
    connection = _connection()
    request = authn.create_authorization_request(
        connection,
        redirect_uri="http://localhost:8000/api/v1/auth/sso/callback",
        redirect_allowlist=("http://localhost:8000/api/v1/auth/sso/callback",),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert request.connection_id == connection.id
    assert request.tenant_id == connection.tenant_id
    assert request.issuer == "http://localhost:5556/dex"
    assert request.redirect_uri.endswith("/auth/sso/callback")
    assert len(request.code_verifier) >= authn.PKCE_VERIFIER_MIN_LENGTH
    assert request.code_challenge == authn.s256_code_challenge(request.code_verifier)
    url = authn.authorization_url(request, connection)
    assert "code_challenge_method=S256" in url
    assert f"state={request.state}" in url and f"nonce={request.nonce}" in url

    now = datetime(2026, 1, 1, tzinfo=UTC)
    used = authn.validate_callback_state(request, state=request.state, now=now)
    assert used.used is True
    with pytest.raises(SsoStateInvalid):
        authn.validate_callback_state(used, state=request.state, now=now)
    with pytest.raises(SsoStateInvalid):
        authn.validate_callback_state(request, state="wrong-state", now=now)


def test_callback_state_expiry_rejected() -> None:
    connection = _connection()
    request = authn.create_authorization_request(
        connection,
        redirect_uri="http://localhost:8000/api/v1/auth/sso/callback",
        redirect_allowlist=("http://localhost:8000/api/v1/auth/sso/callback",),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        ttl_seconds=5,
    )
    with pytest.raises(SsoStateInvalid):
        authn.validate_callback_state(
            request, state=request.state, now=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=6)
        )


def test_disabled_connection_cannot_start_sso() -> None:
    with pytest.raises(Exception) as excinfo:
        authn.validate_connection_usable(_connection(status=authn.SsoConnectionStatus.DISABLED))
    assert excinfo.value.code == "SSO_DISABLED"
    # A non-ACTIVE connection is not discoverable for routing at all (no silent downgrade).
    with pytest.raises(Exception) as not_found:
        authn.resolve_connection_for_start(
            "acme", connections=[_connection(status=authn.SsoConnectionStatus.DISABLED)]
        )
    assert not_found.value.code == "SSO_PROVIDER_NOT_FOUND"


def test_redirect_uri_allowlist_enforced() -> None:
    connection = _connection()
    with pytest.raises(Exception) as excinfo:
        authn.create_authorization_request(
            connection,
            redirect_uri="https://evil.example/callback",
            redirect_allowlist=("http://localhost:8000/api/v1/auth/sso/callback",),
        )
    assert excinfo.value.code == "SSO_CONNECTION_INVALID"


def _claims(**overrides):
    base = {
        "iss": "http://localhost:5556/dex",
        "aud": "inventory-platform",
        "sub": "dev-user-0001",
        "nonce": "n-1",
        "iat": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()),
        "exp": int(datetime(2026, 1, 1, 0, 5, tzinfo=UTC).timestamp()),
        "email": "dev@example.com",
        "email_verified": True,
        "groups": ["platform-admins"],
    }
    base.update(overrides)
    return base


def test_id_token_claims_are_strictly_validated() -> None:
    now = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    claims = authn.validate_id_token_claims(
        _claims(),
        expected_issuer="http://localhost:5556/dex",
        client_id="inventory-platform",
        expected_nonce="n-1",
        now=now,
    )
    assert claims.email == "dev@example.com" and claims.email_verified
    assert claims.groups == ("platform-admins",)

    for override, reason in (
        ({"iss": "http://other/dex"}, "ISSUER_MISMATCH"),
        ({"aud": "other-client"}, "AUDIENCE_MISMATCH"),
        ({"nonce": "other"}, "NONCE_MISMATCH"),
        ({"exp": int(now.timestamp()) - 120}, "TOKEN_EXPIRED"),
        ({"email_verified": False}, "EMAIL_NOT_VERIFIED"),
    ):
        with pytest.raises(SsoIdTokenInvalid) as excinfo:
            authn.validate_id_token_claims(
                _claims(**override),
                expected_issuer="http://localhost:5556/dex",
                client_id="inventory-platform",
                expected_nonce="n-1",
                now=now,
            )
        assert excinfo.value.details.get("reason") or excinfo.value.code == "SSO_ID_TOKEN_INVALID"


def test_enforced_sso_blocks_local_login_but_break_glass_allowed() -> None:
    enforced = _connection(is_enforced=True)
    assert authn.local_login_allowed(enforced) is False
    with pytest.raises(SsoEnforcedLocalDenied):
        authn.enforce_local_login_allowed(enforced)
    assert authn.local_login_allowed(enforced, is_platform_admin=True, break_glass_enabled=True) is True
    assert authn.local_login_allowed(_connection(is_enforced=False)) is True


def test_email_domain_policy() -> None:
    connection = _connection()
    authn.ensure_email_domain_allowed("dev@acme.example", connection)
    with pytest.raises(SsoEmailDomainNotAllowed):
        authn.ensure_email_domain_allowed("dev@other.example", connection)


def test_group_mapping_validation_and_sod_bounds() -> None:
    registry = load_catalogue()
    mappings = [
        authn.GroupMapping("EXACT", "platform-admins", "admin", priority=10, mapping_id="m1"),
    ]
    validated = authn.validate_group_mappings(
        mappings,
        known_role_keys=registry.templates_by_key.keys(),
        assignable_role_keys=registry.templates_by_key.keys(),
    )
    assert validated[0].role_key == "admin"
    with pytest.raises(SsoGroupMappingInvalid):
        authn.validate_group_mappings(
            [authn.GroupMapping("EXACT", "ghost", "not-a-role")],
            known_role_keys=registry.templates_by_key.keys(),
            assignable_role_keys=registry.templates_by_key.keys(),
        )
    with pytest.raises(SsoGroupMappingInvalid):
        authn.validate_group_mappings(
            [mappings[0], mappings[0]],
            known_role_keys=registry.templates_by_key.keys(),
            assignable_role_keys=registry.templates_by_key.keys(),
        )
    # A mapping granting an SoD pair is rejected when two non-admin roles conflict.
    pd = next(pair for pair in registry.sod_pairs if pair.a == "procurement.po.create")
    role_scopes = {f"role-{pd.a}": frozenset({pd.a}), f"role-{pd.b}": frozenset({pd.b})}
    sod_mappings = [
        authn.GroupMapping("EXACT", "group-a", f"role-{pd.a}", mapping_id="a"),
        authn.GroupMapping("EXACT", "group-b", f"role-{pd.b}", mapping_id="b"),
    ]
    with pytest.raises(Exception) as excinfo:
        authn.evaluate_group_role_mappings(
            ["group-a", "group-b"],
            sod_mappings,
            role_scope_sets=role_scopes,
            assignable_role_keys=role_scopes.keys(),
            sod_pairs=((pd.a, pd.b),),
        )
    assert excinfo.value.code == "SOD_CONFLICT"


async def test_in_memory_state_store_is_single_use() -> None:
    store = InMemorySsoStateStore()
    connection = _connection()
    request = authn.create_authorization_request(
        connection,
        redirect_uri="http://localhost:8000/api/v1/auth/sso/callback",
        redirect_allowlist=("http://localhost:8000/api/v1/auth/sso/callback",),
    )
    await store.put(request)
    first = await store.pop(request.state)
    assert first is not None and first.state == request.state
    assert await store.pop(request.state) is None


async def test_in_memory_state_store_drops_expired() -> None:
    store = InMemorySsoStateStore()
    connection = _connection()
    request = authn.create_authorization_request(
        connection,
        redirect_uri="http://localhost:8000/api/v1/auth/sso/callback",
        redirect_allowlist=("http://localhost:8000/api/v1/auth/sso/callback",),
        now=datetime(2020, 1, 1, tzinfo=UTC),
        ttl_seconds=1,
    )
    await store.put(request)
    assert await store.pop(request.state) is None
