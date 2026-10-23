"""FS-06 deferred lot/serial FK closure invariants (DM-GLOBAL-06, INV-18).

The FS-04/FS-05 tables created `lot_id`/`serial_id` nullable with no FK; migration 0013 must close every
existing column with a validated composite `(tenant_id, <col>)` FK to `traceability.lot` /
`traceability.serial_number`, and the runtime orphan probe (`deferred-fks/status`, nightly job) must
report zero orphans. Columns that do not exist in the frozen FS-04 schema are reported, not faked.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.modules.traceability_numbered.repository import NumberedRepository

pytestmark = pytest.mark.integration

#: (schema, table, column, target) candidates from DATA-MODEL §6/§7; only existing columns are asserted.
CANDIDATES: tuple[tuple[str, str, str, str], ...] = (
    ("inventory_ledger", "inventory_transaction_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_transaction_line", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_transaction_detail", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_transaction_detail", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_balance", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_balance", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_balance_bin", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_balance_bin", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_balance_lot", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_commitment_allocation", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_commitment_allocation", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_transfer_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_transfer_line", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "transfer_order_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "transfer_order_line", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_adjustment_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_adjustment_line", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_worksheet_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_worksheet_line", "serial_id", "traceability.serial_number"),
    ("inventory_ledger", "inventory_distribution_line", "lot_id", "traceability.lot"),
    ("inventory_ledger", "inventory_distribution_line", "serial_id", "traceability.serial_number"),
    ("costing", "item_cost", "lot_id", "traceability.lot"),
    ("costing", "item_cost", "serial_id", "traceability.serial_number"),
)


async def _existing_columns(db_session) -> set[tuple[str, str, str]]:
    rows = await db_session.execute(
        text(
            """
            SELECT table_schema::text AS table_schema, table_name::text AS table_name,
                   column_name::text AS column_name
              FROM information_schema.columns
             WHERE table_schema IN ('inventory_ledger', 'costing')
            """
        )
    )
    return {(row["table_schema"], row["table_name"], row["column_name"]) for row in rows.mappings()}


async def _constraint(db_session, name: str) -> dict | None:
    row = (
        (
            await db_session.execute(
                text(
                    """
                SELECT conname::text AS conname, convalidated AS convalidated,
                       pg_get_constraintdef(oid) AS definition
                  FROM pg_constraint WHERE conname = :name
                """
                ),
                {"name": name},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def test_every_existing_deferred_column_has_a_validated_fk(db_session) -> None:
    existing = await _existing_columns(db_session)
    problems: list[str] = []
    checked = 0
    for schema, table, column, target in CANDIDATES:
        if (schema, table, column) not in existing:
            continue
        checked += 1
        name = f"fk_{table}_{column}"
        constraint = await _constraint(db_session, name)
        if constraint is None:
            problems.append(f"{schema}.{table}.{column}: missing {name}")
            continue
        definition = constraint["definition"]
        if target not in definition:
            problems.append(f"{schema}.{table}.{column}: FK targets {definition!r}, expected {target}")
        if not constraint["convalidated"]:
            problems.append(f"{schema}.{table}.{column}: FK is NOT VALID")
    assert checked >= 20, f"expected at least 20 existing deferred columns, checked {checked}"
    assert problems == [], problems

    # FSI columns that the frozen FS-04 schema does not carry are reported explicitly (not silently faked).
    missing = sorted(
        f"{schema}.{table}.{column}"
        for schema, table, column, _ in CANDIDATES
        if (schema, table, column) not in existing
    )
    print(f"deferred columns absent from frozen FS-04/FS-05 schema (skipped): {missing}")


async def test_no_orphan_rows_on_deferred_columns(db_session) -> None:
    repository = NumberedRepository(db_session)
    counts = await repository.deferred_fk_counts()
    assert counts, "orphan probe must cover at least one deferred column"
    orphans = {key: value for key, value in counts.items() if value}
    assert orphans == {}, f"orphan lot/serial references found: {orphans}"


async def test_numbered_identity_tables_and_history_partitions_exist(db_session) -> None:
    tables = {
        row["relname"]
        for row in (
            await db_session.execute(
                text(
                    "SELECT c.relname::text AS relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'traceability'"
                )
            )
        ).mappings()
    }
    assert {"lot", "serial_number", "serial_number_history"} <= tables
    partitions = await db_session.scalar(
        text(
            "SELECT count(*) FROM pg_inherits i JOIN pg_class parent ON parent.oid = i.inhparent "
            "JOIN pg_namespace n ON n.oid = parent.relnamespace "
            "WHERE n.nspname = 'traceability' AND parent.relname = 'serial_number_history'"
        )
    )
    assert partitions >= 6
    orphan_probe = await db_session.scalar(
        text("SELECT count(*) FROM pg_constraint WHERE conname = 'fk_serial_number_history_tenant'")
    )
    assert orphan_probe >= 1  # partitioned parents clone the FK to each partition
