"""0021_planning — FS-10 planning core: rules/fences/demand/supply/DRP/allocation (21 tables).

Creates the ``replenishment`` schema with the revision-0021 tables from
``app.modules.replenishment.tables``, monthly ``demand_history`` partitions (3 months ahead + current
year), updated-at triggers, tenant RLS/grants, settings/flag/job and number-sequence seeds. Planning is
a pure derived-read/write layer: no FK points at ledger/cost tables (DM-PLAN-01/INV-20).

Chain position: depends on FS-09 ``0020_manufacturing_execution``.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.replenishment import tables as t

revision = "0021_planning"
down_revision = "0020_manufacturing_execution"
branch_labels = None
depends_on = None

SCHEMA = "replenishment"
SETTINGS_NAMESPACE = "inventory"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

PLANNING_SETTINGS_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("replenishment.horizon_days", "90", "number"),
    ("replenishment.horizon.max_days", "365", "number"),
    ("replenishment.bucket_granularity", "WEEK", "string"),
    ("replenishment.time_fence_default_days", "0", "number"),
    ("replenishment.demand_change_threshold_percent", "20", "number"),
    ("replenishment.demand.method_default", "MOVING_AVERAGE", "string"),
    ("replenishment.demand.moving_average_window", "3", "number"),
    ("replenishment.demand.season_length", "12", "number"),
    ("replenishment.aim_formula_policy", '{"id":"aim-default-v1","version":1}', "json"),
    ("replenishment.atp_method", '{"id":"atp-earliest-v1","version":1}', "json"),
    ("replenishment.aim.analysis_interval_days", "30", "number"),
    ("replenishment.aim.seasonal_interval_days", "365", "number"),
    ("replenishment.aim.seasonal_mode", "WEEKDAY", "string"),
    ("replenishment.aim.order_interval_days", "7", "number"),
    ("replenishment.aim.default_lead_time_days", "7", "number"),
    ("replenishment.aim.change_epsilon", "0.000000001", "string"),
    ("replenishment.supply.default_lot_sizing", "LOT_FOR_LOT", "string"),
    ("replenishment.allocation.reserve_commitments", "false", "boolean"),
    ("replenishment.atp.horizon_days", "90", "number"),
    ("replenishment.atp.consistency_lag_seconds", "300", "number"),
    ("replenishment.planning.readonly_guard", "ENFORCE", "string"),
)

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("planning.demand.calculate", "planning", "planning.demand.calculate", "0 5 * * *", 1800),
    ("planning.supply.run", "planning", "planning.supply.run", "0 6 * * *", 3600),
    ("planning.allocation.run", "heavy", "planning.allocation.run", None, 1800),
    ("replenishment.aim", "planning", "replenishment.aim", "0 4 * * 1", 3600),
    ("replenishment.ordering", "planning", "replenishment.ordering", "30 4 * * 1", 900),
    ("atp.rebuild", "planning", "atp.rebuild", "*/30 * * * *", 900),
)

SEQUENCE_SEEDS: tuple[tuple[str, str, int], ...] = (("DP", "DP", 6), ("SP", "SP", 6), ("ALC", "ALC", 6), ("AIM", "AIM", 6))

PARTITION_YEARS: tuple[int, ...] = (2026, 2027)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]]) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _partitions() -> None:
    for year in PARTITION_YEARS:
        for month in range(1, 13):
            end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
            op.execute(
                f'CREATE TABLE IF NOT EXISTS {SCHEMA}."demand_history_{year:04d}{month:02d}" '
                f"PARTITION OF {SCHEMA}.\"demand_history\" "
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


def _triggers() -> None:
    for table in t.REVISION_0021_TABLES:
        if table.name == "demand_history":
            continue
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.demand_history_touch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_demand_history_touch BEFORE UPDATE ON {SCHEMA}."demand_history" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.demand_history_touch()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0021_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0021_TABLES)
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
        VALUES (gen_random_uuid(), :tenant_id, :namespace, :key, CAST(:value AS jsonb), :value_type,
                true, now(), now(), 1)
        ON CONFLICT (tenant_id, namespace, key) DO NOTHING
        """
    )
    for tenant_row in _tenants():
        for key, value, value_type in PLANNING_SETTINGS_SEEDS:
            json_value = value if value_type == "json" else (
                value if value_type in {"number", "boolean"} else json.dumps(value)
            )
            bind.execute(
                insert_setting,
                {"tenant_id": tenant_row, "namespace": SETTINGS_NAMESPACE, "key": key, "value": json_value, "value_type": value_type},
            )
        op.execute(
            f"""
            INSERT INTO platform.feature_flag
                (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
            VALUES (gen_random_uuid(), '{tenant_row}'::uuid, 'replenishment.planning.enabled',
                    false, 'MIGRATION', now(), now(), 1)
            ON CONFLICT (tenant_id, flag_key) DO NOTHING
            """
        )
        for doc_type, prefix, padding in SEQUENCE_SEEDS:
            seq_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.number_sequence/{tenant_row}/{doc_type}"))
            op.execute(
                f"""
                INSERT INTO platform.number_sequence
                    (id, tenant_id, doc_type, scope_key, prefix, padding, next_value, reset_policy, gap_policy,
                     created_at, updated_at, version)
                SELECT '{seq_id}', '{tenant_row}'::uuid, '{doc_type}', 'TENANT', '{prefix}', {padding}, 1,
                       'NEVER', 'NON_GAPLESS', now(), now(), 1
                 WHERE NOT EXISTS (
                    SELECT 1 FROM platform.number_sequence n
                     WHERE n.tenant_id = '{tenant_row}'::uuid AND n.doc_type = '{doc_type}'
                       AND n.scope_key = 'TENANT' AND n.period_key IS NULL
                 )
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
            VALUES ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{handler}', {cron_sql},
                    {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _schema_functions()
    deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0021_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table_name}" ADD CONSTRAINT "{fk_name}" '
            f'FOREIGN KEY ({", ".join(local_columns)}) REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )
    _partitions()
    _triggers()
    _rls_and_grants()
    _seeds()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
