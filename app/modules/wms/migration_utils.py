"""Shared DDL helpers for the FS-11 WMS migrations ``0023``–``0025``.

Keeps the three revision files small and consistent: explicit PostgreSQL DDL compiled from
``app.modules.wms.tables`` (no autogenerate), tenant RLS policies, touch triggers, runtime-role grants
and the idempotent per-tenant seeds (system-rule catalogue + tenant settings + the deploy-dark flag).
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.wms import tables as t

SCHEMA = "wms"
SETTINGS_NAMESPACE = "inventory"  # FS-00 platform.tenant_setting namespace is frozen; use wms.* keys
SETTING_PREFIX = "wms."
FLAG_KEY = "wms.execution.enabled"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"


def dialect():
    return postgresql.dialect()


def create_table(table, deferred_fks: list) -> None:
    """Create one table without inline FKs; indexes + FKs are added afterwards in a stable order."""
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=dialect())).strip())
    for fk in table.foreign_key_constraints:
        deferred_fks.append(fk)


def add_fks(deferred_fks: list) -> None:
    for fk in deferred_fks:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        op.execute(
            f'ALTER TABLE {SCHEMA}."{fk.table.name}" ADD CONSTRAINT "{fk.name}" '
            f"FOREIGN KEY ({', '.join(local_columns)}) "
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )


def schema_functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        """
    )


def triggers(tables) -> None:
    for table in tables:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )


def rls_and_grants(tables) -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in tables:
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in tables)
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


def tenants() -> tuple[str, ...]:
    rows = op.get_bind().execute(text("SELECT id::text FROM platform.tenant ORDER BY created_at")).all()
    return tuple(str(row[0]) for row in rows)


def seeds_0023() -> None:
    """Per-tenant WMS defaults: settings, deploy-dark flag and the system-rule catalogue."""
    insert_setting = text(
        """
        INSERT INTO platform.tenant_setting
            (id, tenant_id, namespace, key, value, value_type, is_default, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :namespace, :key, CAST(:value AS jsonb), :value_type,
                true, now(), now(), 1)
        ON CONFLICT (tenant_id, namespace, key) DO NOTHING
        """
    )
    insert_flag = text(
        """
        INSERT INTO platform.feature_flag
            (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :flag_key, false, 'MIGRATION', now(), now(), 1)
        ON CONFLICT (tenant_id, flag_key) DO NOTHING
        """
    )
    insert_rule = text(
        """
        INSERT INTO wms.system_rule
            (id, tenant_id, code, name, description, default_enabled, applies_to, value_type, is_active,
             created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :code, :name, :description, :default_enabled, :applies_to,
                'BOOLEAN', true, now(), now(), 1)
        ON CONFLICT (tenant_id, code) DO NOTHING
        """
    )
    for tenant in tenants():
        for key, value, value_type in t.SETTING_SEEDS:
            raw: Any = value if value_type in {"boolean", "number"} else json.dumps(value)
            op.get_bind().execute(
                insert_setting,
                {
                    "tenant_id": tenant,
                    "namespace": SETTINGS_NAMESPACE,
                    "key": SETTING_PREFIX + key,
                    "value": raw,
                    "value_type": value_type,
                },
            )
        op.get_bind().execute(insert_flag, {"tenant_id": tenant, "flag_key": FLAG_KEY})
        for code, name, description, default_enabled, applies_to in t.SYSTEM_RULE_SEEDS:
            op.get_bind().execute(
                insert_rule,
                {
                    "tenant_id": tenant,
                    "code": code,
                    "name": name,
                    "description": description,
                    "default_enabled": default_enabled,
                    "applies_to": applies_to,
                },
            )


def drop_schema() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


__all__ = [
    "FLAG_KEY",
    "SCHEMA",
    "SETTINGS_NAMESPACE",
    "SETTING_PREFIX",
    "add_fks",
    "create_table",
    "drop_schema",
    "rls_and_grants",
    "schema_functions",
    "seeds_0023",
    "tenants",
    "triggers",
]
