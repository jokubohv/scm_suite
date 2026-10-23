"""Custom-field value read/upsert (app.core.custom_fields) and user data-scope replacement."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.models import platform as p
from app.tests.integration.platform_extras.conftest import (
    TENANT_ADMIN_SCOPES,
    ensure_system_tenant,
    principal,
    seed_custom_field_def,
    seed_membership,
    seed_tenant,
)

pytestmark = pytest.mark.integration


async def test_custom_field_values_roundtrip_validation_and_tenant_isolation(
    extras_client, db_session, principal_holder
) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "CUST")
    other_tenant = await seed_tenant(db_session, "OTHR")
    await seed_custom_field_def(
        db_session, tenant_id, entity_type="ITEM", field_code="color", validation={"regex": "^[A-Z]+$"}
    )
    await seed_custom_field_def(
        db_session,
        tenant_id,
        entity_type="ITEM",
        field_code="rack",
        data_type="NUMBER",
        validation={"min": 1, "max": 42},
    )
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))
    entity_id = str(uuid4())
    put_path = "/api/v1/admin/custom-field-values"
    body = {"entity_type": "ITEM", "entity_id": entity_id, "values": {"color": "RED", "rack": 42}}
    headers = {"Idempotency-Key": "cf-1"}

    put = await extras_client.put(put_path, json=body, headers=headers)
    assert put.status_code == 200, put.text
    stored = {item["field_code"]: item["value"] for item in put.json()["values"]}
    assert stored["color"] == "RED"
    assert str(stored["rack"]) in {"42", "42.000000000000"}

    get = await extras_client.get("/api/v1/custom-field-values", params={"entity_type": "ITEM", "entity_id": entity_id})
    assert get.status_code == 200, get.text
    assert get.json()["count"] == 2

    replay = await extras_client.put(put_path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == put.json()
    conflict = await extras_client.put(put_path, json={**body, "values": {"color": "BLUE"}}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    unknown_entity = await extras_client.put(
        put_path,
        json={"entity_type": "BOGUS", "entity_id": entity_id, "values": {"color": "RED"}},
        headers={"Idempotency-Key": "cf-bad-entity"},
    )
    assert unknown_entity.status_code == 422
    assert unknown_entity.json()["code"] == "CUSTOM_FIELD_ENTITY_TYPE_INVALID"
    unknown_field = await extras_client.put(
        put_path,
        json={"entity_type": "ITEM", "entity_id": entity_id, "values": {"ghost": "x"}},
        headers={"Idempotency-Key": "cf-bad-field"},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["code"] == "CUSTOM_FIELD_UNKNOWN"
    bad_regex = await extras_client.put(
        put_path,
        json={"entity_type": "ITEM", "entity_id": entity_id, "values": {"color": "red"}},
        headers={"Idempotency-Key": "cf-bad-regex"},
    )
    assert bad_regex.status_code == 422

    # Tenant isolation: another tenant cannot read the values written by tenant CUST.
    principal_holder.set(principal(other_tenant, scopes=TENANT_ADMIN_SCOPES))
    isolated = await extras_client.get(
        "/api/v1/custom-field-values", params={"entity_type": "ITEM", "entity_id": entity_id}
    )
    assert isolated.status_code == 200
    assert isolated.json()["items"] == []


async def test_replace_user_data_scopes(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "DSCO")
    membership = await seed_membership(db_session, tenant_id)
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))
    path = f"/api/v1/admin/users/{membership['user_id']}/data-scopes"
    headers = {"Idempotency-Key": "scopes-1"}
    body = {
        "scopes": [
            {"scope_type": "LOCATION", "scope_value": "LOC-1"},
            {"scope_type": "CLASS", "scope_value": "CLASS-A", "include_descendants": True},
        ]
    }
    response = await extras_client.put(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 2
    rows = (
        (await db_session.execute(select(p.user_data_scope).where(p.user_data_scope.c.tenant_id == tenant_id)))
        .mappings()
        .all()
    )
    assert len(rows) == 2

    # Replace semantics: a second call removes the old rows.
    replace = await extras_client.put(
        path,
        json={"scopes": [{"scope_type": "DEPARTMENT", "scope_value": "DEPT-1"}]},
        headers={"Idempotency-Key": "scopes-2"},
    )
    assert replace.status_code == 200
    rows = (
        (await db_session.execute(select(p.user_data_scope).where(p.user_data_scope.c.tenant_id == tenant_id)))
        .mappings()
        .all()
    )
    assert [row["scope_type"] for row in rows] == ["DEPARTMENT"]

    invalid = await extras_client.put(
        path,
        json={"scopes": [{"scope_type": "PLANET", "scope_value": "MARS"}]},
        headers={"Idempotency-Key": "scopes-bad"},
    )
    assert invalid.status_code == 422 and invalid.json()["code"] == "VALIDATION_ERROR"

    # Cross-tenant user id is a 404 (no existence leak).
    other_tenant = await seed_tenant(db_session, "DSO2")
    no_user = await extras_client.put(
        f"/api/v1/admin/users/{uuid4()}/data-scopes",
        json={"scopes": []},
        headers={"Idempotency-Key": "scopes-missing"},
    )
    assert no_user.status_code == 404
    principal_holder.set(principal(other_tenant, scopes=TENANT_ADMIN_SCOPES))
    cross = await extras_client.put(
        path,
        json={"scopes": []},
        headers={"Idempotency-Key": "scopes-cross"},
    )
    assert cross.status_code == 404
