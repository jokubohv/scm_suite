"""Platform token service tests: JWT claims, clock, rotation/replay, API keys (pure)."""

from __future__ import annotations

import pytest

from app.core.security.errors import (
    CrossTenantAccessDenied,
    InvalidCredentials,
    MembershipInactive,
    SessionRevoked,
    TenantSuspended,
    TokenExpired,
    TokenInvalid,
    TokenVersionStale,
)
from app.core.security.tokens import (
    MIN_HMAC_SECRET_BYTES,
    ApiKeyHasher,
    HmacTokenSigner,
    InMemoryRefreshRotationStore,
    StaticSigningKeyRef,
    TokenConfig,
    TokenService,
    constant_time_compare,
    signer_from_key_ref,
)
from app.tests.unit.platform_security.helpers import HMAC_SECRET, PEPPER, StepClock

ISSUER = "inventory-platform-test"
AUDIENCE = "inventory-platform-api-test"


def make_service(clock: StepClock, *, secret: str = HMAC_SECRET) -> TokenService:
    return TokenService(
        HmacTokenSigner(secret.encode()),
        config=TokenConfig(issuer=ISSUER, audience=AUDIENCE, access_ttl_seconds=900, refresh_ttl_seconds=2_592_000),
        clock=clock,
    )


@pytest.fixture()
def clock() -> StepClock:
    return StepClock()


def test_issue_and_decode_token_pair(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(
        identity_id="identity-1",
        app_user_id="app-user-1",
        tenant_id="tenant-1",
        token_version=3,
        scopes={"items.read", "platform.users.manage"},
        session_id="session-1",
    )
    access = service.decode_access_token(pair.access_token)
    assert (access.sub, access.uid, access.tid) == ("identity-1", "app-user-1", "tenant-1")
    assert access.tv == 3
    assert access.scopes == frozenset({"items.read", "platform.users.manage"})
    assert access.iss == ISSUER and access.aud == AUDIENCE
    assert access.exp - access.iat == pair.access_claims.exp - pair.access_claims.iat
    refresh = service.decode_refresh_token(pair.refresh_token)
    assert (refresh.uid, refresh.tid, refresh.sid) == ("app-user-1", "tenant-1", "session-1")
    assert pair.token_type == "Bearer"
    assert pair.expires_in == 900


def test_expiry_is_clock_injected(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(
        identity_id="i",
        app_user_id="u",
        tenant_id="t",
        token_version=0,
        scopes=(),
        session_id="s",
    )
    clock.advance(901)
    with pytest.raises(TokenExpired):
        service.decode_access_token(pair.access_token)
    clock.advance(2_592_000)
    with pytest.raises(TokenExpired):
        service.decode_refresh_token(pair.refresh_token)


def test_tampered_or_wrong_secret_token_is_invalid(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(identity_id="i", app_user_id="u", tenant_id="t", token_version=0, scopes=(), session_id="s")
    tampered = pair.access_token[:-2] + ("aa" if pair.access_token[-2:] != "aa" else "bb")
    with pytest.raises(TokenInvalid):
        service.decode_access_token(tampered)
    other = make_service(clock, secret="another-unit-test-hmac-secret-0123456789")
    with pytest.raises(TokenInvalid):
        other.decode_access_token(pair.access_token)


def test_token_type_confusion_is_rejected(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(identity_id="i", app_user_id="u", tenant_id="t", token_version=0, scopes=(), session_id="s")
    with pytest.raises(TokenInvalid):
        service.decode_refresh_token(pair.access_token)
    with pytest.raises(TokenInvalid):
        service.decode_access_token(pair.refresh_token)


def test_issuer_audience_mismatch(clock: StepClock) -> None:
    signer = HmacTokenSigner(HMAC_SECRET.encode())
    foreign = TokenService(signer, config=TokenConfig(issuer="other-issuer", audience=AUDIENCE), clock=clock)
    pair = foreign.issue_token_pair(identity_id="i", app_user_id="u", tenant_id="t", token_version=0, scopes=(), session_id="s")
    service = make_service(clock)
    with pytest.raises(TokenInvalid):
        service.decode_access_token(pair.access_token)


def test_validate_access_claims_guards(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(
        identity_id="i1",
        app_user_id="u1",
        tenant_id="t1",
        token_version=4,
        scopes=("items.read",),
        session_id="s1",
    )
    claims = service.decode_access_token(pair.access_token)
    service.validate_access_claims(claims, expected_token_version=4)
    with pytest.raises(TokenVersionStale):
        service.validate_access_claims(claims, expected_token_version=5)
    with pytest.raises(MembershipInactive):
        service.validate_access_claims(claims, expected_token_version=4, membership_status="SUSPENDED")
    with pytest.raises(TenantSuspended):
        service.validate_access_claims(claims, expected_token_version=4, tenant_status="SUSPENDED")
    with pytest.raises(CrossTenantAccessDenied):
        service.validate_access_claims(claims, expected_token_version=4, expected_tenant_id="t2")
    with pytest.raises(TokenInvalid):
        service.validate_access_claims(claims, expected_token_version=4, expected_identity_id="i2")


def test_refresh_rotation_and_replay_detection(clock: StepClock) -> None:
    service = make_service(clock)
    pair = service.issue_token_pair(identity_id="i", app_user_id="u", tenant_id="t", token_version=2, scopes=("items.read",), session_id="s")
    store = InMemoryRefreshRotationStore()
    refresh = service.decode_refresh_token(pair.refresh_token)
    rotated = service.rotate_refresh_token(refresh, store=store, expected_token_version=2, scopes=("items.read",), now=clock.advance(60))
    assert rotated.refresh_claims.jti != refresh.jti
    assert rotated.refresh_claims.sid == refresh.sid == "s"
    # Re-using the rotated token is replay -> session revoked.
    with pytest.raises(SessionRevoked):
        service.rotate_refresh_token(refresh, store=store, expected_token_version=2, scopes=("items.read",))
    assert store.is_session_revoked("s")
    with pytest.raises(SessionRevoked):
        service.validate_refresh_claims(service.decode_refresh_token(rotated.refresh_token), expected_token_version=2, session_active=False)
    with pytest.raises(TokenVersionStale):
        service.rotate_refresh_token(service.decode_refresh_token(rotated.refresh_token), store=store, expected_token_version=3, scopes=())


def test_selection_token_is_identity_scoped_and_not_access(clock: StepClock) -> None:
    service = make_service(clock)
    token, claims = service.issue_selection_token(identity_id="identity-9")
    decoded = service.decode_selection_token(token)
    assert decoded.sub == "identity-9"
    assert decoded.exp - decoded.iat == claims.exp - claims.iat
    with pytest.raises(TokenInvalid):
        service.decode_access_token(token)
    clock.advance(301)
    with pytest.raises(TokenExpired):
        service.decode_selection_token(token)


def test_hmac_secret_policy_and_key_ref_signer() -> None:
    with pytest.raises(ValueError):
        HmacTokenSigner(b"short")
    ref = StaticSigningKeyRef(algorithm="HS256", private_key_pem=HMAC_SECRET)
    signer = signer_from_key_ref(ref)
    assert signer.algorithm == "HS256"
    assert MIN_HMAC_SECRET_BYTES == 32


def test_api_key_generation_hash_and_constant_time_verify() -> None:
    hasher = ApiKeyHasher(PEPPER)
    material = hasher.generate()
    assert material.full_key.startswith(material.key_prefix + ".")
    assert hasher.verify(material.full_key, material.secret_hash) is True
    assert hasher.verify(material.full_key, "0" * 64) is False
    assert hasher.verify(material.key_prefix + ".wrong-secret-value-but-long", material.secret_hash) is False
    parts = hasher.parse(material.full_key)
    assert parts.prefix == material.key_prefix
    assert hasher.split_prefix(material.full_key) == material.key_prefix
    with pytest.raises(InvalidCredentials):
        hasher.parse("not-a-valid-key")
    assert constant_time_compare("abc", "abc") is True
    assert constant_time_compare("abc", "abd") is False
    assert constant_time_compare(b"abc", "abc") is True
    with pytest.raises(ValueError):
        ApiKeyHasher("")
