"""Append-only privilege invariants for the FS-00 audit/outbox/finance-intent tables (INV-09).

The platform ledger-style tables are immutable: the database must reject ``UPDATE``/``DELETE`` through
the reviewed ``platform.reject_mutation()`` trigger on every append-only table (partitioned parents
included) and, when the runtime roles are bootstrapped, ``platform_app``/``finance_adapter`` must not
hold ``UPDATE``/``DELETE`` grants on them.

Graceful skip: the DB-backed tests request the shared ``db_session`` fixture (skips when no database is
configured); the grant test skips when the environment's DB roles have not been bootstrapped yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import DBAPIError

from app.core.models import platform as p

pytestmark = pytest.mark.integration

SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

#: Tables whose rows may never be mutated (DATA-MODEL §1.5 / APPEND_ONLY_TABLES).
APPEND_ONLY_TABLES = (
    "audit_event",
    "outbox_event",
    "finance_journal_intent",
    "approval_action",
    "document_link",
)

#: PostgreSQL trigger-type bitmask: 1=ROW, 2=BEFORE, 8=DELETE, 16=UPDATE.
_TRIGGER_EXPECTED_BITS = 1 | 2 | 8 | 16


async def _trigger_rows(session):
    result = await session.execute(
        text(
            """
            SELECT c.relname::text AS table_name,
                   t.tgname::text AS trigger_name,
                   t.tgtype::int AS tgtype,
                   t.tgenabled::text AS enabled
              FROM pg_trigger t
              JOIN pg_class c ON c.oid = t.tgrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'platform'
               AND NOT t.tgisinternal
             ORDER BY 1, 2
            """
        )
    )
    rows: dict[str, list[dict]] = {}
    for row in result.mappings():
        rows.setdefault(row["table_name"], []).append(dict(row))
    return rows


async def _expect_rejected(session, statement) -> None:
    """Run one mutating statement in a savepoint and assert the append-only trigger raises."""
    with pytest.raises(DBAPIError, match="append-only"):
        async with session.begin_nested():
            await session.execute(statement)


async def test_reject_mutation_function_exists(db_session) -> None:
    result = await db_session.execute(
        text("SELECT proname::text AS name FROM pg_proc WHERE proname = 'reject_mutation'")
    )
    assert [row["name"] for row in result.mappings()] == ["reject_mutation"]


async def test_append_only_triggers_cover_update_and_delete(db_session) -> None:
    triggers = await _trigger_rows(db_session)
    problems: list[str] = []
    for table in APPEND_ONLY_TABLES:
        expected_name = f"trg_{table}_append_only"
        matching = [row for row in triggers.get(table, []) if row["trigger_name"] == expected_name]
        if not matching:
            problems.append(f"{table}: missing trigger {expected_name}")
            continue
        for row in matching:
            if row["enabled"] != "O":
                problems.append(f"{table}: trigger {expected_name} is not enabled (tgenabled={row['enabled']})")
            if row["tgtype"] & _TRIGGER_EXPECTED_BITS != _TRIGGER_EXPECTED_BITS:
                problems.append(
                    f"{table}: trigger {expected_name} does not fire BEFORE ROW UPDATE OR DELETE "
                    f"(tgtype={row['tgtype']})"
                )
    assert problems == [], problems


async def test_audit_event_rejects_update_and_delete(db_session) -> None:
    audit_id = uuid4()
    occurred_at = datetime.now(UTC)
    await db_session.execute(
        insert(p.audit_event).values(
            id=audit_id,
            tenant_id=SYSTEM_TENANT_ID,
            event_id=uuid4(),
            actor_type="SYSTEM",
            action="TEST_APPEND_ONLY",
            resource_type="platform.test",
            source="SYSTEM",
            occurred_at=occurred_at,
        )
    )
    await _expect_rejected(
        db_session,
        update(p.audit_event)
        .where(p.audit_event.c.id == audit_id, p.audit_event.c.occurred_at == occurred_at)
        .values(action="MUTATED"),
    )
    await _expect_rejected(
        db_session,
        delete(p.audit_event).where(p.audit_event.c.id == audit_id, p.audit_event.c.occurred_at == occurred_at),
    )


async def test_outbox_event_rejects_update_and_delete(db_session) -> None:
    event_id, occurred_at = uuid4(), datetime.now(UTC)
    aggregate_id = uuid4()
    await db_session.execute(
        insert(p.outbox_event).values(
            id=event_id,
            tenant_id=SYSTEM_TENANT_ID,
            event_id=uuid4(),
            aggregate_type="platform.test",
            aggregate_id=aggregate_id,
            aggregate_version=1,
            event_type="platform.test.event",
            event_version="1.0",
            occurred_at=occurred_at,
        )
    )
    await _expect_rejected(
        db_session,
        update(p.outbox_event)
        .where(p.outbox_event.c.id == event_id, p.outbox_event.c.occurred_at == occurred_at)
        .values(published_at=datetime.now(UTC)),
    )
    await _expect_rejected(
        db_session,
        delete(p.outbox_event).where(p.outbox_event.c.id == event_id, p.outbox_event.c.occurred_at == occurred_at),
    )


async def test_finance_journal_intent_rejects_update_and_delete(db_session) -> None:
    intent_id = uuid4()
    await db_session.execute(
        insert(p.finance_journal_intent).values(
            id=intent_id,
            tenant_id=SYSTEM_TENANT_ID,
            intent_line_no=1,
            inventory_transaction_id=uuid4(),
            source_doc_type="ITEM_RECEIPT",
            source_doc_id=uuid4(),
            posting_rule_id="RCPT",
            account_ref="INV_ASSET",
            debit_amount_base=1,
            credit_amount_base=0,
            currency_code="USD",
            recorded_at=datetime.now(UTC),
        )
    )
    await _expect_rejected(
        db_session,
        update(p.finance_journal_intent).where(p.finance_journal_intent.c.id == intent_id).values(debit_amount_base=2),
    )
    await _expect_rejected(
        db_session,
        delete(p.finance_journal_intent).where(p.finance_journal_intent.c.id == intent_id),
    )


async def test_runtime_roles_have_no_update_delete_grants(db_session) -> None:
    """When the DB bootstrap created the runtime roles, append-only grants must be absent."""
    result = await db_session.execute(
        text(
            """
            SELECT rolname::text AS rolname
              FROM pg_roles
             WHERE rolname = ANY(:roles)
            """
        ),
        {"roles": ["platform_app", "platform_auth", "finance_adapter"]},
    )
    roles = {row["rolname"] for row in result.mappings()}
    if not roles:
        pytest.skip(
            "runtime DB roles (platform_app/platform_auth/finance_adapter) not bootstrapped in this "
            "environment; grant-level append-only enforcement is covered by migration review"
        )

    problems: list[str] = []
    for role in sorted(roles):
        for table in APPEND_ONLY_TABLES:
            update_allowed = await db_session.scalar(
                text("SELECT has_table_privilege(:role, :table, 'UPDATE')"),
                {"role": role, "table": f"platform.{table}"},
            )
            delete_allowed = await db_session.scalar(
                text("SELECT has_table_privilege(:role, :table, 'DELETE')"),
                {"role": role, "table": f"platform.{table}"},
            )
            if update_allowed:
                problems.append(f"{role}: UPDATE granted on append-only platform.{table}")
            if delete_allowed:
                problems.append(f"{role}: DELETE granted on append-only platform.{table}")
    assert problems == [], problems
