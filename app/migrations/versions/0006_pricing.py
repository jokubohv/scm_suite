"""0006_pricing — FS-02 pricing engine schema (DATA-MODEL §5, §21.1 M02).

Creates the ``pricing`` schema with the 17 tables from ``app.modules.pricing.tables``, indexes the
resolution/CRUD paths, installs updated-at + overlap triggers, the ``EXCLUDE USING gist`` range
constraint on ``price_update`` (DM-PRICE-03), tenant RLS/grants, the default price-level + six job
seeds, and closes the FS-01 ``item_master.item_sales`` deferred pricing FKs (§4.2, DM-GLOBAL-06).

Chain position: depends on FS-01 ``0004_item_master`` + ``0005_item_master_uom``. Additive;
downgrade drops the pricing schema and the three ``item_sales`` FK constraints only.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.pricing import tables as t

revision = "0006_pricing"
down_revision = "0005_item_master_uom"
branch_labels = None
depends_on = None

SCHEMA = "pricing"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"
ITEM_SALES_FK_COLUMNS = (
    ("price_level_id", f"{SCHEMA}.price_level"),
    ("quantity_pricing_schedule_id", f"{SCHEMA}.price_schedule"),
    ("pricing_group_id", f"{SCHEMA}.pricing_group"),
)
JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("pricing.apply_effective_updates", "default", "pricing.apply_effective_updates", "*/60 * * * * *", 900),
    ("pricing.recompute_dynamic_collections", "default", "pricing.recompute_dynamic_collections", "0 2 * * *", 3600),
    ("pricing.coupon.expire", "default", "pricing.coupon.expire", "30 2 * * *", 900),
    ("pricing.mass_price_update", "heavy", "pricing.mass_price_update", None, 1800),
    ("pricing.level_swap", "heavy", "pricing.level_swap", None, 1800),
    ("pricing.purchase_price_update", "heavy", "pricing.purchase_price_update", None, 1800),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]]) -> None:
    """Create the table without inline FKs (referenced schemas are not in this metadata).

    Foreign keys are added explicitly after every pricing table exists; that keeps the migration a
    pure DDL script and avoids resolving cross-schema references through SQLAlchemy metadata.
    """
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schemas = {parts[0] for parts in targets}
        if len(ref_schemas) != 1 or len({parts[1] for parts in targets}) != 1:
            raise RuntimeError(f"composite FK {fk.name} mixes reference targets")
        ref_schema = targets[0][0]
        ref_table = targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _indexes() -> None:
    statements = (
        'CREATE INDEX ix_price_level_active ON pricing."price_level" (tenant_id, is_inactive)',
        'CREATE INDEX ix_price_level_currency ON pricing."price_level" (tenant_id, currency_code)',
        'CREATE UNIQUE INDEX uq_price_level_default ON pricing."price_level" (tenant_id) '
        "WHERE is_default AND NOT is_inactive",
        'CREATE INDEX ix_item_group_active ON pricing."item_group" (tenant_id, is_inactive)',
        'CREATE INDEX ix_item_group_member_item ON pricing."item_group_member" (tenant_id, item_id)',
        'CREATE INDEX ix_customer_group_active ON pricing."customer_group" (tenant_id, is_inactive)',
        'CREATE INDEX ix_customer_group_member_ref ON pricing."customer_group_member" (tenant_id, external_customer_ref)',
        'CREATE INDEX ix_pricing_group_customer ON pricing."pricing_group" (tenant_id, customer_group_id)',
        'CREATE INDEX ix_item_price_lookup ON pricing."item_price" (tenant_id, item_id, price_level_id)',
        'CREATE INDEX ix_item_price_effective ON pricing."item_price" (tenant_id, effective_from, effective_to)',
        'CREATE INDEX ix_price_schedule_active ON pricing."price_schedule" (tenant_id, is_inactive)',
        'CREATE INDEX ix_price_schedule_break_schedule ON pricing."price_schedule_break" (tenant_id, price_schedule_id)',
        'CREATE INDEX ix_price_rule_active_priority ON pricing."price_rule" (tenant_id, is_active, priority)',
        'CREATE INDEX ix_price_rule_dates ON pricing."price_rule" (tenant_id, start_date, end_date)',
        'CREATE INDEX ix_price_rule_criteria_rule ON pricing."price_rule_criteria" (tenant_id, price_rule_id)',
        'CREATE INDEX ix_price_rule_criteria_value ON pricing."price_rule_criteria" (tenant_id, criterion, value_text)',
        'CREATE INDEX ix_item_collection_active ON pricing."item_collection" (tenant_id, is_inactive)',
        'CREATE INDEX ix_item_collection_member_item ON pricing."item_collection_member" (tenant_id, item_id)',
        'CREATE INDEX ix_coupon_window ON pricing."coupon" (tenant_id, is_active, start_date, end_date)',
        'CREATE INDEX ix_price_update_status ON pricing."price_update" (tenant_id, status, effective_start)',
        'CREATE INDEX ix_price_update_target ON pricing."price_update" (tenant_id, update_type, target_ref)',
        'CREATE INDEX ix_price_update_detail_update ON pricing."price_update_detail" (tenant_id, price_update_id)',
        'CREATE INDEX ix_price_update_detail_item ON pricing."price_update_detail" (tenant_id, item_id)',
        'CREATE UNIQUE INDEX uq_price_update_detail_target ON pricing."price_update_detail" '
        "(tenant_id, price_update_id, item_id, external_customer_ref, qty_min) NULLS NOT DISTINCT",
    )
    for sql in statements:
        op.execute(sql)


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
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_item_price_overlap() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.is_active AND EXISTS (
                SELECT 1 FROM {SCHEMA}.item_price p
                 WHERE p.tenant_id = NEW.tenant_id
                   AND p.item_id = NEW.item_id
                   AND p.price_level_id = NEW.price_level_id
                   AND p.currency_code = NEW.currency_code
                   AND p.is_active
                   AND p.id <> NEW.id
                   AND p.qty_min < COALESCE(NEW.qty_max, 'infinity'::numeric)
                   AND COALESCE(p.qty_max, 'infinity'::numeric) > NEW.qty_min
                   AND daterange(p.effective_from, p.effective_to, '[]')
                       && daterange(NEW.effective_from, NEW.effective_to, '[]')
            ) THEN
                RAISE EXCEPTION 'price band overlaps an existing active item_price row'
                    USING ERRCODE = 'unique_violation', DETAIL = 'RULE-PRICE-10';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_schedule_break_overlap() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.price_schedule_break b
                 WHERE b.tenant_id = NEW.tenant_id
                   AND b.price_schedule_id = NEW.price_schedule_id
                   AND b.id <> NEW.id
                   AND b.qty_from < COALESCE(NEW.qty_to, 'infinity'::numeric)
                   AND COALESCE(b.qty_to, 'infinity'::numeric) > NEW.qty_from
            ) THEN
                RAISE EXCEPTION 'price schedule breaks overlap'
                    USING ERRCODE = 'unique_violation', DETAIL = 'schedule breaks must not overlap';
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    for table in t.REVISION_0006_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f'CREATE TRIGGER trg_item_price_overlap BEFORE INSERT OR UPDATE ON {SCHEMA}."item_price" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_item_price_overlap()"
    )
    op.execute(
        f'CREATE TRIGGER trg_price_schedule_break_overlap BEFORE INSERT OR UPDATE ON {SCHEMA}."price_schedule_break" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_schedule_break_overlap()"
    )
    op.execute(
        f'CREATE INDEX ix_price_update_range ON {SCHEMA}."price_update" '
        "USING gist (tenant_id, update_type, target_ref, price_level_id, currency_code, "
        "daterange(effective_start, effective_end, '[]'))"
    )
    op.execute(
        f'ALTER TABLE {SCHEMA}."price_update" ADD CONSTRAINT ex_price_update_overlap '
        "EXCLUDE USING gist (tenant_id WITH =, update_type WITH =, target_ref WITH =, "
        "price_level_id WITH =, currency_code WITH =, "
        "daterange(effective_start, effective_end, '[]') WITH &&) "
        "WHERE (status IN ('PENDING','APPLIED'))"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0006_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0006_TABLES)
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


def _seeds() -> None:
    op.execute(
        f"""
        INSERT INTO {SCHEMA}.price_level
            (id, tenant_id, code, name, currency_code, is_default, is_inactive, sort_order,
             created_at, updated_at, version)
        SELECT gen_random_uuid(), ten.id, 'DEFAULT', 'Default',
               COALESCE(ten.base_currency_code, 'USD'), true, false, 0, now(), now(), 1
          FROM platform.tenant ten
         WHERE ten.id <> '{SYSTEM_TENANT_ID}'::uuid
           AND EXISTS (SELECT 1 FROM platform.currency c WHERE c.code = COALESCE(ten.base_currency_code, 'USD'))
        ON CONFLICT (tenant_id, code) DO NOTHING
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


def _close_item_sales_fks() -> None:
    for column, target in ITEM_SALES_FK_COLUMNS:
        constraint = f"fk_item_sales_{column}"
        op.execute(
            f"""
            DO $$
            DECLARE orphan_count bigint;
            BEGIN
                SELECT count(*) INTO orphan_count
                  FROM item_master.item_sales s
                 WHERE s.{column} IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM {target} x
                        WHERE x.tenant_id = s.tenant_id AND x.id = s.{column}
                   );
                IF orphan_count > 0 THEN
                    RAISE EXCEPTION 'FS-01 deferred FK {column} has % orphan rows (DM-GLOBAL-06)', orphan_count;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{constraint}') THEN
                    ALTER TABLE item_master.item_sales
                        ADD CONSTRAINT {constraint}
                        FOREIGN KEY (tenant_id, {column}) REFERENCES {target} (tenant_id, id);
                END IF;
            END $$;
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _schema_functions()
    deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0006_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.create_foreign_key(
            fk_name,
            table_name,
            ref_table,
            local_columns,
            ref_columns,
            source_schema=SCHEMA,
            referent_schema=ref_schema,
        )
    _indexes()
    _triggers()
    _rls_and_grants()
    _seeds()
    _close_item_sales_fks()


def downgrade() -> None:
    for column, _target in ITEM_SALES_FK_COLUMNS:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_item_sales_{column}') THEN
                    ALTER TABLE item_master.item_sales DROP CONSTRAINT fk_item_sales_{column};
                END IF;
            END $$;
            """
        )
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(
            f"DELETE FROM platform.job_definition WHERE name = '{name}' AND tenant_id = '{SYSTEM_TENANT_ID}'::uuid"
        )
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
