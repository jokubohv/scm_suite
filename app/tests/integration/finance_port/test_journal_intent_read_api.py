"""FS-00 finance-port journal-intent read API (D2): scope, tenant isolation, read-only contract.

D2 makes the GL a boundary: postings emit immutable ``finance_journal_intent`` rows (via
``platform.finance``) that downstream finance consumption reads, and Phase 1 exposes **no** mutation
endpoint for them. These tests exercise the mounted API end-to-end against a real (rollback-isolated)
database:

* list + detail require ``platform.finance.read`` and are tenant-scoped (cross-tenant = 404, no leak);
* filtering/cursor paging is deterministic;
* list responses omit the full ``intent_payload`` while detail exposes it;
* mutation verbs are not routed (405) — the table is append-only (trigger suite covers the DB layer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert

from app.core.models import platform as p
from app.tests.integration.finance_port.conftest import (
    JOURNAL,
    scoped_headers,
    seed_tenant,
)

pytestmark = pytest.mark.integration


async def _insert_intent(
    session: Any,
    tenant_id: UUID,
    *,
    line_no: int,
    recorded_at: datetime,
    status: str = "RECORDED",
    source_doc_type: str = "ITEM_RECEIPT",
    posting_rule_id: str = "RCPT",
    transaction_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> UUID:
    intent_id = uuid4()
    await session.execute(
        insert(p.finance_journal_intent).values(
            id=intent_id,
            tenant_id=tenant_id,
            intent_line_no=line_no,
            inventory_transaction_id=transaction_id or uuid4(),
            ledger_sequence=line_no,
            idempotency_key=f"posting-{intent_id.hex[:12]}",
            source_doc_type=source_doc_type,
            source_doc_id=uuid4(),
            posting_rule_id=posting_rule_id,
            account_ref="INV_ASSET",
            debit_amount_base=Decimal("100.000000000000"),
            credit_amount_base=Decimal("0"),
            amount_txn=Decimal("100"),
            currency_code="USD",
            exchange_rate=Decimal("1"),
            qty=Decimal("2.5"),
            segments={"class": "A"},
            intent_payload=payload or {"note": "intent"},
            status=status,
            recorded_at=recorded_at,
        )
    )
    return intent_id


async def test_finance_intent_list_and_detail_are_tenant_scoped(finance_client, db_session) -> None:
    tenant_a = await seed_tenant(db_session)
    tenant_b = await seed_tenant(db_session)
    tenant_a_id, tenant_b_id = UUID(tenant_a["tenant_id"]), UUID(tenant_b["tenant_id"])

    transaction_id = uuid4()
    first = await _insert_intent(
        db_session,
        tenant_a_id,
        line_no=1,
        recorded_at=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        transaction_id=transaction_id,
    )
    second = await _insert_intent(
        db_session,
        tenant_a_id,
        line_no=2,
        recorded_at=datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
        status="ACKNOWLEDGED",
        source_doc_type="VENDOR_BILL",
        posting_rule_id="BILL",
        payload={"note": "second"},
    )
    await _insert_intent(db_session, tenant_b_id, line_no=1, recorded_at=datetime(2026, 10, 23, 10, 0, tzinfo=UTC))

    listed = await finance_client.get(JOURNAL, headers=tenant_a["headers"])
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert {item["id"] for item in body["items"]} == {str(first), str(second)}
    assert body["count"] == 2
    assert "intent_payload" not in body["items"][0], "list view must not expose the full intent payload"

    item = next(row for row in body["items"] if row["id"] == str(first))
    assert item["debit_amount_base"] == "100.000000000000"
    assert item["qty"] == "2.500000000000"
    assert item["status"] == "RECORDED"
    assert item["recorded_at"].startswith("2026-10-21T10:00:00")

    by_status = await finance_client.get(JOURNAL, params={"status": "ACKNOWLEDGED"}, headers=tenant_a["headers"])
    assert [row["id"] for row in by_status.json()["items"]] == [str(second)]

    by_doc = await finance_client.get(JOURNAL, params={"source_doc_type": "ITEM_RECEIPT"}, headers=tenant_a["headers"])
    assert [row["id"] for row in by_doc.json()["items"]] == [str(first)]

    by_transaction = await finance_client.get(
        JOURNAL, params={"transaction_id": str(transaction_id)}, headers=tenant_a["headers"]
    )
    assert [row["id"] for row in by_transaction.json()["items"]] == [str(first)]

    by_date = await finance_client.get(
        JOURNAL, params={"from": "2026-10-22", "to": "2026-10-22"}, headers=tenant_a["headers"]
    )
    assert [row["id"] for row in by_date.json()["items"]] == [str(second)]

    detail = await finance_client.get(f"{JOURNAL}/{second}", headers=tenant_a["headers"])
    assert detail.status_code == 200
    assert detail.json()["id"] == str(second)
    assert detail.json()["intent_payload"] == {"note": "second"}

    # Cross-tenant detail is 404 (no existence leak); tenant B sees only its own row.
    cross = await finance_client.get(f"{JOURNAL}/{first}", headers=tenant_b["headers"])
    assert cross.status_code == 404
    assert cross.json()["code"] == "JOURNAL_INTENT_NOT_FOUND"
    b_listed = await finance_client.get(JOURNAL, headers=tenant_b["headers"])
    assert b_listed.json()["count"] == 1


async def test_finance_intent_requires_the_finance_read_scope(finance_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    await _insert_intent(
        db_session, UUID(tenant["tenant_id"]), line_no=1, recorded_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    )

    no_scope = scoped_headers(tenant, frozenset({"inventory.balance.read"}))
    denied = await finance_client.get(JOURNAL, headers=no_scope)
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/problem+json")
    assert denied.json()["code"] == "FORBIDDEN_SCOPE"

    anonymous = await finance_client.get(JOURNAL)
    assert anonymous.status_code == 401

    # The read scope only grants reads: no mutation verb is routed for journal intents (D2).
    for method in ("post", "put", "patch", "delete"):
        kwargs = {} if method == "delete" else {"json": {}}
        response = await getattr(finance_client, method)(JOURNAL, headers=tenant["headers"], **kwargs)
        assert response.status_code == 405, f"{method.upper()} must not be routed"

    allowed = await finance_client.get(JOURNAL, headers=tenant["headers"])
    assert allowed.status_code == 200


async def test_finance_intent_cursor_pagination_is_stable(finance_client, db_session) -> None:
    tenant = await seed_tenant(db_session)
    tenant_id = UUID(tenant["tenant_id"])
    for line_no in range(1, 4):
        await _insert_intent(
            db_session, tenant_id, line_no=line_no, recorded_at=datetime(2026, 10, 20 + line_no, 9, 0, tzinfo=UTC)
        )

    page_one = await finance_client.get(JOURNAL, params={"limit": 1}, headers=tenant["headers"])
    first_body = page_one.json()
    assert len(first_body["items"]) == 1 and first_body["next_cursor"]

    page_two = await finance_client.get(
        JOURNAL, params={"limit": 1, "cursor": first_body["next_cursor"]}, headers=tenant["headers"]
    )
    second_body = page_two.json()
    assert len(second_body["items"]) == 1
    assert second_body["items"][0]["id"] != first_body["items"][0]["id"]
    assert second_body["next_cursor"]

    bad_cursor = await finance_client.get(JOURNAL, params={"cursor": "not-a-cursor"}, headers=tenant["headers"])
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"


async def test_finance_intent_rows_are_append_only_at_db_level(db_session) -> None:
    """The API has no mutation route; the database additionally rejects UPDATE/DELETE on intents."""
    from datetime import UTC as _UTC

    from sqlalchemy.exc import DBAPIError

    tenant = await seed_tenant(db_session)
    intent_id = await _insert_intent(db_session, UUID(tenant["tenant_id"]), line_no=1, recorded_at=datetime.now(_UTC))
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            from sqlalchemy import delete

            await db_session.execute(delete(p.finance_journal_intent).where(p.finance_journal_intent.c.id == intent_id))
