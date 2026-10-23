"""Document-link API: append-only creation (idempotent natural key), queries, guarded delete."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.models import platform as p
from app.tests.integration.platform_artifacts.conftest import seed_tenant

pytestmark = pytest.mark.integration

LINKS = "/api/v1/document-links"
ADMIN_LINKS = "/api/v1/admin/document-links"


def _link_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "link_type": "CONVERTED_FROM",
        "from_type": "PURCHASE_ORDER",
        "from_id": str(uuid4()),
        "to_type": "ITEM_RECEIPT",
        "to_id": str(uuid4()),
    }
    payload.update(overrides)
    return payload


async def test_document_link_create_is_idempotent_by_natural_key(artifacts_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    payload = _link_payload()

    created = await artifacts_client.post(LINKS, json=payload, headers=tenant["headers"])
    assert created.status_code == 201, created.text
    link = created.json()["link"]
    assert created.json()["created"] is True
    assert link["link_type"] == "CONVERTED_FROM"
    assert link["from_id"] == payload["from_id"]

    duplicate = await artifacts_client.post(LINKS, json=payload, headers=tenant["headers"])
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["link"]["id"] == link["id"]

    rows = await db_session.scalar(
        select(func.count()).select_from(p.document_link).where(
            p.document_link.c.tenant_id == UUID(tenant["tenant_id"])
        )
    )
    assert rows == 1

    audit_rows = await db_session.execute(
        select(p.audit_event).where(
            p.audit_event.c.tenant_id == UUID(tenant["tenant_id"]),
            p.audit_event.c.action == "document_link.created",
        )
    )
    assert len(list(audit_rows.mappings())) == 1


async def test_document_link_query_filters_and_isolation(artifacts_client, db_session) -> None:
    tenant_a = await seed_tenant(db_session)
    tenant_b = await seed_tenant(db_session)
    po_id = str(uuid4())
    payload = _link_payload(from_id=po_id, link_type="BILLS")
    created = await artifacts_client.post(LINKS, json=payload, headers=tenant_a["headers"])
    link_id = created.json()["link"]["id"]
    await artifacts_client.post(
        LINKS,
        json=_link_payload(from_type="VENDOR_RETURN", to_type="VENDOR_CREDIT"),
        headers=tenant_a["headers"],
    )

    filtered = await artifacts_client.get(
        LINKS, params={"from_id": po_id, "link_type": "BILLS"}, headers=tenant_a["headers"]
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [link_id]

    detail = await artifacts_client.get(f"{LINKS}/{link_id}", headers=tenant_a["headers"])
    assert detail.status_code == 200
    assert detail.json()["link"]["id"] == link_id

    # Tenant isolation: B sees nothing and A's id is indistinguishable from missing.
    other = await artifacts_client.get(LINKS, headers=tenant_b["headers"])
    assert other.status_code == 200 and other.json()["items"] == []
    cross = await artifacts_client.get(f"{LINKS}/{link_id}", headers=tenant_b["headers"])
    assert cross.status_code == 404 and cross.json()["code"] == "DOCUMENT_LINK_NOT_FOUND"

    # Task-brief admin aliases are served by the same handlers.
    alias = await artifacts_client.get(ADMIN_LINKS, headers=tenant_a["headers"])
    assert alias.status_code == 200
    assert alias.json()["count"] == 2


async def test_document_link_delete_is_guarded_append_only(artifacts_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    created = await artifacts_client.post(LINKS, json=_link_payload(), headers=tenant["headers"])
    link_id = created.json()["link"]["id"]

    for path in (f"{LINKS}/{link_id}", f"{ADMIN_LINKS}/{link_id}"):
        refused = await artifacts_client.delete(path, headers=tenant["headers"])
        assert refused.status_code == 409
        assert refused.headers["content-type"].startswith("application/problem+json")
        assert refused.json()["code"] == "DOCUMENT_LINK_APPEND_ONLY"

    still_there = await db_session.scalar(
        select(func.count()).select_from(p.document_link).where(
            p.document_link.c.tenant_id == UUID(tenant["tenant_id"])
        )
    )
    assert still_there == 1

    rejected = await db_session.execute(
        select(p.audit_event).where(
            p.audit_event.c.tenant_id == UUID(tenant["tenant_id"]),
            p.audit_event.c.action == "document_link.delete_rejected",
        )
    )
    assert len(list(rejected.mappings())) == 2

    missing = await artifacts_client.delete(f"{LINKS}/{uuid4()}", headers=tenant["headers"])
    assert missing.status_code == 404
    assert missing.json()["code"] == "DOCUMENT_LINK_NOT_FOUND"


async def test_document_link_validation(artifacts_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    self_link = await artifacts_client.post(
        LINKS,
        json=_link_payload(from_type="PURCHASE_ORDER", to_type="PURCHASE_ORDER", to_id="same"),
        headers=tenant["headers"],
    )
    # ``to_id`` is not a UUID -> 422 validation; a same-type link is rejected by the domain rule.
    assert self_link.status_code == 422

    bad_type = _link_payload(link_type="lower_case")
    rejected = await artifacts_client.post(LINKS, json=bad_type, headers=tenant["headers"])
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "VALIDATION_ERROR"

    from_id = str(uuid4())
    same_id = await artifacts_client.post(
        LINKS,
        json=_link_payload(from_type="PURCHASE_ORDER", from_id=from_id, to_type="PURCHASE_ORDER", to_id=from_id),
        headers=tenant["headers"],
    )
    assert same_id.status_code == 422
