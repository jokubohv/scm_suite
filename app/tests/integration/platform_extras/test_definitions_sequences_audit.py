"""Approval-definition and number-sequence patches (If-Match, guards) + canonical audit read alias."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.core.models import platform as p
from app.tests.integration.platform_extras.conftest import (
    TENANT_ADMIN_SCOPES,
    ensure_system_tenant,
    principal,
    seed_approval_definition,
    seed_number_sequence,
    seed_tenant,
)

pytestmark = pytest.mark.integration


async def test_patch_approval_definition(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "APPR")
    definition_id = await seed_approval_definition(db_session, tenant_id)
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))
    path = f"/api/v1/admin/approval-definitions/{definition_id}"

    patched = await extras_client.patch(path, json={"priority": 5, "is_enabled": False}, headers={"If-Match": "1"})
    assert patched.status_code == 200, patched.text
    definition = patched.json()["definition"]
    assert definition["priority"] == 5 and definition["is_enabled"] is False
    assert definition["version"] == 2

    stale = await extras_client.patch(path, json={"priority": 6}, headers={"If-Match": "1"})
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    invalid_steps = await extras_client.patch(path, json={"steps": "not-an-array"})
    assert invalid_steps.status_code == 422
    empty = await extras_client.patch(path, json={})
    assert empty.status_code == 422
    missing = await extras_client.patch(f"/api/v1/admin/approval-definitions/{uuid4()}", json={"priority": 1})
    assert missing.status_code == 404
    other = await seed_tenant(db_session, "APO2")
    principal_holder.set(principal(other, scopes=TENANT_ADMIN_SCOPES))
    cross = await extras_client.patch(path, json={"priority": 1})
    assert cross.status_code == 404


async def test_patch_number_sequence_never_decreases_next_value(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "SEQ")
    sequence_id = await seed_number_sequence(db_session, tenant_id, next_value=10)
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))
    path = f"/api/v1/admin/number-sequences/{sequence_id}"

    patched = await extras_client.patch(path, json={"prefix": "PO-", "padding": 5}, headers={"If-Match": "1"})
    assert patched.status_code == 200, patched.text
    sequence = patched.json()["sequence"]
    assert sequence["prefix"] == "PO-" and sequence["padding"] == 5 and sequence["next_value"] == 10
    assert sequence["version"] == 2

    decrease = await extras_client.patch(path, json={"next_value": 9})
    assert decrease.status_code == 422
    assert "decrease" in decrease.json()["message"].lower()
    increase = await extras_client.patch(path, json={"next_value": 12})
    assert increase.status_code == 200 and increase.json()["sequence"]["next_value"] == 12
    stale = await extras_client.patch(path, json={"padding": 4}, headers={"If-Match": "1"})
    assert stale.status_code == 409
    invalid_policy = await extras_client.patch(path, json={"reset_policy": "WEEKLY"})
    assert invalid_policy.status_code == 422
    missing = await extras_client.patch(f"/api/v1/admin/number-sequences/{uuid4()}", json={"padding": 3})
    assert missing.status_code == 404


async def test_canonical_audit_alias_filters_and_cursor(extras_client, db_session, principal_holder) -> None:
    await ensure_system_tenant(db_session)
    tenant_id = await seed_tenant(db_session, "AUDT")
    actor_id = uuid4()
    for index, action in enumerate(("tenant.suspended", "api_key.revoked", "user.data_scopes.replaced")):
        await db_session.execute(
            insert(p.audit_event).values(
                id=uuid4(),
                tenant_id=tenant_id,
                event_id=uuid4(),
                actor_type="USER",
                actor_id=actor_id,
                action=action,
                resource_type="app_user" if index == 2 else "tenant",
                resource_id=str(uuid4()),
                diff={"index": index},
                source="API",
            )
        )
    principal_holder.set(principal(tenant_id, scopes=TENANT_ADMIN_SCOPES))

    page = await extras_client.get("/api/v1/audit-events", params={"limit": 2})
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["items"]) == 2 and body["next_cursor"]
    next_page = await extras_client.get("/api/v1/audit-events", params={"limit": 2, "cursor": body["next_cursor"]})
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1

    filtered = await extras_client.get("/api/v1/audit-events", params={"resource_type": "app_user"})
    assert [item["action"] for item in filtered.json()["items"]] == ["user.data_scopes.replaced"]
    by_actor = await extras_client.get("/api/v1/audit-events", params={"actor_id": str(actor_id)})
    assert len(by_actor.json()["items"]) == 3
    bad_cursor = await extras_client.get("/api/v1/audit-events", params={"cursor": "!!!"})
    assert bad_cursor.status_code == 422

    other = await seed_tenant(db_session, "AUO2")
    principal_holder.set(principal(other, scopes=TENANT_ADMIN_SCOPES))
    isolated = await extras_client.get("/api/v1/audit-events")
    assert isolated.json()["items"] == []
    principal_holder.set(principal(other, scopes={"platform.customfield.read"}))
    not_allowed = await extras_client.get("/api/v1/audit-events")
    assert not_allowed.status_code == 403
