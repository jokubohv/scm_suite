"""FS-00 Δ-SSO tenant isolation: cross-tenant connections/links are 404/no-leak and tenant-scoped."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.api.deps import token_service
from app.tests.integration.platform.support import (
    SSO_SCOPES,
    activate_sso_connection,
    create_sso_connection,
    dex_claims,
    seed_admin_tenant,
)

pytestmark = pytest.mark.integration


async def _active_connection(client, headers, **overrides):
    connection = await create_sso_connection(client, headers, **overrides)
    return await activate_sso_connection(client, headers, connection["id"])


async def test_cross_tenant_connection_access_is_404_without_leak(app_client, db_session) -> None:
    tenant_a = await seed_admin_tenant(db_session, code="EGT-ISO-A", scopes=tuple(SSO_SCOPES))
    tenant_b = await seed_admin_tenant(db_session, code="EGT-ISO-B", scopes=tuple(SSO_SCOPES))
    connection = await _active_connection(app_client, tenant_a["headers"], email_domains=["iso-a.example"])

    # B cannot read, patch or delete A's connection: all indistinguishable from not-found (404).
    for method, url, kwargs in (
        ("GET", f"/api/v1/admin/sso-connections/{connection['id']}", {}),
        ("PATCH", f"/api/v1/admin/sso-connections/{connection['id']}", {"json": {"name": "hijacked"}}),
        ("DELETE", f"/api/v1/admin/sso-connections/{connection['id']}", {}),
        ("GET", f"/api/v1/admin/sso-connections/{connection['id']}/group-mappings", {}),
        ("GET", f"/api/v1/admin/sso-connections/{connection['id']}/links", {}),
    ):
        response = await app_client.request(method, url, headers=tenant_b["headers"], **kwargs)
        assert response.status_code == 404, f"{method} {url}: {response.text}"
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "SSO_CONNECTION_NOT_FOUND"
        assert "iso-a.example" not in response.text

    other_list = await app_client.get("/api/v1/admin/sso-connections", headers=tenant_b["headers"])
    assert other_list.status_code == 200 and other_list.json()["items"] == []
    own_view = await app_client.get(f"/api/v1/admin/sso-connections/{connection['id']}", headers=tenant_a["headers"])
    assert own_view.status_code == 200 and own_view.json()["id"] == connection["id"]


async def test_sso_callback_tokens_and_links_are_tenant_scoped(app_client, platform_app, db_session) -> None:
    tenant_a = await seed_admin_tenant(db_session, code="EGT-ISO-C", scopes=tuple(SSO_SCOPES))
    tenant_b = await seed_admin_tenant(db_session, code="EGT-ISO-D", scopes=tuple(SSO_SCOPES))
    await _active_connection(app_client, tenant_a["headers"], email_domains=["iso.example"], jit_provisioning=True)
    await _active_connection(app_client, tenant_b["headers"], email_domains=["other.example"], jit_provisioning=True)

    started = await app_client.post("/api/v1/auth/sso/start", json={"email_or_tenant": "user@iso.example"})
    assert started.status_code == 200, started.text
    params = parse_qs(urlparse(started.json()["authorization_url"]).query)
    platform_app.state.dex_client.claims = dex_claims(nonce=params["nonce"][0], email="user@iso.example")
    callback = await app_client.get(
        "/api/v1/auth/sso/callback", params={"code": "code", "state": params["state"][0]}
    )
    assert callback.status_code == 200, callback.text
    tokens = callback.json()
    claims = token_service().decode_access_token(tokens["access_token"])
    assert claims.tid == str(tenant_a["tenant_id"])
    assert claims.tid != str(tenant_b["tenant_id"])

    # B's admin still cannot inspect A's connection/links.
    cross = await app_client.get(
        f"/api/v1/admin/sso-connections/{tokens['connection_id']}/links", headers=tenant_b["headers"]
    )
    assert cross.status_code == 404 and cross.json()["code"] == "SSO_CONNECTION_NOT_FOUND"
    own_links = await app_client.get(
        f"/api/v1/admin/sso-connections/{tokens['connection_id']}/links", headers=tenant_a["headers"]
    )
    assert own_links.status_code == 200, own_links.text
    assert len(own_links.json()["items"]) == 1

    me = await app_client.get("/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["tenant"]["id"] == str(tenant_a["tenant_id"])
    memberships = await app_client.get(
        "/api/v1/me/tenants", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert memberships.status_code == 200
    assert [item["tenant_code"] for item in memberships.json()["items"]] == ["EGT-ISO-C"]


async def test_start_resolves_only_matching_tenant_connection(app_client, db_session) -> None:
    tenant_a = await seed_admin_tenant(db_session, code="EGT-ISO-E", scopes=tuple(SSO_SCOPES))
    await _active_connection(app_client, tenant_a["headers"], email_domains=["only-a.example"])
    started = await app_client.post("/api/v1/auth/sso/start", json={"email_or_tenant": "user@only-a.example"})
    assert started.status_code == 200
    assert started.json()["tenant_code"] == "EGT-ISO-E"
    unknown = await app_client.post("/api/v1/auth/sso/start", json={"email_or_tenant": "user@tenant-b.example"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "SSO_PROVIDER_NOT_FOUND"
