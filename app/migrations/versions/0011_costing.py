"""0011_costing — FS-05 costing core: cost objects, layers/consumption, ledger, categories/templates,
variances, System COGS adjustments, landed cost, group-average (18 tables).

Creates the ``costing`` schema with the revision-0011 tables from ``app.modules.costing.tables``,
monthly partitions for the two append-only tables (``cost_ledger_entry`` on ``effective_date``,
``cost_layer_consumption`` on ``created_at``), append-only/updated-at/consistency triggers, RLS +
grants, and reference/seed rows (cost categories, costing settings, feature flag, job definitions).

PostgreSQL requires the partition key in every unique constraint of a partitioned table; the DATA-MODEL
uniqueness keys on ``cost_ledger_entry`` / ``cost_layer_consumption`` therefore carry the partition
column as a trailing column (reported in the FS-05 implementation report as a documentation deviation).

Chain position: depends on FS-04 ``0010_ledger_balances`` (ledger tables + fiscal-period FK targets).
Additive; downgrade drops the costing schema (0012 objects are dropped by 0012's downgrade first).
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.costing import tables as t

revision = "0011_costing"
down_revision = "0010_ledger_balances"
branch_labels = None
depends_on = None

SCHEMA = "costing"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

CATEGORY_SEEDS: tuple[tuple[str, str, str, bool, int], ...] = (
    ("MATERIAL", "Material", "MATERIAL", True, 10),
    ("LABOR", "Labor", "LABOR", True, 20),
    ("MACHINE", "Machine", "MACHINE", True, 30),
    ("OVERHEAD", "Overhead", "OVERHEAD", True, 40),
    ("FREIGHT", "Freight", "FREIGHT", False, 50),
    ("DUTY", "Duty", "DUTY", False, 60),
    ("OTHER", "Other", "OTHER", False, 70),
)

COSTING_SETTINGS_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("block_when_recalc_pending", "true", "boolean"),
    ("allow_provisional", "false", "boolean"),
    ("allow_negative", "true", "boolean"),
    ("rounding_mode", "PER_MOVEMENT", "string"),
    ("returns.costing_policy", "ORIGINAL_WHEN_TRACEABLE", "string"),
    ("landed_cost.default_basis", "QUANTITY", "string"),
    ("group_average.include_in_transit", "false", "boolean"),
    ("estimated_landed_cost.enabled", "true", "boolean"),
)

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("costing.recalculate_item_cost", "costing", "costing.recalc.item_cost", None, 1800),
    ("costing.standards.revalue", "costing", "costing.standard.revalue", None, 1800),
    ("costing.landedcost.allocate", "costing", "costing.landedcost.allocate", None, 900),
    ("costing.cogs.true_up", "costing", "costing.cogs.true_up", None, 900),
    ("costing.reconcile", "costing", "costing.reconcile", "30 2 * * *", 1800),
    ("costing.rollup.run", "costing", "costing.rollup.run", None, 3600),
)

PARTITION_MONTHS: tuple[tuple[int, int], ...] = tuple(
    (year, month) for year in (2026, 2027) for month in range(1, 13)
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
        ref_schemas = {parts[0] for parts in targets}
        if len(ref_schemas) != 1:
            raise RuntimeError(f"composite FK {fk.name} mixes reference schemas")
        ref_schema = targets[0][0]
        ref_table = targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        if ref_schema == SCHEMA:
            # internal FK: table exists once the loop reaches the referenced table; defer all to the end
            deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))
        else:
            deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _partitions() -> None:
    for table in (t.cost_ledger_entry, t.cost_layer_consumption):
        for year, month in PARTITION_MONTHS:
            end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
            op.execute(
                f'CREATE TABLE IF NOT EXISTS {SCHEMA}."{table.name}_{year:04d}{month:02d}" '
                f"PARTITION OF {SCHEMA}.\"{table.name}\" "
                f"FOR VALUES FROM ('{year:04d}-{month:02d}-01') TO ('{end_year:04d}-{end_month:02d}-01')"
            )


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
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_cost_layer() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.qty_remaining < 0 OR NEW.value_remaining < 0 THEN
                RAISE EXCEPTION 'cost_layer remaining values cannot be negative (INV-15)';
            END IF;
            IF NEW.qty_remaining > NEW.qty_original THEN
                RAISE EXCEPTION 'cost_layer qty_remaining exceeds qty_original';
            END IF;
            IF (NEW.status = 'EXHAUSTED') <> (NEW.qty_remaining = 0) THEN
                RAISE EXCEPTION 'cost_layer status must be EXHAUSTED iff qty_remaining = 0';
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    mutable = tuple(
        table
        for table in t.REVISION_0011_TABLES
        if table.name not in {"cost_layer_consumption", "cost_ledger_entry"}
    )
    for table in mutable:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    for name in ("cost_layer_consumption", "cost_ledger_entry"):
        op.execute(
            f'CREATE TRIGGER trg_{name}_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}."{name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
        )
    op.execute(
        f'CREATE TRIGGER trg_cost_layer_guard BEFORE INSERT OR UPDATE ON {SCHEMA}."cost_layer" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_cost_layer()"
    )
    # item_cost status/qty/value consistency (DM-COST-10, DM-COST-01)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_item_cost() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.qty_on_hand = 0 AND NEW.value_base <> 0 THEN
                RAISE EXCEPTION 'item_cost value must be zero when qty_on_hand is zero (DM-COST-01)';
            END IF;
            IF NEW.status = 'PENDING_RECALC' AND NEW.cost_provisional THEN
                RAISE EXCEPTION 'item_cost cannot be provisional while pending recalculation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_item_cost_guard BEFORE INSERT OR UPDATE ON {SCHEMA}."item_cost" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_item_cost()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0011_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0011_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {names} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _seeds() -> None:
    for tenant_row in _tenants():
        for code, name, category_type, rollup, sort_order in CATEGORY_SEEDS:
            category_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"costing.category/{tenant_row}/{code}"))
            op.execute(
                f"""
                INSERT INTO {SCHEMA}.cost_category
                    (id, tenant_id, code, name, category_type, is_rollup_input, sort_order, is_active,
                     created_at, updated_at, version)
                VALUES
                    ('{category_id}', '{tenant_row}'::uuid, '{code}', '{name}', '{category_type}',
                     {str(rollup).lower()}, {sort_order}, true, now(), now(), 1)
                ON CONFLICT (tenant_id, code) DO NOTHING
                """
            )
        for key, value, value_type in COSTING_SETTINGS_SEEDS:
            if value_type == "boolean":
                json_value = f"'{value}'::jsonb"
            else:
                json_value = f'\'"{value}"\'::jsonb'
            op.execute(
                f"""
                INSERT INTO platform.tenant_setting
                    (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
                VALUES
                    (gen_random_uuid(), '{tenant_row}'::uuid, '{SCHEMA}', '{key}',
                     {json_value}, '{value_type}', true, now(), now(), 1)
                ON CONFLICT (tenant_id, namespace, key) DO NOTHING
                """
            )
        op.execute(
            f"""
            INSERT INTO platform.feature_flag
                (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
            VALUES
                (gen_random_uuid(), '{tenant_row}'::uuid, 'costing.engine.enabled',
                 true, 'MIGRATION', now(), now(), 1)
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


def _tenants() -> tuple[str, ...]:
    connection = op.get_bind()
    rows = connection.execute(text("SELECT id::text FROM platform.tenant ORDER BY created_at")).all()
    return tuple(str(row[0]) for row in rows)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _schema_functions()
    deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0011_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table_name}" ADD CONSTRAINT "{fk_name}" '
            f'FOREIGN KEY ({", ".join(local_columns)}) '
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )
    _partitions()
    _triggers()
    _rls_and_grants()
    _seeds()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
