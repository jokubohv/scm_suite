"""0007_traceability_registry — FS-03 storage topology + inventory-status registry (DATA-MODEL §6).

Creates the ``traceability`` schema with the 4 tables from ``app.modules.traceability.tables``, indexes,
updated-at/cycle/bin-zone triggers, tenant RLS/grants, per-tenant bootstrap seeds (UNALLOCATED sentinel +
status catalogue incl. Δ-REUSE USED/REFURB), the two traceability job definitions, and closes the FS-01
``item_master`` deferred location/bin/status FKs (DMX-19 / DM-GLOBAL-06) after orphan checks.

Chain: 0006_pricing → 0007_traceability_registry. Additive; downgrade drops the traceability schema and
the six ``item_master`` FK constraints (nullable/default columns stay, matching the FS-01 pre-FS-03 state).
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.traceability import tables as t

revision = "0007_traceability_registry"
down_revision = "0006_pricing"
branch_labels = None
depends_on = None

SCHEMA = "traceability"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("traceability.bootstrap.sentinel", "default", "traceability.bootstrap.sentinel", "0 3 * * *", 900),
    ("traceability.deferred_fk.close", "default", "traceability.deferred_fk.close", "30 3 * * *", 900),
)

#: FS-01 deferred references closed by this migration (DMX-19): (table, column, target, nullable).
ITEM_MASTER_FKS: tuple[tuple[str, str, str, bool], ...] = (
    ("item_location", "location_id", "traceability.location", False),
    ("item_location", "default_bin_id", "traceability.bin", True),
    ("item_location", "default_status_id", "traceability.inventory_status", True),
    ("item_bin", "location_id", "traceability.location", False),
    ("item_bin", "bin_id", "traceability.bin", False),
    ("item_inventory", "default_status_id", "traceability.inventory_status", True),
)

#: (code, name, available_for_allocation, committable, sellable, consigned, default, system)
STATUS_SEEDS: tuple[tuple[str, str, bool, bool, bool, bool, bool, bool], ...] = (
    ("AVAILABLE", "Available", True, True, True, False, True, True),
    ("HOLD", "Hold", False, False, False, False, False, False),
    ("DAMAGED", "Damaged", False, False, False, False, False, False),
    ("RETURNED", "Returned", False, False, False, False, False, False),
    ("CONSIGNED_GOOD", "Consigned Good", True, True, True, True, False, True),
    # Δ-REUSE: refurbished/used stock is physically available but not sellable/committable by default.
    ("USED", "Used", True, False, False, False, False, False),
    ("REFURB", "Refurbished", True, False, False, False, False, False),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table) -> list[tuple[str, str, list[str], str, str, list[str]]]:
    """Create a traceability table without inline FKs; return deferred FK descriptors."""
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    deferred: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for fk in table.foreign_key_constraints:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        if len({parts[0] for parts in targets}) != 1 or len({parts[1] for parts in targets}) != 1:
            raise RuntimeError(f"composite FK {fk.name} mixes reference targets")
        ref_schema = targets[0][0]
        ref_table = targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))
    return deferred


def _functions() -> None:
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
        CREATE OR REPLACE FUNCTION {SCHEMA}.check_location_hierarchy() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            depth integer := 1;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.is_unallocated THEN
                IF NEW.code <> OLD.code
                   OR NEW.location_type <> OLD.location_type
                   OR NEW.is_unallocated <> OLD.is_unallocated
                   OR NEW.parent_id IS DISTINCT FROM OLD.parent_id
                   OR NEW.is_bins_enabled <> OLD.is_bins_enabled
                   OR NEW.is_inactive <> OLD.is_inactive THEN
                    RAISE EXCEPTION 'DM-TRACE-01: UNALLOCATED sentinel identity flags are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF NEW.parent_id IS NOT NULL THEN
                IF NEW.parent_id = NEW.id THEN
                    RAISE EXCEPTION 'DM-TRACE-01: location cannot be its own parent'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF EXISTS (
                    WITH RECURSIVE ancestors AS (
                        SELECT id, parent_id, 1 AS depth
                          FROM {SCHEMA}.location
                         WHERE tenant_id = NEW.tenant_id AND id = NEW.parent_id
                        UNION ALL
                        SELECT l.id, l.parent_id, a.depth + 1
                          FROM {SCHEMA}.location l
                          JOIN ancestors a ON l.id = a.parent_id AND l.tenant_id = NEW.tenant_id
                         WHERE a.depth < 64
                    )
                    SELECT 1 FROM ancestors WHERE id = NEW.id
                ) THEN
                    RAISE EXCEPTION 'DM-TRACE-01: location hierarchy cycle detected'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.check_bin_zone_location() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            zone_location uuid;
        BEGIN
            IF NEW.zone_id IS NOT NULL THEN
                SELECT location_id INTO zone_location
                  FROM {SCHEMA}.zone
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.zone_id;
                IF zone_location IS NULL THEN
                    RAISE EXCEPTION 'DM-TRACE-02: bin zone does not exist'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF zone_location <> NEW.location_id THEN
                    RAISE EXCEPTION 'DM-TRACE-02: bin zone belongs to a different location'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    for table in t.REVISION_0007_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f"CREATE TRIGGER trg_location_hierarchy_cycle BEFORE INSERT OR UPDATE ON {SCHEMA}.location "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.check_location_hierarchy()"
    )
    op.execute(
        f"CREATE TRIGGER trg_bin_zone_location BEFORE INSERT OR UPDATE ON {SCHEMA}.bin "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.check_bin_zone_location()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0007_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0007_TABLES)
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


def _seed_tenant_statuses() -> None:
    for code, name, available, committable, sellable, consigned, is_default, system in STATUS_SEEDS:
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.inventory_status
                (id, tenant_id, code, name, is_available_for_allocation, is_committable, is_sellable,
                 is_consigned, is_default, is_system, is_active, created_at, updated_at, version)
            SELECT gen_random_uuid(), ten.id, '{code}', '{name}', {str(available).lower()},
                   {str(committable).lower()}, {str(sellable).lower()}, {str(consigned).lower()},
                   {str(is_default).lower()}, {str(system).lower()}, true, now(), now(), 1
              FROM platform.tenant ten
             WHERE ten.id <> '{SYSTEM_TENANT_ID}'::uuid
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )


def _seed_sentinel_locations() -> None:
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.location
            (id, tenant_id, code, name, parent_id, location_type, is_bins_enabled, is_statuses_enabled,
             is_consignment_allowed, allow_commit_from_non_available, subsidiary_ref, timezone, address,
             is_unallocated, is_inactive, created_at, updated_at, version)
        SELECT gen_random_uuid(), ten.id, 'UNALLOCATED', 'Unallocated', NULL, 'UNALLOCATED', false, true,
               false, false, NULL, COALESCE(ten.timezone, 'UTC'), NULL, true, false, now(), now(), 1
          FROM platform.tenant ten
         WHERE ten.id <> '{SYSTEM_TENANT_ID}'::uuid
           AND NOT EXISTS (
               SELECT 1 FROM {SCHEMA}.location existing
                WHERE existing.tenant_id = ten.id AND existing.is_unallocated
           )
        """
    )


def _seed_jobs() -> None:
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


def _close_item_master_fks() -> None:
    for table, column, target, nullable in ITEM_MASTER_FKS:
        constraint = f"fk_{table}_{column}"
        nullable_sql = f"s.{column} IS NOT NULL" if not nullable else f"s.{column} IS NOT NULL"
        op.execute(
            f"""
            DO $$
            DECLARE orphan_count bigint;
            BEGIN
                SELECT count(*) INTO orphan_count
                  FROM item_master.{table} s
                 WHERE {nullable_sql}
                   AND NOT EXISTS (
                       SELECT 1 FROM {target} target
                        WHERE target.tenant_id = s.tenant_id AND target.id = s.{column}
                   );
                IF orphan_count > 0 THEN
                    RAISE EXCEPTION 'FS-01 deferred FK {table}.{column} has % orphan rows (DM-GLOBAL-06)', orphan_count;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{constraint}') THEN
                    ALTER TABLE item_master.{table}
                        ADD CONSTRAINT {constraint}
                        FOREIGN KEY (tenant_id, {column}) REFERENCES {target} (tenant_id, id);
                END IF;
            END $$;
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _functions()
    deferred: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0007_TABLES:
        deferred.extend(_create_table(table))
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred:
        op.create_foreign_key(
            fk_name,
            table_name,
            ref_table,
            local_columns,
            ref_columns,
            source_schema=SCHEMA,
            referent_schema=ref_schema,
        )
    _triggers()
    _rls_and_grants()
    _seed_tenant_statuses()
    _seed_sentinel_locations()
    _seed_jobs()
    _close_item_master_fks()


def downgrade() -> None:
    for table, column, _target, _nullable in ITEM_MASTER_FKS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_{table}_{column}') THEN
                    ALTER TABLE item_master.{table} DROP CONSTRAINT fk_{table}_{column};
                END IF;
            END $$;
            """
        )
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(
            f"DELETE FROM platform.job_definition WHERE name = '{name}' AND tenant_id = '{SYSTEM_TENANT_ID}'::uuid"
        )
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
