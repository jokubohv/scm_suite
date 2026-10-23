"""FS-00 Δ-SSO OIDC flow with mocked Dex: start→callback→platform tokens/link, replay, enforced SSO."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.core.models.platform import user_identity_link
from app.tests.integration.platform.support import (
    SSO_SCOPES,
    activate_sso_connection,
    create_sso_connection,
    dex_claims,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration


async def _start(client, payload: dict):
    return await client.post("/api/v1/auth/sso/start", json=payload)


async def _enabled_connection(client, headers, **overrides):
    connection = await create_sso_connection(client, headers, **overrides)
    return await activate_sso_connection(client, headers, connection["id"])


async def test_start_callback_issues_platform_tokens_and_links_identity(
    app_client, platform_app, db_session
) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-SSO-A", scopes=tuple(SSO_SCOPES))
    connection = await _enabled_connection(
        app_client, admin["headers"], email_domains=["example.com"], jit_provisioning=True
    )
    started = await _start(app_client, {"email_or_tenant": "dev@example.com"})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["connection_id"] == connection["id"]
    assert body["protocol"] == "OIDC"
    assert body["local_login_allowed"] is True
    params = parse_qs(urlparse(body["authorization_url"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert body["state_expires_at"]

    platform_app.state.dex_client.claims = dex_claims(nonce=params["nonce"][0])
    callback = await app_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert callback.status_code == 200, callback.text
    tokens = callback.json()
    assert tokens["access_token"] and tokens["refresh_token"] and tokens["token_type"] == "Bearer"
    assert tokens["tenant_id"] == str(admin["tenant_id"])
    links = await db_session.execute(select(func.count()).select_from(user_identity_link))
    assert int(links.scalar_one()) == 1

    # State is single-use: replaying the callback is rejected with a typed SSO error.
    replay = await app_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert replay.status_code == 400 and replay.json()["code"] == "SSO_STATE_INVALID"

    # The platform access token identifies the SSO-created identity/tenant.
    me = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["tenant"]["id"] == str(admin["tenant_id"])


async def test_enforced_sso_domain_and_discovery_errors(app_client, platform_app, db_session) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-SSO-B", scopes=tuple(SSO_SCOPES))
    connection = await _enabled_connection(
        app_client, admin["headers"], email_domains=["example.com"], is_enforced=True
    )
    started = await _start(app_client, {"email_or_tenant": "EGT-SSO-B"})
    assert started.status_code == 200, started.text
    assert started.json()["local_login_allowed"] is False  # enforced SSO: no local fallback

    params = parse_qs(urlparse(started.json()["authorization_url"]).query)
    platform_app.state.dex_client.claims = dex_claims(nonce=params["nonce"][0], email="dev@other.example")
    mismatch = await app_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "SSO_EMAIL_DOMAIN_NOT_ALLOWED"

    unknown = await _start(app_client, {"email_or_tenant": "no-such-tenant"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "SSO_PROVIDER_NOT_FOUND"
    bad_state = await app_client.get("/api/v1/auth/sso/callback", params={"code": "x", "state": "unknown-state"})
    assert bad_state.status_code == 400 and bad_state.json()["code"] == "SSO_STATE_INVALID"

    disabled = await app_client.patch(
        f"/api/v1/admin/sso-connections/{connection['id']}", headers=admin["headers"], json={"status": "DISABLED"}
    )
    assert disabled.status_code == 200, disabled.text
    denied = await _start(app_client, {"email_or_tenant": "EGT-SSO-B"})
    assert denied.status_code == 404


async def test_start_requires_enabled_connection_and_public_no_auth(app_client, db_session) -> None:
    admin = await seed_admin_tenant(db_session, code="EGT-SSO-C", scopes=tuple(SSO_SCOPES))
    await create_sso_connection(app_client, admin["headers"], name="Draft only", email_domains=["draft.example"])
    draft = await _start(app_client, {"email_or_tenant": "user@draft.example"})
    assert draft.status_code == 404  # DRAFT connections are not discoverable
    # Start is public: no bearer token needed.
    no_auth = await _start(app_client, {"email_or_tenant": "user@unknown.example"})
    assert no_auth.status_code == 404 and no_auth.json()["code"] == "SSO_PROVIDER_NOT_FOUND"
