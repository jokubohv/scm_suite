"""Δ-SSO public login flow: start (state/nonce/PKCE) and callback (mocked Dex, no live network)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.core.models.platform import user_identity_link
from app.tests.integration.platform_sso.conftest import create_connection, dex_claims, seed_tenant

pytestmark = pytest.mark.integration


async def _start(sso_client, payload: dict):
    return await sso_client.post("/api/v1/auth/sso/start", json=payload)


async def _active_connection(sso_client, headers, **overrides):
    connection = await create_connection(sso_client, headers, **overrides)
    tested = await sso_client.post(f"/api/v1/admin/sso-connections/{connection['id']}/test", headers=headers)
    assert tested.status_code == 200, tested.text
    return tested.json()


async def test_start_callback_issues_platform_tokens_and_links_identity(sso_client, sso_app, db_session) -> None:
    tenant = await seed_tenant(db_session, code="echo")
    connection = await _active_connection(
        sso_client, tenant["headers"], email_domains=["example.com"], jit_provisioning=True
    )
    started = await _start(sso_client, {"email_or_tenant": "example.com"})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["connection_id"] == connection["id"]
    assert body["protocol"] == "OIDC"
    parsed = urlparse(body["authorization_url"])
    params = parse_qs(parsed.query)
    assert params["code_challenge_method"] == ["S256"]
    assert body["state_expires_at"]

    sso_app.state.dex_client.claims = dex_claims(nonce=params["nonce"][0])
    callback = await sso_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert callback.status_code == 200, callback.text
    tokens = callback.json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["token_type"] == "Bearer"
    assert tokens["tenant_id"] == tenant["tenant_id"]
    links = await db_session.execute(select(func.count()).select_from(user_identity_link))
    assert int(links.scalar_one()) == 1

    # Replaying the same state is rejected (single-use, tenant-bound).
    replay = await sso_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert replay.status_code == 400
    assert replay.json()["code"] == "SSO_STATE_INVALID"


async def test_start_errors_enforced_policy_and_domain_mismatch(sso_client, sso_app, db_session) -> None:
    tenant = await seed_tenant(db_session, code="foxtrot")
    connection = await _active_connection(
        sso_client, tenant["headers"], email_domains=["example.com"], is_enforced=True
    )
    started = await _start(sso_client, {"email_or_tenant": "foxtrot"})
    assert started.status_code == 200
    assert started.json()["local_login_allowed"] is False  # enforced SSO: no local fallback

    params = parse_qs(urlparse(started.json()["authorization_url"]).query)
    sso_app.state.dex_client.claims = dex_claims(nonce=params["nonce"][0], email="dev@other.example")
    mismatch = await sso_client.get(
        "/api/v1/auth/sso/callback", params={"code": "fake-code", "state": params["state"][0]}
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "SSO_EMAIL_DOMAIN_NOT_ALLOWED"

    unknown = await _start(sso_client, {"email_or_tenant": "no-such-tenant"})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "SSO_PROVIDER_NOT_FOUND"

    bad_state = await sso_client.get("/api/v1/auth/sso/callback", params={"code": "x", "state": "unknown-state"})
    assert bad_state.status_code == 400
    assert bad_state.json()["code"] == "SSO_STATE_INVALID"

    # A disabled connection cannot start a login (typed deny, no silent fallback).
    patch = await sso_client.patch(
        f"/api/v1/admin/sso-connections/{connection['id']}",
        json={"status": "DISABLED"},
        headers=tenant["headers"],
    )
    assert patch.status_code == 200
    denied = await _start(sso_client, {"email_or_tenant": "foxtrot"})
    assert denied.status_code == 404
