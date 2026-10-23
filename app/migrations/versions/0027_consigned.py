"""0027_consigned — FS-13 consigned inventory: agreements/covered items, ownership-transfer documents/lines and
consumption audit trail (5 tables), RLS/triggers, consigned settings/flags/jobs/sequences.

Chain position: after FS-12 ``0026_quality``. Additive; downgrade drops the ``consigned`` schema.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.consigned import tables as t

revision = "0027_consigned"
down_revision = "0026_quality"
branch_labels = None
depends_on = None

SCHEMA = "consigned"
SETTINGS_NAMESPACE = "inventory"
SETTING_PREFIX = "consigned."
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("assembly_incompatible_guard", "true", "boolean"),
    ("default_consigned_status_code", "CONSIGNED_GOOD", "string"),
    ("default_owned_status_code", "AVAILABLE", "string"),
    ("payment_trigger_default", "ON_SALE", "string"),
    ("consumption_requires_external_ref", "true", "boolean"),
    ("auto_bill_on_consumption", "true", "boolean"),
    ("reconcile_strict", "true", "boolean"),
)

FLAG_SEEDS: tuple[tuple[str, bool], ...] = (
    ("consigned.inventory.enabled", False),
    ("consigned.assembly_incompatible_guard", True),
)

JOB_SEEDS: tuple[tuple[str, str, str | None, int], ...] = (
    ("consigned.consumption.reconcile", "default", "*/30 * * * *", 900),
    ("consigned.agreement.expiry", "default", "0 1 * * *", 1800),
    ("consigned.ownership.reconcile", "default", "0 2 * * *", 1800),
    ("consigned.stock.guard", "default", "0 3 * * *", 900),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred: list) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    deferred.extend(table.foreign_key_constraints)


def _add_fks(deferred: list) -> None:
    for fk in deferred:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        op.execute(
            f'ALTER TABLE {SCHEMA}."{fk.table.name}" ADD CONSTRAINT "{fk.name}" '
            f"FOREIGN KEY ({', '.join(local_columns)}) "
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )


def _functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_agreement_item() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE consigned_is_consigned boolean; owned_is_consigned boolean;
        BEGIN
            SELECT is_consigned INTO consigned_is_consigned FROM traceability.inventory_status
             WHERE tenant_id = NEW.tenant_id AND id = NEW.consigned_status_id;
            SELECT is_consigned INTO owned_is_consigned FROM traceability.inventory_status
             WHERE tenant_id = NEW.tenant_id AND id = NEW.owned_status_id;
            IF consigned_is_consigned IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'consigned status must have is_consigned=true';
            END IF;
            IF owned_is_consigned IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'owned status must have is_consigned=false';
            END IF;
            RETURN NEW;
        END $$;
        """
    )


def _triggers() -> None:
    for table in t.REVISION_0027_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f'CREATE TRIGGER trg_consignment_agreement_item_guard BEFORE INSERT OR UPDATE ON {SCHEMA}."consignment_agreement_item" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_agreement_item()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0027_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0027_TABLES)
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

    insert_setting = text(
        """
        INSERT INTO platform.tenant_setting
            (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :namespace, :key, CAST(:value AS jsonb), :value_type, true, now(), now(), 1)
        ON CONFLICT (tenant_id, namespace, key) DO NOTHING
        """
    )
    insert_flag = text(
        """
        INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :flag_key, :enabled, 'MIGRATION', now(), now(), 1)
        ON CONFLICT (tenant_id, flag_key) DO NOTHING
        """
    )
    insert_sequence = text(
        """
        INSERT INTO platform.number_sequence
            (id, tenant_id, doc_type, scope_key, prefix, padding, next_value, reset_policy, gap_policy, period_key,
             created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :doc_type, 'TENANT', :prefix, 6, 1, 'NEVER', 'NON_GAPLESS', NULL, now(), now(), 1)
        """
    )
    for tenant in _tenants():
        for key, value, value_type in SETTING_SEEDS:
            raw = value if value_type == "boolean" else json.dumps(value)
            op.get_bind().execute(insert_setting, {
                "tenant_id": tenant, "namespace": SETTINGS_NAMESPACE, "key": SETTING_PREFIX + key,
                "value": raw, "value_type": value_type,
            })
        for flag_key, enabled in FLAG_SEEDS:
            op.get_bind().execute(insert_flag, {"tenant_id": tenant, "flag_key": flag_key, "enabled": enabled})
        for code in ("CAG", "CTR"):
            op.get_bind().execute(insert_sequence, {"tenant_id": tenant, "doc_type": code, "prefix": code})
    for name, queue, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts, singleton, is_enabled,
                 created_at, updated_at, version)
            VALUES ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{name}', '{cron}', {timeout}, 3, true, true,
                    now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _functions()
    deferred: list = []
    for table in t.REVISION_0027_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _triggers()
    _rls_and_grants()
    _seeds()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
