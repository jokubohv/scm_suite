"""0018_manufacturing_master — FS-09 assembly items, BOM/revisions/components/control (6 tables),
plus the deferred ``assembly_item.default_bom_id → bom`` FK, manufacturing settings/flags/number
sequences and the Δ-REUSE used-condition inventory status seeds.

Chain position: depends on FS-08 ``0017_procurement_billing``; 0019/0020 continue the same schema.
Additive; downgrade drops the ``manufacturing`` schema (0019/0020 objects are dropped by their own
downgrades first) and the additive status seeds it created.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.manufacturing import tables as t

revision = "0018_manufacturing_master"
down_revision = "0017_procurement_billing"
branch_labels = None
depends_on = None

SCHEMA = "manufacturing"
SETTINGS_NAMESPACE = "manufacturing"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("wip.method_guard", "ENFORCE", "string"),
    ("wip.default_wip_account_ref", None, "string"),
    ("unbuild.default_variance_account_ref", None, "string"),
    ("component.commit_on_release", "true", "boolean"),
    ("component.use_yield", "true", "boolean"),
    ("component.round_up_qty", "false", "boolean"),
    ("over_issue_tolerance_pct", "0", "number"),
    ("completion.validate_sequence", "true", "boolean"),
    ("operations.overlap_enabled", "false", "boolean"),
    ("scheduling.default_method", "BACKWARD", "string"),
    ("scheduling.policy_version", "mfg-sched-v1", "string"),
    ("scheduling.max_horizon_days", "365", "number"),
    ("work_center.default_capacity_hours_per_day", "8", "number"),
    ("actual_production_dates_mode", "AUTO", "string"),
    ("allow_purchase_of_assemblies", "false", "boolean"),
    ("backflush.explicit_enabled", "false", "boolean"),
    ("wo.create_from_sales_order", "false", "boolean"),
    ("plan.generate_planned_work_orders", "false", "boolean"),
    ("bom.max_depth", "20", "number"),
    ("unbuild.require_original_build", "false", "boolean"),
    ("eco.implement_on_final_approval", "false", "boolean"),
    ("available_to_build.policy_version", "atb-v1", "string"),
    ("available_to_build.level_mode", "ALL_LEVELS", "string"),
    ("available_to_build.basis", "ON_HAND", "string"),
    ("available_to_build.include_subassemblies", "true", "boolean"),
    ("available_to_build.kit_expansion", "true", "boolean"),
    ("component.pick_interface.mode", "EVENT_AND_PULL", "string"),
    ("compat.enforcement_default", "HARD", "string"),
    ("compat.allow_override", "true", "boolean"),
    ("compat.rule_engine.max_rules_per_evaluation", "500", "number"),
    ("compat.rule_set_cache_ttl_seconds", "60", "number"),
    ("compat.validation.simulation_context", "ATB", "string"),
    ("unbuild.default_disposition", "RETURN_TO_STOCK", "string"),
    ("unbuild.value_policy", "ORIGINAL_COST", "string"),
    ("unbuild.value_policy_by_assembly", "{}", "json"),
    ("unbuild.nrv_source", "CURRENT_COST", "string"),
    ("unbuild.default_used_condition_code", "USED_A", "string"),
    ("unbuild.default_used_status_code", "USED", "string"),
    ("unbuild.default_hold_status_code", "HOLD", "string"),
    ("unbuild.allow_resale_default", "false", "boolean"),
    ("unbuild.scrap_variance_account_ref", None, "string"),
)

FLAG_SEEDS: tuple[tuple[str, bool], ...] = (
    ("manufacturing.enabled", False),
    ("manufacturing.wip.enabled", False),
)

SEQUENCE_SEEDS: tuple[tuple[str, str], ...] = (
    ("WO", "WO-"),
    ("WIP", "WIP-"),
    ("AB", "AB-"),
    ("AU", "AU-"),
    ("ECN", "ECN-"),
    ("MFGSCH", "SCH-"),
)

#: Δ-REUSE inventory statuses the platform owns additively (FS-03 seeded the base set in 0007).
USED_STATUSES: tuple[tuple[str, str], ...] = (
    ("USED", "Used"),
    ("REFURB", "Refurbished"),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list) -> None:
    op.execute(
        str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip()
    )
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        deferred_fks.append(fk)


def _add_fks(deferred_fks: list) -> None:
    for fk in deferred_fks:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        op.execute(
            f'ALTER TABLE {SCHEMA}."{fk.table.name}" ADD CONSTRAINT "{fk.name}" '
            f"FOREIGN KEY ({', '.join(local_columns)}) "
            f"REFERENCES {ref_schema}.\"{ref_table}\" ({', '.join(ref_columns)})"
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


def _triggers(tables) -> None:
    for table in tables:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    for name in t.APPEND_ONLY_TABLES:
        if any(table.name == name for table in tables):
            op.execute(
                f'CREATE TRIGGER trg_{name}_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}."{name}" '
                f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
            )


def _rls_and_grants(tables) -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in tables:
        name = table.name
        op.execute(f'ALTER TABLE {SCHEMA}."{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
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


def _tenants() -> tuple[str, ...]:
    rows = op.get_bind().execute(text("SELECT id::text FROM platform.tenant ORDER BY created_at")).all()
    return tuple(str(row[0]) for row in rows)


def _seeds() -> None:
    import json

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
        VALUES (gen_random_uuid(), :tenant_id, :flag_key, :enabled, 'MIGRATION', now(), now(), 1)
        ON CONFLICT (tenant_id, flag_key) DO NOTHING
        """
    )
    insert_sequence = text(
        """
        INSERT INTO platform.number_sequence
            (id, tenant_id, doc_type, scope_key, prefix, padding, next_value, reset_policy, gap_policy,
             period_key, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :doc_type, 'TENANT', :prefix, 6, 1, 'NEVER', 'NON_GAPLESS',
                NULL, now(), now(), 1)
        ON CONFLICT (tenant_id, doc_type, scope_key, period_key) DO NOTHING
        """
    )
    insert_status = text(
        """
        INSERT INTO traceability.inventory_status
            (id, tenant_id, code, name, is_available_for_allocation, is_committable, is_sellable,
             is_consigned, is_default, is_system, is_active, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :code, :name, false, false, false, false, false, false, true,
                now(), now(), 1)
        ON CONFLICT (tenant_id, code) DO NOTHING
        """
    )
    for tenant in _tenants():
        for key, value, value_type in SETTING_SEEDS:
            raw = "null" if value is None else (
                value if value_type in {"boolean", "number"} else json.dumps(value)
            )
            op.get_bind().execute(
                insert_setting,
                {
                    "tenant_id": tenant,
                    "namespace": SETTINGS_NAMESPACE,
                    "key": key,
                    "value": raw,
                    "value_type": value_type,
                },
            )
        for flag_key, enabled in FLAG_SEEDS:
            op.get_bind().execute(insert_flag, {"tenant_id": tenant, "flag_key": flag_key, "enabled": enabled})
        for doc_type, prefix in SEQUENCE_SEEDS:
            op.get_bind().execute(
                insert_sequence, {"tenant_id": tenant, "doc_type": doc_type, "prefix": prefix}
            )
        if tenant != SYSTEM_TENANT_ID:
            for code, name in USED_STATUSES:
                op.get_bind().execute(insert_status, {"tenant_id": tenant, "code": code, "name": name})


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    deferred: list = []
    for table in t.MASTER_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _schema_functions()
    _triggers(t.MASTER_TABLES)
    _rls_and_grants(t.MASTER_TABLES)
    _seeds()


def downgrade() -> None:
    op.execute(
        "DELETE FROM traceability.inventory_status WHERE code IN ('USED', 'REFURB') "
        "AND tenant_id <> '00000000-0000-0000-0000-000000000000' "
        "AND NOT EXISTS (SELECT 1 FROM inventory_ledger.inventory_transaction_detail d "
        "WHERE d.tenant_id = traceability.inventory_status.tenant_id "
        "AND d.inventory_status_id = traceability.inventory_status.id)"
    )
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
