"""0013_numbered_inventory — FS-06 lot/serial identity, projection/history, deferred-FK closure.

Creates the three ``traceability`` numbered-inventory tables (DATA-MODEL §6 part 2), monthly partitions
for the append-only ``serial_number_history``, the serial projection/history and numbered-detail guards
(``TN001``–``TN006`` SQLSTATEs), the deferred lot/serial FKs created nullable by FS-04/FS-05
(``DM-GLOBAL-06``: backfill → ``NOT VALID`` → ``VALIDATE``), RLS/grants, tenant setting/feature-flag
defaults and the two FS-06 job definitions.

Chain: 0012_costing_standard → 0013_numbered_inventory. Additive; downgrade drops the triggers, the
deferred FK constraints and the schema (FS-04/FS-05 columns stay nullable = N-1 compatible).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.traceability_numbered import tables as t

revision = "0013_numbered_inventory"
down_revision = "0012_costing_standard"
branch_labels = None
depends_on = None

SCHEMA = "traceability"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_DEFAULTS: tuple[tuple[str, object, str], ...] = (
    ("numbered.enabled", False, "BOOLEAN"),
    ("numbered.fefo_enabled", True, "BOOLEAN"),
    ("numbered.require_expiry_for_fefo", True, "BOOLEAN"),
    ("numbered.commit_allocates_serials", True, "BOOLEAN"),
    ("numbered.serial_auto_numbering", False, "BOOLEAN"),
    ("numbered.expiry_warning_days", [30, 60, 90], "JSON"),
    ("numbered.max_serials_per_posting", 1000, "NUMBER"),
)

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("traceability.deferred_fks.validate", "default", "traceability.deferred_fks.validate", "15 4 * * *", 900),
    ("traceability.expiry.scan", "default", "traceability.expiry.scan", "45 2 * * *", 900),
)

#: Deferred identity columns opened by FS-04/FS-05: (schema, table, column, target table).
DEFERRED_FKS: tuple[tuple[str, str, str, str], ...] = (
    ("inventory_ledger", "inventory_transaction_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_transaction_line", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_transaction_detail", "lot_id", "lot"),
    ("inventory_ledger", "inventory_transaction_detail", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_balance", "lot_id", "lot"),
    ("inventory_ledger", "inventory_balance", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_balance_bin", "lot_id", "lot"),
    ("inventory_ledger", "inventory_balance_bin", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_balance_lot", "lot_id", "lot"),
    ("inventory_ledger", "inventory_commitment_allocation", "lot_id", "lot"),
    ("inventory_ledger", "inventory_commitment_allocation", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_transfer_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_transfer_line", "serial_id", "serial_number"),
    ("inventory_ledger", "transfer_order_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_adjustment_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_adjustment_line", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_worksheet_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_worksheet_line", "serial_id", "serial_number"),
    ("inventory_ledger", "inventory_distribution_line", "lot_id", "lot"),
    ("inventory_ledger", "inventory_distribution_line", "serial_id", "serial_number"),
    ("costing", "item_cost", "lot_id", "lot"),
    ("costing", "item_cost", "serial_id", "serial_number"),
)

#: Item-id expression used to import legacy identities for tables without a direct item_id column.
ITEM_ID_JOIN: dict[tuple[str, str], str] = {
    ("inventory_ledger", "inventory_transaction_detail"): (
        "JOIN inventory_ledger.inventory_transaction_line l "
        "ON l.tenant_id = s.tenant_id AND l.id = s.inventory_transaction_line_id"
    ),
    ("inventory_ledger", "inventory_commitment_allocation"): (
        "JOIN inventory_ledger.inventory_commitment c ON c.tenant_id = s.tenant_id AND c.id = s.commitment_id"
    ),
}
ITEM_ID_EXPR: dict[tuple[str, str], str] = {
    ("inventory_ledger", "inventory_transaction_detail"): "l.item_id",
    ("inventory_ledger", "inventory_commitment_allocation"): "c.item_id",
}


def _dialect():
    return postgresql.dialect()


def _create_tables() -> None:
    for table in t.REVISION_0013_TABLES:
        if table.name in t.PARTITIONED_TABLES:
            op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
            _ensure_history_partitions()
        else:
            op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
        for index in table.indexes:
            op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())


def _ensure_history_partitions(months: int = 6) -> None:
    now = datetime.now(UTC).date().replace(day=1)
    total = now.year * 12 + (now.month - 1)
    for offset in range(months):
        start_total = total + offset
        start = datetime(start_total // 12, start_total % 12 + 1, 1).date()
        nxt_total = start_total + 1
        end = datetime(nxt_total // 12, nxt_total % 12 + 1, 1).date()
        partition = f"serial_number_history_{start.strftime('%Y%m')}"
        op.execute(
            f'CREATE TABLE IF NOT EXISTS {SCHEMA}."{partition}" '
            f'PARTITION OF {SCHEMA}."serial_number_history" '
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )


def _add_fks() -> None:
    # Resolve FK targets from the stored column specs (the metadata module intentionally does not import
    # other contexts' tables, so ``fk.elements[0].column`` is not resolvable at migration time).
    for table in t.REVISION_0013_TABLES:
        for fk in table.foreign_key_constraints:
            targets = [element.target_fullname.split(".") for element in fk.elements]
            referent_schema = targets[0][0]
            referent_table = targets[0][1]
            local_columns = [column.name for column in fk.columns]
            referent_columns = [parts[2] for parts in targets]
            op.create_foreign_key(
                fk.name,
                table.name,
                referent_table,
                local_columns,
                referent_columns,
                source_schema=SCHEMA,
                referent_schema=referent_schema,
            )


# --------------------------------------------------------------------------- triggers ----------
def _functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END $$;
        """
    )
    # Serial projection guard: only the posting/commitment triggers may update the current projection.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_serial_projection() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('traceability.projection_write', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'DM-TRACE-08: serial projection is derived and updated only by the posting engine'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    # Numbered posting detail guard + serial projection/history synchronisation.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.sync_serial_from_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            line RECORD;
            identity RECORD;
            new_state text;
            history_event text;
            qty numeric;
        BEGIN
            SELECT l.item_id, l.movement_type, l.movement_direction, l.location_id, l.inventory_status_id,
                   l.ownership, l.transaction_date, it.requires_numbering
              INTO line
              FROM inventory_ledger.inventory_transaction_line l
              JOIN inventory_ledger.inventory_transaction tx
                ON tx.tenant_id = l.tenant_id AND tx.id = l.inventory_transaction_id
              LEFT JOIN item_master.item i ON i.tenant_id = l.tenant_id AND i.id = l.item_id
              LEFT JOIN item_master.item_type_definition it ON it.tenant_id = i.tenant_id AND it.id = i.item_type_id
             WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.inventory_transaction_line_id;
            qty := NEW.qty_base;
            IF NEW.serial_id IS NOT NULL THEN
                IF abs(qty) <> 1 THEN
                    RAISE EXCEPTION 'TN001: serialized detail qty must be +/-1 (got %)', qty
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT * INTO identity FROM {SCHEMA}.serial_number
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id FOR UPDATE;
                IF identity IS NULL THEN
                    RAISE EXCEPTION 'TN003: serial identity is required for a numbered posting'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                SELECT CASE
                    WHEN line.movement_type IN ('FULFILLMENT','KIT_FULFILLMENT') THEN 'FULFILLED'
                    WHEN qty > 0 THEN 'AVAILABLE'
                    WHEN identity.state = 'UNKNOWN' THEN 'AVAILABLE'
                    ELSE identity.state
                END INTO new_state;
                history_event := CASE
                    WHEN qty > 0 AND identity.state = 'UNKNOWN' THEN 'RECEIVED'
                    WHEN qty > 0 THEN 'RETURNED'
                    WHEN line.movement_type IN ('FULFILLMENT','KIT_FULFILLMENT') THEN 'FULFILLED'
                    WHEN line.movement_type IN ('ADJUSTMENT','COUNT_VARIANCE') THEN 'ADJUSTED'
                    ELSE 'MOVED'
                END;
                PERFORM set_config('traceability.projection_write', 'on', true);
                UPDATE {SCHEMA}.serial_number
                   SET state = new_state,
                       current_location_id = line.location_id,
                       current_status_id = line.inventory_status_id,
                       ownership = line.ownership,
                       last_transaction_id = NEW.inventory_transaction_id,
                       updated_at = now(),
                       version = version + 1
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id;
                PERFORM set_config('traceability.projection_write', 'off', true);
                INSERT INTO {SCHEMA}.serial_number_history
                    (id, tenant_id, serial_id, inventory_transaction_id, inventory_transaction_line_id,
                     event, from_location_id, from_status_id, to_location_id, to_status_id, ownership, occurred_at)
                VALUES
                    (gen_random_uuid(), NEW.tenant_id, NEW.serial_id, NEW.inventory_transaction_id,
                     NEW.inventory_transaction_line_id, history_event, identity.current_location_id,
                     identity.current_status_id, line.location_id, line.inventory_status_id, line.ownership, clock_timestamp());
            ELSIF NEW.lot_id IS NOT NULL THEN
                IF qty < 0 AND EXISTS (
                    SELECT 1 FROM {SCHEMA}.lot l
                     WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.lot_id
                       AND l.expiry_date IS NOT NULL AND l.expiry_date < line.transaction_date
                ) AND COALESCE(
                    (SELECT (ts.value #>> '{{}}')::boolean FROM platform.tenant_setting ts
                      WHERE ts.tenant_id = NEW.tenant_id AND ts.namespace = 'inventory'
                        AND ts.key = 'numbered.require_expiry_for_fefo'), true) THEN
                    RAISE EXCEPTION 'TN004: expired lot cannot be issued (DM-TRACE-05)'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF line.requires_numbering THEN
                RAISE EXCEPTION 'TN003: lot/serial detail is required for a numbered item'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    # Commitment allocation: serial reserved at most once; projection becomes COMMITTED.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.sync_serial_from_commitment() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            identity RECORD;
        BEGIN
            IF NEW.serial_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF NEW.qty <> 1 THEN
                RAISE EXCEPTION 'TN001: a serialized commitment allocation must be exactly one unit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT * INTO identity FROM {SCHEMA}.serial_number
             WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id FOR UPDATE;
            IF identity IS NULL THEN
                RAISE EXCEPTION 'TN003: serial identity is required for a commitment allocation'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            PERFORM set_config('traceability.projection_write', 'on', true);
            UPDATE {SCHEMA}.serial_number
               SET state = 'COMMITTED', updated_at = now(), version = version + 1
             WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id;
            PERFORM set_config('traceability.projection_write', 'off', true);
            INSERT INTO {SCHEMA}.serial_number_history
                (id, tenant_id, serial_id, event, from_location_id, from_status_id, ownership, occurred_at)
            VALUES
                (gen_random_uuid(), NEW.tenant_id, NEW.serial_id, 'COMMITTED',
                 identity.current_location_id, identity.current_status_id, identity.ownership, clock_timestamp());
            RETURN NEW;
        END $$;
        """
    )
    # Allocation release -> serial AVAILABLE again (unless another active allocation exists).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.release_serial_from_commitment() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            active_count integer;
            identity RECORD;
        BEGIN
            IF NEW.serial_id IS NULL OR NEW.released_at IS NULL OR OLD.released_at IS NOT NULL THEN
                RETURN NEW;
            END IF;
            SELECT count(*) INTO active_count FROM inventory_ledger.inventory_commitment_allocation a
             WHERE a.tenant_id = NEW.tenant_id AND a.serial_id = NEW.serial_id
               AND a.released_at IS NULL AND a.allocation_seq <> NEW.allocation_seq;
            IF active_count = 0 THEN
                SELECT * INTO identity FROM {SCHEMA}.serial_number
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id FOR UPDATE;
                PERFORM set_config('traceability.projection_write', 'on', true);
                UPDATE {SCHEMA}.serial_number
                   SET state = 'AVAILABLE', updated_at = now(), version = version + 1
                 WHERE tenant_id = NEW.tenant_id AND id = NEW.serial_id AND state = 'COMMITTED';
                PERFORM set_config('traceability.projection_write', 'off', true);
                INSERT INTO {SCHEMA}.serial_number_history
                    (id, tenant_id, serial_id, event, from_location_id, from_status_id, ownership, occurred_at)
                VALUES
                    (gen_random_uuid(), NEW.tenant_id, NEW.serial_id, 'RELEASED',
                     identity.current_location_id, identity.current_status_id, identity.ownership, clock_timestamp());
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    for table_name in ("lot", "serial_number"):
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table_name}_touch ON {SCHEMA}."{table_name}"')
        op.execute(
            f'CREATE TRIGGER trg_{table_name}_touch BEFORE UPDATE ON {SCHEMA}."{table_name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f"CREATE TRIGGER trg_serial_number_guard BEFORE UPDATE ON {SCHEMA}.serial_number "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_serial_projection()"
    )
    op.execute(
        f"CREATE TRIGGER trg_serial_number_history_append_only BEFORE UPDATE OR DELETE "
        f"ON {SCHEMA}.serial_number_history FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_numbered_detail_sync AFTER INSERT ON inventory_ledger.inventory_transaction_detail "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.sync_serial_from_ledger()"
    )
    op.execute(
        "CREATE TRIGGER trg_numbered_allocation_sync AFTER INSERT ON inventory_ledger.inventory_commitment_allocation "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.sync_serial_from_commitment()"
    )
    op.execute(
        "CREATE TRIGGER trg_numbered_allocation_release AFTER UPDATE OF released_at "
        "ON inventory_ledger.inventory_commitment_allocation "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.release_serial_from_commitment()"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_alloc_active_serial "
        "ON inventory_ledger.inventory_commitment_allocation (tenant_id, serial_id) "
        "WHERE serial_id IS NOT NULL AND released_at IS NULL"
    )


# --------------------------------------------------------------------------- RLS/grants -------
def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0013_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0013_TABLES)
    history = f'{SCHEMA}."serial_number_history"'
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {names} TO {APP_ROLE}';
                EXECUTE 'REVOKE UPDATE, DELETE ON {history} FROM {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _tenants(bind: sa.Connection) -> list[str]:
    rows = bind.execute(
        sa.text("SELECT id::text FROM platform.tenant WHERE id <> :system ORDER BY id"),
        {"system": SYSTEM_TENANT_ID},
    )
    return [row[0] for row in rows]


def _seeds(bind: sa.Connection) -> None:
    import json

    tenant_ids = _tenants(bind)
    for tenant_id in tenant_ids:
        for key, value, value_type in SETTING_DEFAULTS:
            json_value = f"'{json.dumps(value)}'::jsonb"
            op.execute(
                f"""
                INSERT INTO platform.tenant_setting
                    (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
                VALUES
                    (gen_random_uuid(), '{tenant_id}'::uuid, 'inventory', '{key}',
                     {json_value}, '{value_type}', true, now(), now(), 1)
                ON CONFLICT (tenant_id, namespace, key) DO NOTHING
                """
            )
        op.execute(
            f"""
            INSERT INTO platform.feature_flag
                (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
            VALUES
                (gen_random_uuid(), '{tenant_id}'::uuid, 'inventory.numbered.enabled',
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


# --------------------------------------------------------------------------- deferred FKs -----
def _table_columns(bind: sa.Connection, schema: str, table: str) -> set[str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = :schema AND table_name = :table
            """
        ),
        {"schema": schema, "table": table},
    )
    return {row[0] for row in rows}


def _identity_table(target: str) -> str:
    return f'{SCHEMA}."{target}"'


def _import_legacy_identities(bind: sa.Connection) -> list[str]:
    notices: list[str] = []
    for schema, table, column, target in DEFERRED_FKS:
        columns = _table_columns(bind, schema, table)
        if column not in columns:
            continue
        key = (schema, table)
        join = ITEM_ID_JOIN.get(key, "")
        item_expr = ITEM_ID_EXPR.get(key, "s.item_id") if "item_id" in columns or key in ITEM_ID_EXPR else None
        if item_expr is None:
            continue
        number_expr = f"'LEGACY-' || s.{column}::text"
        if target == "lot":
            insert_sql = f"""
                INSERT INTO {SCHEMA}.lot
                    (id, tenant_id, item_id, lot_number, is_active, created_at, updated_at, version)
                SELECT gen_random_uuid(), src.tenant_id, src.item_id, 'LEGACY-' || src.{column}::text, true, now(), now(), 1
                  FROM (
                    SELECT DISTINCT ON (s.tenant_id, s.{column}) s.tenant_id, s.{column}, {item_expr} AS item_id
                      FROM {schema}.{table} s {join}
                     WHERE s.{column} IS NOT NULL
                     ORDER BY s.tenant_id, s.{column}
                  ) src
                 WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.lot existing
                     WHERE existing.tenant_id = src.tenant_id AND existing.id = src.{column}
                 )
                 ON CONFLICT (tenant_id, item_id, lot_number) DO NOTHING
            """
        else:
            insert_sql = f"""
                INSERT INTO {SCHEMA}.serial_number
                    (id, tenant_id, item_id, serial_number, state, ownership, created_at, updated_at, version)
                SELECT gen_random_uuid(), src.tenant_id, src.item_id, 'LEGACY-' || src.{column}::text,
                       'UNKNOWN', 'OWNED', now(), now(), 1
                  FROM (
                    SELECT DISTINCT ON (s.tenant_id, s.{column}) s.tenant_id, s.{column}, {item_expr} AS item_id
                      FROM {schema}.{table} s {join}
                     WHERE s.{column} IS NOT NULL
                     ORDER BY s.tenant_id, s.{column}
                  ) src
                 WHERE NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.serial_number existing
                     WHERE existing.tenant_id = src.tenant_id AND existing.id = src.{column}
                 )
                 ON CONFLICT (tenant_id, item_id, serial_number) DO NOTHING
            """
        try:
            with bind.begin_nested():
                bind.execute(sa.text(insert_sql))
        except Exception as exc:  # noqa: BLE001 - legacy import is best effort; report in notices
            notices.append(f"legacy identity import skipped for {schema}.{table}.{column}: {exc}")
        # Null out unresolvable legacy references so the validated FK can close (identity never resolved).
        nulled = bind.execute(
            sa.text(
                f"""
                UPDATE {schema}.{table} s
                   SET {column} = NULL
                 WHERE s.{column} IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM {_identity_table(target)} identity
                        WHERE identity.tenant_id = s.tenant_id AND identity.id = s.{column}
                   )
                """
            )
        )
        if getattr(nulled, "rowcount", 0):
            notices.append(f"nulled {nulled.rowcount} unresolvable legacy {schema}.{table}.{column} value(s)")
    return notices


def _close_deferred_fks(bind: sa.Connection) -> list[str]:
    notices = _import_legacy_identities(bind)
    for schema, table, column, target in DEFERRED_FKS:
        columns = _table_columns(bind, schema, table)
        if column not in columns:
            continue
        constraint = f"fk_{table}_{column}"
        exists = bind.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": constraint}
        ).first()
        if exists:
            continue
        op.execute(
            f'ALTER TABLE {schema}."{table}" ADD CONSTRAINT {constraint} '
            f'FOREIGN KEY (tenant_id, {column}) REFERENCES {SCHEMA}."{target}" (tenant_id, id) NOT VALID'
        )
        op.execute(f'ALTER TABLE {schema}."{table}" VALIDATE CONSTRAINT {constraint}')
    return notices


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _functions()
    _create_tables()
    _add_fks()
    _triggers()
    _rls_and_grants()
    bind = op.get_bind()
    _seeds(bind)
    notices = _close_deferred_fks(bind)
    for notice in notices:
        op.execute(f"DO $$ BEGIN RAISE NOTICE '{notice.replace(chr(39), chr(39) * 2)}'; END $$")


def downgrade() -> None:
    for trigger, table in (
        ("trg_numbered_allocation_release", "inventory_ledger.inventory_commitment_allocation"),
        ("trg_numbered_allocation_sync", "inventory_ledger.inventory_commitment_allocation"),
        ("trg_numbered_detail_sync", "inventory_ledger.inventory_transaction_detail"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP INDEX IF EXISTS inventory_ledger.uq_alloc_active_serial")
    for schema, table, column, _target in DEFERRED_FKS:
        op.execute(f'ALTER TABLE {schema}."{table}" DROP CONSTRAINT IF EXISTS fk_{table}_{column}')
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(
            f"DELETE FROM platform.job_definition WHERE name = '{name}' AND tenant_id = '{SYSTEM_TENANT_ID}'::uuid"
        )
    op.execute(
        "DELETE FROM platform.tenant_setting WHERE namespace = 'inventory' AND key LIKE 'numbered.%' AND is_default"
    )
    op.execute(
        "DELETE FROM platform.feature_flag WHERE flag_key = 'inventory.numbered.enabled' AND source = 'MIGRATION'"
    )
    # FS-06 owns only these three tables (the `traceability` schema is shared with FS-03): never drop the
    # schema here. Partitions are removed by CASCADE from the partitioned history parent.
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.serial_number_history CASCADE")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.serial_number CASCADE")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.lot CASCADE")
