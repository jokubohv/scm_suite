"""Feature flags admin API (live DB): tenant-scoped upsert, idempotency, audit + outbox event."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.models import platform as p
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID

PREFIX = "/api/v1/admin/flags"
FLAG = "inventory.ledger.enabled"


async def _count_rows(db_session: Any, table: Any, where: Any) -> int:
    result = await db_session.execute(select(func.count()).select_from(table).where(where))
    return int(result.scalar_one())


async def test_list_flags_starts_empty(app_client: Any) -> None:
    response = await app_client.get(PREFIX)
    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


async def test_patch_flag_creates_transactional_audit_and_event(app_client: Any, db_session: Any) -> None:
    patched = await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": True, "notes": "enable ledger"})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["flag_key"] == FLAG
    assert body["is_enabled"] is True
    assert body["source"] == "OVERRIDE"
    assert body["notes"] == "enable ledger"
    assert body["changed_by"]

    fetched = await app_client.get(f"{PREFIX}/{FLAG}")
    assert fetched.status_code == 200
    assert fetched.json()["is_enabled"] is True

    listed = await app_client.get(PREFIX)
    assert {item["flag_key"] for item in listed.json()["items"]} == {FLAG}
    filtered = await app_client.get(PREFIX, params={"flag_key": FLAG})
    assert filtered.json()["count"] == 1

    audit = await db_session.execute(
        select(p.audit_event.c.action, p.audit_event.c.resource_type, p.audit_event.c.diff).where(
            p.audit_event.c.tenant_id == TENANT_ID,
            p.audit_event.c.resource_type == "feature_flag",
        )
    )
    audit_rows = audit.mappings().all()
    assert len(audit_rows) == 1
    assert audit_rows[0]["action"] == "platform.flag.changed"
    assert audit_rows[0]["diff"]["after"]["flag_key"] == FLAG

    outbox = await db_session.execute(
        select(p.outbox_event.c.event_type, p.outbox_event.c.aggregate_type, p.outbox_event.c.payload).where(
            p.outbox_event.c.tenant_id == TENANT_ID
        )
    )
    outbox_rows = outbox.mappings().all()
    assert len(outbox_rows) == 1
    assert outbox_rows[0]["event_type"] == "platform.flag.changed"
    assert outbox_rows[0]["aggregate_type"] == "feature_flag"
    assert outbox_rows[0]["payload"]["flag_key"] == FLAG
    assert outbox_rows[0]["payload"]["is_enabled"] is True


async def test_patch_flag_is_idempotent_when_unchanged(app_client: Any, db_session: Any) -> None:
    first = await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": True, "notes": "on"})
    assert first.status_code == 200
    version = first.json()["version"]
    changed_at = first.json()["changed_at"]

    second = await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": True, "notes": "on"})
    assert second.status_code == 200
    assert second.json()["version"] == version
    assert second.json()["changed_at"] == changed_at

    audit_count = await _count_rows(
        db_session,
        p.audit_event,
        (p.audit_event.c.tenant_id == TENANT_ID) & (p.audit_event.c.resource_type == "feature_flag"),
    )
    event_count = await _count_rows(db_session, p.outbox_event, p.outbox_event.c.tenant_id == TENANT_ID)
    assert audit_count == 1
    assert event_count == 1

    changed = await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": False, "notes": "off"})
    assert changed.status_code == 200
    assert changed.json()["is_enabled"] is False
    assert changed.json()["version"] > version
    assert await _count_rows(db_session, p.outbox_event, p.outbox_event.c.tenant_id == TENANT_ID) == 2


async def test_flag_validation_errors(app_client: Any) -> None:
    bad_key = await app_client.patch(f"{PREFIX}/Not.A.Flag", json={"is_enabled": True})
    assert bad_key.status_code == 422
    assert bad_key.json()["code"] == "FLAG_KEY_INVALID"

    bad_body = await app_client.patch(f"{PREFIX}/{FLAG}", json={"notes": "missing is_enabled"})
    assert bad_body.status_code == 422

    bad_type = await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": "yes"})
    assert bad_type.status_code == 422
    assert bad_type.json()["code"] == "VALIDATION_ERROR"

    unknown = await app_client.get(f"{PREFIX}/inventory.unknown.enabled")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "FLAG_NOT_FOUND"


async def test_flags_are_tenant_scoped(app_client: Any, other_principal: Any, client_factory: Any) -> None:
    await app_client.patch(f"{PREFIX}/{FLAG}", json={"is_enabled": True})
    async with client_factory(other_principal) as other_client:
        other_list = await other_client.get(PREFIX)
        assert other_list.status_code == 200
        assert other_list.json()["items"] == []
    # The primary tenant's row is unchanged.
    primary = await app_client.get(PREFIX)
    assert primary.json()["items"][0]["is_enabled"] is True
