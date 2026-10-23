"""0008_ledger_core — FS-04 inventory ledger schema, append-only posting tables and owned documents.

Creates the ``inventory_ledger`` schema and the 19 core tables owned by revision 0008 (transactions,
lines, details, commitments/allocations, transfer/TO documents, in-transit, adjustments, worksheets,
distributions, reason codes, imports), append-only triggers on ledger tables, tenant RLS, grants,
reason-code/job seeds and best-effort FKs to FS-00/FS-01/FS-03.

Partitioning note: transaction/line/detail are created as regular tables in this development revision
(the production partition-maintenance job in 0010 adds monthly partitions; the column/PK shape is
partition-compatible). See FS-04 §14 for the resolved migration split.

Revision ID: 0008_ledger_core
Revises: 0007_traceability_registry
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.models.platform import tenant as _platform_tenant
from app.modules.inventory_ledger import tables as t

if "platform.tenant" not in t.metadata.tables:  # FK resolution for compiled DDL
    _platform_tenant.to_metadata(t.metadata)

revision = "0008_ledger_core"
down_revision = "0007_traceability_registry"
branch_labels = None
depends_on = None

SCHEMA = "inventory_ledger"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

REASON_CODES: tuple[tuple[str, str, str, str], ...] = (
    ("CYCLE_COUNT", "Cycle Count", "COUNT", "4100"),
    ("DAMAGE", "Damaged Goods", "ADJUSTMENT", "4200"),
    ("FOUND", "Found Stock", "ADJUSTMENT", "4300"),
    ("SHRINKAGE", "Shrinkage", "ADJUSTMENT", "4400"),
    ("SCRAP", "Scrap", "SCRAP", "4500"),
    ("RETURN_IN", "Customer Return", "RETURN", "4600"),
    ("TRANSFER_DEFAULT", "Default Transfer", "TRANSFER", "4700"),
    ("STATUS_CHANGE_DEFAULT", "Status Change", "STATUS_CHANGE", "4800"),
)

JOB_SEEDS: tuple[tuple[str, str, str, str, int], ...] = (
    ("ledger.import.process", "default", "inventory_ledger.import.process", None, 1800),
    ("ledger.commit_orders.schedule", "default", "inventory_ledger.commit_orders.schedule", "*/5 * * * *", 300),
    ("ledger.reconcile_balances", "default", "inventory_ledger.reconcile_balances", "15 2 * * *", 1800),
    ("ledger.partition.maintenance", "default", "inventory_ledger.partition.maintenance", "0 3 1 * *", 1800),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table) -> None:
    op.execute(str(CreateTable(table).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())


def _append_only() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END $$;
        """
    )
    for name in ("inventory_transaction", "inventory_transaction_line", "inventory_transaction_detail"):
        op.execute(f'DROP TRIGGER IF EXISTS trg_{name}_append_only ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE TRIGGER trg_{name}_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}."{name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
        )


def _updated_at_triggers() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
        """
    )
    for table in t.REVISION_0008_TABLES:
        if "updated_at" not in table.c:
            continue
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table.name}_touch ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0008_TABLES)
    for table in t.REVISION_0008_TABLES:
        name = table.name
        op.execute(f'ALTER TABLE {SCHEMA}."{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
        op.execute(f'DROP POLICY IF EXISTS system_tenant_read ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE POLICY system_tenant_read ON {SCHEMA}."{name}" FOR SELECT '
            f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)"
        )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {names} TO {APP_ROLE}';
                EXECUTE 'REVOKE UPDATE, DELETE ON {SCHEMA}.inventory_transaction, {SCHEMA}.inventory_transaction_line, {SCHEMA}.inventory_transaction_detail FROM {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _cross_context_fks() -> None:
    """Best-effort FKs to FS-01 (item/uom) and FS-03 (location/bin/status); skipped if absent."""
    statements = [
        ("fk_line_item", "inventory_transaction_line", "item_id", "item_master.item"),
        ("fk_line_location", "inventory_transaction_line", "location_id", "traceability.location"),
        ("fk_line_status", "inventory_transaction_line", "inventory_status_id", "traceability.inventory_status"),
        ("fk_line_uom", "inventory_transaction_line", "uom_id", "item_master.uom"),
        ("fk_adj_line_item", "inventory_adjustment_line", "item_id", "item_master.item"),
        ("fk_transfer_line_item", "inventory_transfer_line", "item_id", "item_master.item"),
        ("fk_to_line_item", "transfer_order_line", "item_id", "item_master.item"),
        ("fk_dist_line_item", "inventory_distribution_line", "item_id", "item_master.item"),
    ]
    for constraint, child, column, target in statements:
        schema, relation = target.split(".", 1)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('{schema}.{relation}') IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_constraint c
                       JOIN pg_class r ON r.oid = c.conrelid
                       JOIN pg_namespace n ON n.oid = r.relnamespace
                       WHERE c.conname = '{constraint}' AND n.nspname = '{SCHEMA}' AND r.relname = '{child}')
                THEN
                    EXECUTE 'ALTER TABLE {SCHEMA}.{child} ADD CONSTRAINT {constraint} '
                        || 'FOREIGN KEY (tenant_id, {column}) REFERENCES {schema}.{relation}(tenant_id, id)';
                END IF;
            EXCEPTION WHEN others THEN
                NULL;  -- referenced unique constraint not present yet (closing set migration owns it)
            END $$;
            """
        )


def _seed_reason_codes() -> None:
    for code, name, reason_type, account_ref in REASON_CODES:
        reason_id = uuid.uuid5(uuid.NAMESPACE_URL, f"inventory_ledger/reason/{reason_type}/{code}")
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.inventory_reason_code
                (id, tenant_id, code, name, reason_type, account_ref, is_system, is_active, created_at, updated_at, version)
            VALUES ('{reason_id}', '{SYSTEM_TENANT_ID}', '{code}', '{name}', '{reason_type}', '{account_ref}', true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, reason_type, code) DO NOTHING
            """
        )


def _seed_jobs() -> None:
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"inventory_ledger/jobs/{name}")
        cron_sql = "NULL" if cron is None else f"'{cron}'"
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts, singleton,
                 is_enabled, created_at, updated_at, version)
            VALUES ('{job_id}', '{SYSTEM_TENANT_ID}', '{name}', '{queue}', '{handler}', {cron_sql},
                    {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for table in t.REVISION_0008_TABLES:
        _create_table(table)
    _append_only()
    _updated_at_triggers()
    _cross_context_fks()
    _rls_and_grants()
    _seed_reason_codes()
    _seed_jobs()


def downgrade() -> None:
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(f"DELETE FROM platform.job_definition WHERE tenant_id = '{SYSTEM_TENANT_ID}' AND name = '{name}'")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
