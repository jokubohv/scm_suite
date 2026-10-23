"""Attachment metadata API: register/list/detail/delete, idempotency, tenancy, scopes, audit."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.models import platform as p
from app.tests.integration.platform_artifacts.conftest import (
    FINANCE_SCOPES,
    scoped_headers,
    seed_tenant,
)

pytestmark = pytest.mark.integration

ATTACHMENTS = "/api/v1/attachments"
ADMIN_ATTACHMENTS = "/api/v1/admin/attachments"


async def _register(client, headers: dict[str, str], entity_id: str, *, key: str, **overrides):
    payload = {
        "entity_type": "INVOICE",
        "entity_id": entity_id,
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
    }
    payload.update(overrides)
    return await client.post(ATTACHMENTS, json=payload, headers={**headers, "Idempotency-Key": key})


async def test_attachment_lifecycle_idempotency_and_audit(artifacts_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    entity_id = str(uuid4())

    created = await _register(artifacts_client, tenant["headers"], entity_id, key="att-key-1")
    assert created.status_code == 201, created.text
    body = created.json()
    attachment = body["attachment"]
    attachment_id = attachment["id"]
    assert attachment["entity_type"] == "INVOICE"
    assert attachment["entity_id"] == entity_id
    assert attachment["storage_key"] == attachment["storage_ref"]
    assert attachment["storage_key"].startswith(f"tenant/{tenant['tenant_id']}/")
    assert body["upload"]["provider"] == "fake-s3"
    assert body["upload"]["url"].startswith("https://fake-s3.example/")
    assert body["storage"] == {"provider": "fake-s3", "presigned_upload_supported": True}

    stored = await db_session.execute(
        select(p.attachment_metadata).where(p.attachment_metadata.c.id == UUID(attachment_id))
    )
    row = stored.mappings().first()
    assert row is not None
    assert row["tenant_id"] == UUID(tenant["tenant_id"])
    assert row["filename"] == "invoice.pdf"

    # Idempotent replay: same key + same request -> same resource, replay header, no duplicate row.
    replay = await _register(artifacts_client, tenant["headers"], entity_id, key="att-key-1")
    assert replay.status_code == 201
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json()["attachment"]["id"] == attachment_id
    count_rows = await db_session.scalar(
        select(func.count()).select_from(p.attachment_metadata).where(
            p.attachment_metadata.c.tenant_id == UUID(tenant["tenant_id"])
        )
    )
    assert count_rows == 1

    # Same key with a different body is an idempotency conflict (problem+json).
    conflict = await _register(
        artifacts_client, tenant["headers"], entity_id, key="att-key-1", filename="other.pdf"
    )
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    listed = await artifacts_client.get(
        ATTACHMENTS,
        params={"entity_type": "INVOICE", "entity_id": entity_id},
        headers=tenant["headers"],
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [attachment_id]
    assert listed.json()["count"] == 1

    detail = await artifacts_client.get(f"{ATTACHMENTS}/{attachment_id}", headers=tenant["headers"])
    assert detail.status_code == 200
    assert detail.json()["attachment"]["id"] == attachment_id
    assert detail.json()["download_url"].startswith("https://fake-s3.example/")

    # The task-brief admin alias is served by the same handler.
    alias = await _register(artifacts_client, tenant["headers"], entity_id, key="att-key-alias")
    assert alias.status_code == 201
    alias_listed = await artifacts_client.get(ADMIN_ATTACHMENTS, headers=tenant["headers"])
    assert alias_listed.status_code == 200
    assert alias_listed.json()["count"] == 2

    deleted = await artifacts_client.delete(f"{ATTACHMENTS}/{attachment_id}", headers=tenant["headers"])
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": attachment_id, "storage_key": attachment["storage_key"]}
    remaining = await db_session.scalar(
        select(func.count()).select_from(p.attachment_metadata).where(
            p.attachment_metadata.c.tenant_id == UUID(tenant["tenant_id"])
        )
    )
    assert remaining == 1  # only the alias-registered attachment remains

    audit_rows = await db_session.execute(
        select(p.audit_event).where(
            p.audit_event.c.tenant_id == UUID(tenant["tenant_id"]),
            p.audit_event.c.resource_type == "attachment",
            p.audit_event.c.action == "attachment.deleted",
        )
    )
    audit = list(audit_rows.mappings())
    assert len(audit) == 1
    assert audit[0]["resource_id"] == attachment_id

    missing = await artifacts_client.delete(f"{ATTACHMENTS}/{attachment_id}", headers=tenant["headers"])
    assert missing.status_code == 404
    assert missing.json()["code"] == "ATTACHMENT_NOT_FOUND"


async def test_attachment_tenant_isolation_and_scope_enforcement(artifacts_client, db_session) -> None:
    tenant_a = await seed_tenant(db_session)
    tenant_b = await seed_tenant(db_session)
    entity_id = str(uuid4())
    created = await _register(artifacts_client, tenant_a["headers"], entity_id, key="iso-key-1")
    attachment_id = created.json()["attachment"]["id"]

    # Tenant B cannot see A's rows (list or detail: 404, no existence leak).
    listed = await artifacts_client.get(ATTACHMENTS, headers=tenant_b["headers"])
    assert listed.status_code == 200 and listed.json()["items"] == []
    cross = await artifacts_client.get(f"{ATTACHMENTS}/{attachment_id}", headers=tenant_b["headers"])
    assert cross.status_code == 404
    assert cross.json()["code"] == "ATTACHMENT_NOT_FOUND"

    # Missing capability scope -> 403 problem+json, deny-by-default.
    no_scope_headers = scoped_headers(tenant_a, FINANCE_SCOPES)
    forbidden = await artifacts_client.get(ATTACHMENTS, headers=no_scope_headers)
    assert forbidden.status_code == 403
    assert forbidden.headers["content-type"].startswith("application/problem+json")
    assert forbidden.json()["code"] == "FORBIDDEN_SCOPE"


async def test_attachment_validation_errors_are_problem_json(artifacts_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    entity_id = str(uuid4())

    disallowed = await _register(
        artifacts_client, tenant["headers"], entity_id, key="bad-type", content_type="application/zip"
    )
    assert disallowed.status_code == 422
    assert disallowed.headers["content-type"].startswith("application/problem+json")
    assert disallowed.json()["code"] == "VALIDATION_ERROR"

    oversize = await _register(
        artifacts_client, tenant["headers"], entity_id, key="too-big",
        size_bytes=25 * 1024 * 1024 + 1,
    )
    assert oversize.status_code == 422
    assert oversize.json()["code"] == "VALIDATION_ERROR"

    path_filename = await _register(
        artifacts_client, tenant["headers"], entity_id, key="path-name", filename="../escape.pdf"
    )
    assert path_filename.status_code == 422

    # Idempotency-Key is mandatory on registration (contract x-idempotency: required).
    missing_key = await artifacts_client.post(
        ATTACHMENTS,
        json={
            "entity_type": "INVOICE",
            "entity_id": entity_id,
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
        },
        headers=tenant["headers"],
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "VALIDATION_ERROR"
