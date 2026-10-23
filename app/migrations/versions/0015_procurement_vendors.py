"""0015_procurement_vendors — FS-08 vendor master, categories, item-vendor associations/pricing and
purchase contracts/blanket agreements (6 tables), plus the deferred
``item_master.item_purchasing.preferred_vendor_id → procurement.vendor`` foreign key.

Chain position: depends on FS-07 ``0014_counts`` (only 0013/0014 are between the verified FS-05 head
and this revision). Additive; downgrade drops the ``procurement`` schema (0016/0017 objects are dropped
by their own downgrades first).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.procurement import tables as t

revision = "0015_procurement_vendors"
down_revision = "0014_counts"
branch_labels = None
depends_on = None

SCHEMA = "procurement"
# FS-00 ``platform.tenant_setting`` namespace CHECK does not include "procurement" (frozen FS-00
# revision); FS-08 settings are stored under the existing "inventory" namespace with a
# ``procurement.`` key prefix (same approach as FS-07 ``counts.*``; no FS-00 schema change here).
SETTINGS_NAMESPACE = "inventory"
SETTING_PREFIX = "procurement."
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

SETTING_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("advanced_receiving_mode", "true", "boolean"),
    ("allow_over_receipt", "false", "boolean"),
    ("over_receipt_tolerance_percent", "0", "number"),
    ("bill_match_tolerance_amount", "0.01", "number"),
    ("bill_match_tolerance_percent", "1.0", "number"),
    ("match.require_3way", "true", "boolean"),
    ("vendor_bill_approval_required", "false", "boolean"),
    ("multiple_vendors_enabled", "false", "boolean"),
    ("purchase_contracts_enabled", "false", "boolean"),
    ("blanket_orders_enabled", "false", "boolean"),
    ("requisitions_enabled", "false", "boolean"),
    ("purchase_requests_enabled", "false", "boolean"),
    ("rfq_enabled", "false", "boolean"),
    ("drop_ship_enabled", "false", "boolean"),
    ("receipt_exchange_rate_source", "PO", "string"),
    ("estimated_landed_cost_default", "false", "boolean"),
    ("default_variance_account_ref", None, "string"),
    ("credit_limit_enforcement", "false", "boolean"),
)

FLAG_SEEDS: tuple[tuple[str, bool], ...] = (
    ("procurement.p2p.enabled", False),
    ("procurement.multiple_vendors_enabled", False),
    ("procurement.purchase_contracts_enabled", False),
    ("procurement.blanket_orders_enabled", False),
    ("procurement.requisitions_enabled", False),
    ("procurement.purchase_requests_enabled", False),
    ("procurement.rfq_enabled", False),
    ("procurement.drop_ship_enabled", False),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
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
        """
    )
    for tenant in _tenants():
        for key, value, value_type in SETTING_SEEDS:
            raw = "null" if value is None else (
                value if value_type in {"boolean", "number"} else json.dumps(value)
            )
            op.get_bind().execute(
                insert_setting,
                {"tenant_id": tenant, "namespace": SETTINGS_NAMESPACE, "key": SETTING_PREFIX + key,
                 "value": raw, "value_type": value_type},
            )
        for flag_key, enabled in FLAG_SEEDS:
            op.get_bind().execute(insert_flag, {"tenant_id": tenant, "flag_key": flag_key, "enabled": enabled})
        for code in ("PO", "IR", "VB", "VC", "VRA"):
            op.get_bind().execute(insert_sequence, {"tenant_id": tenant, "doc_type": code, "prefix": code})


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _schema_functions()
    deferred: list = []
    for table in t.REVISION_0015_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    # Close the FS-01 deferred FK now that procurement.vendor exists.
    op.execute(
        f'ALTER TABLE item_master.item_purchasing ADD CONSTRAINT fk_item_purchasing_preferred_vendor '
        f"FOREIGN KEY (tenant_id, preferred_vendor_id) REFERENCES {SCHEMA}.vendor (tenant_id, id) "
        f"ON DELETE SET NULL"
    )
    _triggers(t.REVISION_0015_TABLES)
    _rls_and_grants(t.REVISION_0015_TABLES)
    _seeds()


def downgrade() -> None:
    op.execute("ALTER TABLE item_master.item_purchasing DROP CONSTRAINT IF EXISTS fk_item_purchasing_preferred_vendor")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
