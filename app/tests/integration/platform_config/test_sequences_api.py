"""Number sequences admin API (live DB): read-only heads, filters, tenant scoping."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.core.models import platform as p
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID

PREFIX = "/api/v1/admin/sequences"


async def _insert_sequence(
    db_session: Any,
    *,
    tenant_id: Any,
    doc_type: str,
    prefix: str,
    next_value: int = 1,
    scope_key: str = "TENANT",
) -> None:
    await db_session.execute(
        p.number_sequence.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            doc_type=doc_type,
            scope_key=scope_key,
            prefix=prefix,
            padding=6,
            next_value=next_value,
            reset_policy="NEVER",
            gap_policy="NON_GAPLESS",
        )
    )


async def test_list_sequences(app_client: Any, db_session: Any) -> None:
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="PO", prefix="PO", next_value=42)
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="IR", prefix="IR")
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="PO", prefix="PO2", scope_key="LOC-1")
    await db_session.flush()

    response = await app_client.get(PREFIX)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert response.json()["count"] == 3
    assert [(item["doc_type"], item["scope_key"]) for item in items] == [
        ("IR", "TENANT"),
        ("PO", "LOC-1"),
        ("PO", "TENANT"),
    ]
    po = next(item for item in items if item["doc_type"] == "PO" and item["scope_key"] == "TENANT")
    assert po["prefix"] == "PO"
    assert po["next_value"] == 42
    assert po["gap_policy"] == "NON_GAPLESS"
    assert po["reset_policy"] == "NEVER"


async def test_filter_by_doc_type_and_limit(app_client: Any, db_session: Any) -> None:
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="PO", prefix="PO")
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="IR", prefix="IR")
    await db_session.flush()

    filtered = await app_client.get(PREFIX, params={"doc_type": "PO"})
    assert filtered.status_code == 200
    assert {item["doc_type"] for item in filtered.json()["items"]} == {"PO"}

    limited = await app_client.get(PREFIX, params={"limit": 1})
    assert limited.status_code == 200
    assert len(limited.json()["items"]) == 1

    bad_filter = await app_client.get(PREFIX, params={"doc_type": "po"})
    assert bad_filter.status_code == 422
    assert bad_filter.json()["code"] == "SEQUENCE_DOC_TYPE_INVALID"


async def test_sequences_are_tenant_scoped_and_read_only(app_client: Any, db_session: Any) -> None:
    await _insert_sequence(db_session, tenant_id=OTHER_TENANT_ID, doc_type="PO", prefix="OTHER")
    await _insert_sequence(db_session, tenant_id=TENANT_ID, doc_type="PO", prefix="MINE")
    await db_session.flush()

    response = await app_client.get(PREFIX)
    assert response.status_code == 200
    assert [item["prefix"] for item in response.json()["items"]] == ["MINE"]

    # This task exposes sequence heads read-only; mutation is owned by the posting/sequence service.
    for method in ("post", "patch", "delete", "put"):
        result = await getattr(app_client, method)(PREFIX)
        assert result.status_code == 405


async def test_sequences_require_catalogued_scope(client_factory: Any, principal: Any) -> None:
    from app.api.deps import Principal

    no_scope = Principal(
        identity_id=principal.identity_id,
        app_user_id=principal.app_user_id,
        tenant_id=principal.tenant_id,
        token_version=0,
        scopes=frozenset({"platform.reference.read"}),
    )
    async with client_factory(no_scope) as client:
        denied = await client.get(PREFIX)
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_SCOPE"
    assert denied.json()["details"][0]["required_scope"] == "platform.sequence.manage"
