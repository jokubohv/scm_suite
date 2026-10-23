"""Canonical API-key revoke alias + pure validators/route-scope contract (no DB for pure cases)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routers.platform import api_keys as api_keys_router
from app.api.routers.platform import extras as extras_router
from app.core.models import platform as p
from app.core.repositories.platform_extras import (
    ValidationError,
    normalise_email,
    parse_version,
    validate_approval_definition_patch,
    validate_data_scopes,
    validate_number_sequence_patch,
)
from app.core.security.scopes import load_catalogue
from app.tests.integration.platform_extras.conftest import (
    ensure_system_tenant,
    principal,
    seed_api_key,
    seed_tenant,
)

pytestmark = pytest.mark.integration


async def test_canonical_api_key_revoke_alias(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "AKEY")
    key_id = await seed_api_key(db_session, tenant_id)
    principal_holder.set(principal(tenant_id, scopes={"platform.admin"}))
    path = f"/api/v1/admin/api-keys/{key_id}/revoke"
    headers = {"Idempotency-Key": "revoke-1"}
    response = await extras_client.post(path, json={"reason": "compromised"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["key"]["revoked"] is True
    replay = await extras_client.post(path, json={"reason": "compromised"}, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    missing_key = await extras_client.post(path, json={"reason": "compromised"})
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    short_reason = await extras_client.post(path, json={"reason": "x"}, headers={"Idempotency-Key": "revoke-short"})
    assert short_reason.status_code == 422
    missing = await extras_client.post(
        f"/api/v1/admin/api-keys/{uuid4()}/revoke",
        json={"reason": "compromised"},
        headers={"Idempotency-Key": "revoke-missing"},
    )
    assert missing.status_code == 404
    principal_holder.set(principal(tenant_id, scopes={"platform.customfield.read"}))
    forbidden = await extras_client.post(
        path, json={"reason": "compromised"}, headers={"Idempotency-Key": "revoke-forbidden"}
    )
    assert forbidden.status_code == 403
    row = (await db_session.execute(select(p.api_key).where(p.api_key.c.id == key_id))).mappings().one()
    assert row["revoked_at"] is not None


def test_pure_validators() -> None:
    assert parse_version(None) is None
    assert parse_version("3") == 3
    assert parse_version('"7"') == 7
    assert parse_version('W/"2"') == 2
    with pytest.raises(ValidationError):
        parse_version("abc")
    with pytest.raises(ValidationError):
        normalise_email("not-an-email")
    assert normalise_email(" Admin@Example.COM ") == "admin@example.com"

    rows = validate_data_scopes(
        [
            {"scope_type": "location", "scope_value": "L1"},
            {"scope_type": "CLASS", "scope_value": "C1", "include_descendants": True},
        ]
    )
    assert rows[0]["scope_type"] == "LOCATION" and rows[1]["include_descendants"] is True
    with pytest.raises(ValidationError):
        validate_data_scopes([{"scope_type": "PLANET", "scope_value": "MARS"}])
    with pytest.raises(ValidationError):
        validate_data_scopes(
            [
                {"scope_type": "LOCATION", "scope_value": "L1"},
                {"scope_type": "LOCATION", "scope_value": "L1"},
            ]
        )
    with pytest.raises(ValidationError):
        validate_approval_definition_patch({"bogus": 1})
    with pytest.raises(ValidationError):
        validate_approval_definition_patch({"steps": "x"})
    with pytest.raises(ValidationError):
        validate_approval_definition_patch({})
    assert validate_approval_definition_patch({"priority": 3, "is_enabled": False}) == {
        "priority": 3,
        "is_enabled": False,
    }
    assert validate_number_sequence_patch({"prefix": "PO-", "padding": 4}, current_next_value=10) == {
        "prefix": "PO-",
        "padding": 4,
    }
    with pytest.raises(ValidationError):
        validate_number_sequence_patch({"next_value": 9}, current_next_value=10)
    assert validate_number_sequence_patch({"next_value": 11}, current_next_value=10) == {"next_value": 11}
    with pytest.raises(ValidationError):
        validate_number_sequence_patch({"reset_policy": "WEEKLY"}, current_next_value=10)
    with pytest.raises(ValidationError):
        validate_number_sequence_patch({}, current_next_value=10)


def _routes(module) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in module.router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        for method in methods:
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                found.add((method, path))
    return found


def test_route_scope_contract_matches_spec() -> None:
    catalogue = load_catalogue()
    catalogued = {scope.scope for scope in catalogue.scopes}
    assert set(extras_router.ROUTE_SCOPES) == _routes(extras_router)
    assert set(extras_router.ROUTE_SCOPES) == {
        ("POST", "/api/v1/platform/tenants/{tenant_id}/offboard"),
        ("POST", "/api/v1/platform/tenants/{tenant_id}/bootstrap-admin"),
        ("GET", "/api/v1/custom-field-values"),
        ("PUT", "/api/v1/admin/custom-field-values"),
        ("PUT", "/api/v1/admin/users/{user_id}/data-scopes"),
        ("PATCH", "/api/v1/admin/approval-definitions/{definition_id}"),
        ("PATCH", "/api/v1/admin/number-sequences/{sequence_id}"),
        ("GET", "/api/v1/audit-events"),
    }
    for scope in extras_router.ROUTE_SCOPES.values():
        assert scope in catalogued
    assert ("POST", "/api/v1/admin/api-keys/{key_id}/revoke") in api_keys_router.ROUTE_SCOPES
    assert set(api_keys_router.ROUTE_SCOPES) == _routes(api_keys_router)
    for scope in api_keys_router.ROUTE_SCOPES.values():
        assert scope in catalogued
