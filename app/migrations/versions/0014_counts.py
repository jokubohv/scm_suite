"""0014_counts — FS-07 inventory counts & cycle counts schema (6 tables).

Creates the ``counts`` schema with ``count_plan``, ``count_plan_filter``, ``inventory_count``,
``inventory_count_line`` (generated ``variance_qty``), ``inventory_count_snapshot`` (append-only
immutable snapshot revisions) and ``inventory_count_recount`` (append-only attempts); plus the
posted-count immutability trigger, updated-at triggers, tenant RLS/grants, RBAC-adjacent settings,
the ``inventory.counts.enabled`` deploy-dark flag (default off) and the three count job definitions.

Chain position: depends on FS-06 ``0013_numbered_inventory`` (serial/lot identity validation contract).
Additive; downgrade drops the six tables in reverse order.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.counts import tables as t

revision = "0014_counts"
down_revision = "0013_numbered_inventory"
branch_labels = None
depends_on = None

SCHEMA = "counts"
# FS-00 ``platform.tenant_setting`` namespace CHECK does not include "counts" (frozen FS-00 revision);
# the FS-07 settings are therefore stored under the existing "inventory" namespace with a ``counts.``
# key prefix. Recommended FS-00 amendment tracked in the FS-07 report (no FS-00 schema change here).
SETTINGS_NAMESPACE = "inventory"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

COUNT_SETTINGS_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("post_on_approve", "true", "boolean"),
    ("tolerance_qty", "0", "number"),
    ("tolerance_percent", "0", "number"),
    ("auto_approve_within_tolerance", "false", "boolean"),
    ("default_interval_days", "30", "number"),
    ("abc_class_intervals", '{"A":7,"B":30,"C":90}', "json"),
    ("draft_ttl_days", "30", "number"),
    ("require_lot_serial_prerequisite", "true", "boolean"),
)

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("counts.generate_cycle_counts", "default", "counts.generate_cycle_counts", "0 1 * * *", 1800),
    ("counts.snapshot.refresh", "default", "counts.snapshot.refresh", "*/5 * * * *", 300),
    ("counts.stale.cleanup", "default", "counts.stale.cleanup", "30 2 * * *", 900),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]]) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema = targets[0][0]
        ref_table = targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _schema_functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_posted_count_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'POSTED' THEN
                RAISE EXCEPTION 'posted inventory_count is immutable (DM-CNT-02/DM-GLOBAL-07)';
            END IF;
            IF OLD.posted_transaction_id IS NOT NULL AND NEW.posted_transaction_id IS DISTINCT FROM OLD.posted_transaction_id THEN
                RAISE EXCEPTION 'posted_transaction_id is immutable (DM-GLOBAL-07)';
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    for name in ("count_plan", "count_plan_filter", "inventory_count", "inventory_count_line"):
        op.execute(
            f'CREATE TRIGGER trg_{name}_touch BEFORE UPDATE ON {SCHEMA}."{name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    for name in ("inventory_count_snapshot", "inventory_count_recount"):
        op.execute(
            f'CREATE TRIGGER trg_{name}_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}."{name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
        )
    op.execute(
        f'CREATE TRIGGER trg_inventory_count_posted_immutable BEFORE UPDATE ON {SCHEMA}."inventory_count" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_posted_count_change()"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_count_line() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.counted_qty IS NOT NULL AND NEW.counted_qty < 0 THEN
                RAISE EXCEPTION 'counted_qty must be non-negative';
            END IF;
            IF NEW.serial_id IS NOT NULL AND NEW.counted_qty IS NOT NULL AND NEW.counted_qty > 1 THEN
                RAISE EXCEPTION 'serial count line counted_qty must be 0 or 1';
            END IF;
            IF NEW.variance_qty IS NOT NULL AND NEW.counted_qty IS NULL THEN
                RAISE EXCEPTION 'variance_qty requires counted_qty';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_count_line_guard BEFORE INSERT OR UPDATE ON {SCHEMA}."inventory_count_line" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_count_line()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0014_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0014_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {names} TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _tenants() -> tuple[str, ...]:
    rows = op.get_bind().execute(text("SELECT id::text FROM platform.tenant ORDER BY created_at")).all()
    return tuple(str(row[0]) for row in rows)


def _seeds() -> None:
    import json

    bind = op.get_bind()
    insert_setting = text(
        """
        INSERT INTO platform.tenant_setting
            (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
        VALUES
            (gen_random_uuid(), :tenant_id, :namespace, :key, CAST(:value AS jsonb), :value_type,
             true, now(), now(), 1)
        ON CONFLICT (tenant_id, namespace, key) DO NOTHING
        """
    )
    for tenant_row in _tenants():
        for key, value, value_type in COUNT_SETTINGS_SEEDS:
            if value_type == "json":
                json_value = value
            elif value_type == "boolean":
                json_value = "true" if value == "true" else "false"
            elif value_type == "number":
                json_value = str(value)
            else:
                json_value = json.dumps(value)
            bind.execute(
                insert_setting,
                {
                    "tenant_id": tenant_row,
                    "namespace": SETTINGS_NAMESPACE,
                    "key": f"counts.{key}",
                    "value": json_value,
                    "value_type": value_type,
                },
            )
        op.execute(
            f"""
            INSERT INTO platform.feature_flag
                (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
            VALUES
                (gen_random_uuid(), '{tenant_row}'::uuid, 'inventory.counts.enabled',
                 false, 'MIGRATION', now(), now(), 1)
            ON CONFLICT (tenant_id, flag_key) DO NOTHING
            """
        )
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        cron_sql = f"'{cron}'" if cron else "NULL"
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts,
                 singleton, is_enabled, created_at, updated_at, version)
            VALUES
                ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{handler}', {cron_sql},
                 {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _schema_functions()
    deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0014_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table_name}" ADD CONSTRAINT "{fk_name}" '
            f'FOREIGN KEY ({", ".join(local_columns)}) '
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )
    _triggers()
    _rls_and_grants()
    _seeds()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
