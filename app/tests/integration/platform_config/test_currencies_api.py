"""Currencies admin API (live DB): CRUD subset + validation + RBAC scope denial."""

from __future__ import annotations

from typing import Any

from app.tests.integration.platform_config.conftest import CONFIG_SCOPES

PREFIX = "/api/v1/admin/currencies"


async def test_list_currencies_includes_seeded_global_reference(app_client: Any) -> None:
    response = await app_client.get(PREFIX)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["items"])
    codes = {item["code"] for item in body["items"]}
    assert {"USD", "EUR", "JPY"}.issubset(codes)
    usd = next(item for item in body["items"] if item["code"] == "USD")
    assert usd["tenant_scope"] == "GLOBAL"
    assert usd["is_active"] is True


async def test_create_get_patch_currency(app_client: Any) -> None:
    created = await app_client.post(PREFIX, json={"code": "zzz", "numeric_code": 999, "minor_units": 0})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == "ZZZ"
    assert body["numeric_code"] == 999
    assert body["minor_units"] == 0
    assert body["tenant_scope"] == "GLOBAL"
    version = body["version"]

    fetched = await app_client.get(f"{PREFIX}/ZZZ")
    assert fetched.status_code == 200
    assert fetched.json()["code"] == "ZZZ"

    patched = await app_client.patch(
        f"{PREFIX}/ZZZ", json={"is_active": False, "minor_units": 2}, headers={"If-Match": str(version)}
    )
    assert patched.status_code == 200, patched.text
    patched_body = patched.json()
    assert patched_body["is_active"] is False
    assert patched_body["minor_units"] == 2

    inactive = await app_client.get(PREFIX, params={"is_active": "false"})
    assert inactive.status_code == 200
    assert "ZZZ" in {item["code"] for item in inactive.json()["items"]}
    active = await app_client.get(PREFIX, params={"is_active": "true"})
    assert "ZZZ" not in {item["code"] for item in active.json()["items"]}


async def test_duplicate_currency_conflict(app_client: Any) -> None:
    assert (await app_client.post(PREFIX, json={"code": "XTS"})).status_code == 201
    duplicate = await app_client.post(PREFIX, json={"code": "XTS"})
    assert duplicate.status_code == 409
    problem = duplicate.json()
    assert problem["code"] == "CURRENCY_EXISTS"
    assert problem["status"] == 409
    assert problem["correlation_id"] is None or isinstance(problem["correlation_id"], str)


async def test_currency_validation_errors(app_client: Any) -> None:
    bad_code = await app_client.post(PREFIX, json={"code": "X1Y"})
    assert bad_code.status_code == 422
    assert bad_code.json()["code"] == "CURRENCY_CODE_INVALID"

    too_short = await app_client.post(PREFIX, json={"code": "US"})
    assert too_short.status_code == 422
    assert too_short.json()["code"] == "VALIDATION_ERROR"

    out_of_range = await app_client.post(PREFIX, json={"code": "XTS", "numeric_code": 1000})
    assert out_of_range.status_code == 422

    missing = await app_client.get(f"{PREFIX}/QQQ")
    assert missing.status_code == 404
    assert missing.json()["code"] == "CURRENCY_NOT_FOUND"

    code_change = await app_client.patch(f"{PREFIX}/USD", json={"code": "USX"})
    assert code_change.status_code == 422
    assert code_change.json()["code"] == "CURRENCY_CODE_IMMUTABLE"


async def test_currency_if_match_stale_version(app_client: Any) -> None:
    created = await app_client.post(PREFIX, json={"code": "ZZY"})
    assert created.status_code == 201
    stale = await app_client.patch(f"{PREFIX}/ZZY", json={"is_active": False}, headers={"If-Match": "99"})
    assert stale.status_code == 412
    assert stale.json()["code"] == "PRECONDITION_FAILED"


async def test_currency_requires_catalogued_scope(client_factory: Any, principal: Any) -> None:
    from app.api.deps import Principal

    no_scope = Principal(
        identity_id=principal.identity_id,
        app_user_id=principal.app_user_id,
        tenant_id=principal.tenant_id,
        token_version=0,
        scopes=frozenset(CONFIG_SCOPES - {"platform.reference.read", "platform.finance.sync"}),
    )
    async with client_factory(no_scope) as client:
        denied = await client.get(PREFIX)
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_SCOPE"
    assert denied.json()["details"][0]["required_scope"] == "platform.reference.read"
