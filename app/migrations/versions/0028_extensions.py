"""0028_extensions — FS-14 extension registries: item warranty terms, registrations, claims/actions,
repair-order data, customer part numbers and the Auto Assign UPC registry (7 tables) + settings/flags/jobs/sequences.

Chain position: after FS-13 ``0027_consigned``. Additive; downgrade drops the ``extensions`` schema.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.extensions import tables as t

revision = "0028_extensions"
down_revision = "0027_consigned"
branch_labels = None
depends_on = None

SCHEMA = "extensions"
SETTINGS_NAMESPACE = "inventory"
SETTING_PREFIX = "extensions."
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("upc.auto_assign", "false", "boolean"),
    ("upc.company_prefix", "", "string"),
    ("upc.auto_assign_batch_limit", "1000", "number"),
    ("warranty.default_duration_months", "12", "number"),
    ("warranty.expiry_scan_days", "1", "number"),
    ("claims.allow_unregistered", "false", "boolean"),
    ("claims.override_requires_approval", "true", "boolean"),
    ("customer_part.import_limit", "5000", "number"),
    ("warranty.registration_import_limit", "5000", "number"),
)

FLAG_SEEDS: tuple[tuple[str, bool], ...] = (
    ("extensions.registry.enabled", False),
    ("extensions.upc.auto_assign", False),
)

JOB_SEEDS: tuple[tuple[str, str, str | None, int], ...] = (
    ("extensions.warranty.expiry", "default", "0 1 * * *", 1800),
    ("extensions.claims.reconcile", "default", "*/30 * * * *", 900),
    ("extensions.upc.reconcile", "default", "0 2 * * *", 1800),
    ("extensions.upc.assign", "default", "0 3 * * *", 1800),
    ("extensions.registration.import.process", "default", None, 1800),
    ("extensions.customer_part.import.process", "default", None, 1800),
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


def _triggers() -> None:
    for table in t.REVISION_0028_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0028_TABLES:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0028_TABLES)
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
            raw = value if value_type in {"boolean", "number"} else json.dumps(value)
            op.get_bind().execute(insert_setting, {
                "tenant_id": tenant, "namespace": SETTINGS_NAMESPACE, "key": SETTING_PREFIX + key,
                "value": raw, "value_type": value_type,
            })
        for flag_key, enabled in FLAG_SEEDS:
            op.get_bind().execute(insert_flag, {"tenant_id": tenant, "flag_key": flag_key, "enabled": enabled})
        for code in ("WR", "WC", "REP"):
            op.get_bind().execute(insert_sequence, {"tenant_id": tenant, "doc_type": code, "prefix": code})
    for name, queue, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        cron_sql = f"'{cron}'" if cron else "NULL"
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts, singleton, is_enabled,
                 created_at, updated_at, version)
            VALUES ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{name}', {cron_sql}, {timeout}, 3, true, true,
                    now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _functions()
    deferred: list = []
    for table in t.REVISION_0028_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _triggers()
    _rls_and_grants()
    _seeds()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
