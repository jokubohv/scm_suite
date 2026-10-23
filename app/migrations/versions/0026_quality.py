"""0026_quality — FS-12 quality management (specs, inspections, COA) schema + seeds.

Creates the 10 ``quality`` tables with constraints/indexes, updated-at/append-style triggers (issued COA
immutability; completed inspection results immutable), RLS/grants, per-tenant defaults (queue, settings,
feature flag, INSP/COA number sequences) and the six quality job definitions.

Chain: ``0025_wms_execution`` → ``0026_quality`` → ``0027_consigned``. Additive only; downgrade drops the
quality schema (no other context's schema is touched).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.quality import tables as t

revision = "0026_quality"
down_revision = "0025_wms_execution"
branch_labels = None
depends_on = None

SCHEMA = "quality"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_DEFAULTS: tuple[tuple[str, object, str], ...] = (
    ("hold_status_code", "HOLD", "text"),
    ("default_queue_code", "DEFAULT", "text"),
    ("auto_coa", False, "boolean"),
    ("coa_sampling_eligible", ["FULL", "SAMPLE_RANDOM"], "json"),
    ("spec_line_limit", 35, "number"),
    ("derived_fields", [], "json"),
    ("notification_recipients", [], "json"),
    ("inspection_assignment", "MANUAL", "text"),
    ("hold_requires_approval", True, "boolean"),
    ("hold_release_status_code", "AVAILABLE", "text"),
)

SEQUENCES: tuple[tuple[str, str], ...] = (("INSP", "INSP-"), ("COA", "COA-"))

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("quality.trigger.evaluate", "default", "quality.trigger.evaluate", "*/2 * * * *", 600),
    ("quality.hold.reconcile", "default", "quality.hold.reconcile", "15 * * * *", 900),
    ("quality.queue.expire", "default", "quality.queue.expire", "30 3 * * *", 900),
    ("quality.trigger.counter_reset", "default", "quality.trigger.counter_reset", "0 4 * * 1", 900),
    ("quality.coa.auto_issue", "default", "quality.coa.auto_issue", "*/5 * * * *", 600),
    ("quality.derived_field.recalc", "default", "quality.derived_field.recalc", None, 900),
)


def _dialect():
    return postgresql.dialect()


def _create_tables() -> None:
    for table in t.REVISION_0026_TABLES:
        op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
        for index in table.indexes:
            op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())


def _add_fks() -> None:
    for table in t.REVISION_0026_TABLES:
        for fk in table.foreign_key_constraints:
            targets = [element.target_fullname.split(".") for element in fk.elements]
            op.create_foreign_key(
                fk.name,
                table.name,
                targets[0][1],
                [column.name for column in fk.columns],
                [parts[2] for parts in targets],
                source_schema=SCHEMA,
                referent_schema=targets[0][0],
            )


def _triggers() -> None:
    for table in t.REVISION_0026_TABLES:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table.name}_touch ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_coa_immutability() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.status <> 'DRAFT' THEN
                RAISE EXCEPTION 'DM-QA-03: issued COAs are immutable' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'ISSUED' AND
               (NEW.coa_number <> OLD.coa_number OR NEW.inspection_id <> OLD.inspection_id
                OR NEW.item_id <> OLD.item_id OR NEW.lot_id IS DISTINCT FROM OLD.lot_id) THEN
                RAISE EXCEPTION 'DM-QA-03: issued COA identity fields are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END $$;
        """
    )
    op.execute(f'DROP TRIGGER IF EXISTS trg_coa_immutability ON {SCHEMA}.coa')
    op.execute(
        f'CREATE TRIGGER trg_coa_immutability BEFORE UPDATE OR DELETE ON {SCHEMA}.coa '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_coa_immutability()"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_completed_results() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_status text;
        BEGIN
            SELECT status INTO parent_status FROM {SCHEMA}.inspection
             WHERE tenant_id = COALESCE(NEW.tenant_id, OLD.tenant_id)
               AND id = COALESCE(NEW.inspection_id, OLD.inspection_id);
            IF parent_status NOT IN ('PENDING','IN_PROGRESS') THEN
                RAISE EXCEPTION 'DM-QA-01: inspection results are immutable after completion'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END $$;
        """
    )
    op.execute(f'DROP TRIGGER IF EXISTS trg_inspection_result_immutable ON {SCHEMA}.inspection_result')
    op.execute(
        f'CREATE TRIGGER trg_inspection_result_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.inspection_result '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_completed_results()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0026_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0026_TABLES)
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


def _tenants(bind: sa.Connection) -> list[str]:
    rows = bind.execute(
        sa.text("SELECT id::text FROM platform.tenant WHERE id <> :system ORDER BY created_at"), {"system": SYSTEM_TENANT_ID}
    )
    return [row[0] for row in rows]


def _seeds(bind: sa.Connection) -> None:
    import json

    for tenant in _tenants(bind):
        for key, value, value_type in SETTING_DEFAULTS:
            raw = "null" if value is None else (
                json.dumps(value) if value_type in {"json"} else (str(value).lower() if isinstance(value, bool) else str(value))
            )
            op.execute(
                f"""
                INSERT INTO platform.tenant_setting
                    (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
                VALUES (gen_random_uuid(), '{tenant}'::uuid, 'quality', '{key}', '{raw}'::jsonb,
                        '{value_type}', true, now(), now(), 1)
                ON CONFLICT (tenant_id, namespace, key) DO NOTHING
                """
            )
        op.execute(
            f"""
            INSERT INTO platform.feature_flag
                (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
            VALUES (gen_random_uuid(), '{tenant}'::uuid, 'quality.management.enabled', false, 'MIGRATION',
                    now(), now(), 1)
            ON CONFLICT (tenant_id, flag_key) DO NOTHING
            """
        )
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.inspection_queue
                (id, tenant_id, code, name, location_id, trigger_id, assignment_strategy, is_active,
                 created_at, updated_at, version)
            VALUES (gen_random_uuid(), '{tenant}'::uuid, 'DEFAULT', 'Default inspection queue', NULL, NULL,
                    'MANUAL', true, now(), now(), 1)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )
        for doc_type, prefix in SEQUENCES:
            op.execute(
                f"""
                INSERT INTO platform.number_sequence
                    (id, tenant_id, doc_type, scope_key, prefix, padding, next_value, reset_policy, gap_policy,
                     period_key, created_at, updated_at, version)
                VALUES (gen_random_uuid(), '{tenant}'::uuid, '{doc_type}', 'TENANT', '{prefix}', 6, 1,
                        'NEVER', 'NON_GAPLESS', NULL, now(), now(), 1)
                ON CONFLICT (tenant_id, doc_type, scope_key, period_key) DO NOTHING
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
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            NEW.version = COALESCE(OLD.version, 0) + 1;
            RETURN NEW;
        END $$;
        """
    )
    _create_tables()
    _add_fks()
    _triggers()
    _rls_and_grants()
    _seeds(op.get_bind())


def downgrade() -> None:
    for table in reversed(t.REVISION_0026_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(
            f"DELETE FROM platform.job_definition WHERE name = '{name}' AND tenant_id = '{SYSTEM_TENANT_ID}'::uuid"
        )
    op.execute("DELETE FROM platform.tenant_setting WHERE namespace = 'quality' AND is_default")
    op.execute("DELETE FROM platform.feature_flag WHERE flag_key = 'quality.management.enabled' AND source = 'MIGRATION'")
    op.execute("DELETE FROM platform.number_sequence WHERE doc_type IN ('INSP','COA')")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_completed_results()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_coa_immutability()")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
