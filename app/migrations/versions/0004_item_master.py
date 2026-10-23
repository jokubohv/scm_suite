"""0004_item_master — FS-01 item master schema/catalogue (19 tables + taxonomy/UoM/category seeds).

Creates the ``item_master`` schema, the 19 tables owned by revision 0004 (DATA-MODEL §4), standard
updated-at triggers, the D-22 immutability triggers (item header + item_inventory), tenant RLS policies,
best-effort grants and the SYSTEM-tenant seed rows for the 24 item types, UoM types/units and category
roots. Seeds are copied/ensured per tenant lazily by the service on first use (new tenants).

Revision ID: 0004_item_master
Revises: 0003_platform_events
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.models.platform import tenant as _platform_tenant
from app.modules.item_master import tables as t

if "platform.tenant" not in t.metadata.tables:  # FK resolution for compiled DDL
    _platform_tenant.to_metadata(t.metadata)

revision = "0004_item_master"
down_revision = "0003_platform_events"
branch_labels = None
depends_on = None

SCHEMA = "item_master"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

ITEM_TYPES: tuple[tuple[str, bool, bool, bool, bool, int], ...] = (
    ("INVENTORY", True, False, False, False, 10),
    ("SERIALIZED", True, False, True, False, 20),
    ("LOT_NUMBERED", True, False, True, False, 30),
    ("ASSEMBLY", True, True, False, False, 40),
    ("WIP", True, True, False, False, 50),
    ("PHANTOM", False, True, False, False, 60),
    ("OUTSOURCED", True, True, False, False, 70),
    ("MATRIX_PARENT", False, False, False, False, 80),
    ("MATRIX_CHILD", True, False, False, False, 90),
    ("KIT", False, False, False, False, 100),
    ("GROUP", False, False, False, False, 110),
    ("NON_INVENTORY", False, False, False, False, 120),
    ("DROP_SHIP", False, False, False, False, 130),
    ("SPECIAL_ORDER", False, False, False, False, 140),
    ("SERVICE", False, False, False, False, 150),
    ("DOWNLOAD", False, False, False, False, 160),
    ("GIFT_CERTIFICATE", False, False, False, False, 170),
    ("EXPENSE", False, False, False, True, 180),
    ("OTHER_CHARGE", False, False, False, True, 190),
    ("DESCRIPTION", False, False, False, True, 200),
    ("DISCOUNT", False, False, False, True, 210),
    ("MARKUP", False, False, False, True, 220),
    ("SUBTOTAL", False, False, False, True, 230),
    ("PAYMENT", False, False, False, True, 240),
)

UOM_SEEDS: tuple[tuple[str, str, str, tuple[tuple[str, str, str, bool], ...]], ...] = (
    ("EA", "Each", "quantity", (("EA", "Each", "1", True), ("CASE", "Case (12 EA)", "12", False))),
    ("WEIGHT", "Weight", "mass", (("KG", "Kilogram", "1", True), ("G", "Gram", "0.001", False), ("LB", "Pound", "0.453592370000", False))),
    ("VOLUME", "Volume", "volume", (("L", "Litre", "1", True), ("ML", "Millilitre", "0.001", False))),
)

CATEGORY_ROOTS: tuple[tuple[str, str, int], ...] = (
    ("FINISHED_GOODS", "Finished Goods", 10),
    ("RAW_MATERIALS", "Raw Materials", 20),
    ("PACKAGING", "Packaging", 30),
    ("CONSUMABLES", "Consumables", 40),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table) -> None:
    op.execute(str(CreateTable(table).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())


def _drop_schema() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _create_updated_at_function() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
        """
    )


def _install_updated_at_triggers() -> None:
    for table in t.REVISION_0004_TABLES:
        if "updated_at" not in table.c:
            continue
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table.name}_touch ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )


def _install_immutability() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_item_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.item_type_id <> OLD.item_type_id
               OR NEW.base_uom_id IS DISTINCT FROM OLD.base_uom_id
               OR NEW.units_type_id IS DISTINCT FROM OLD.units_type_id THEN
                RAISE EXCEPTION 'D-22: item_type/base_uom/units_type are immutable after save'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_item_inventory_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.costing_method <> OLD.costing_method
               OR NEW.stock_uom_id IS DISTINCT FROM OLD.stock_uom_id
               OR NEW.initial_cost IS DISTINCT FROM OLD.initial_cost THEN
                RAISE EXCEPTION 'D-22: costing_method/stock_uom/initial_cost are immutable after save'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'DROP TRIGGER IF EXISTS trg_item_immutable_saved ON {SCHEMA}."item"'
    )
    op.execute(
        f'CREATE TRIGGER trg_item_immutable_saved BEFORE UPDATE ON {SCHEMA}."item" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_item_immutable_change()"
    )
    op.execute(
        f'DROP TRIGGER IF EXISTS trg_item_inventory_immutable ON {SCHEMA}."item_inventory"'
    )
    op.execute(
        f'CREATE TRIGGER trg_item_inventory_immutable BEFORE UPDATE ON {SCHEMA}."item_inventory" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_item_inventory_immutable_change()"
    )


def _apply_rls() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0004_TABLES:
        if "tenant_id" not in table.c:
            continue
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


def _grants() -> None:
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0004_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {names} TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _seed_reference() -> None:
    for code, can_stock, requires_bom, requires_numbering, charge_only, sort_order in ITEM_TYPES:
        row_id = uuid.uuid5(uuid.NAMESPACE_URL, f"item_master/item_type/{code}")
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.item_type_definition
                (id, tenant_id, code, name, description, can_stock, requires_bom, requires_numbering,
                 is_charge_only, sort_order, is_system, created_at, updated_at, version)
            VALUES ('{row_id}', '{SYSTEM_TENANT_ID}', '{code}', '{code.replace('_', ' ').title()}', '',
                    {str(can_stock).lower()}, {str(requires_bom).lower()}, {str(requires_numbering).lower()},
                    {str(charge_only).lower()}, {sort_order}, true, now(), now(), 1)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )
    for code, name, _kind, units in UOM_SEEDS:
        type_id = uuid.uuid5(uuid.NAMESPACE_URL, f"item_master/uom_type/{code}")
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.uom_type (id, tenant_id, name, code, base_uom_id, is_active, created_at, updated_at, version)
            VALUES ('{type_id}', '{SYSTEM_TENANT_ID}', '{name}', '{code}', NULL, true, now(), now(), 1)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )
        for unit_code, unit_name, factor, is_base in units:
            unit_id = uuid.uuid5(uuid.NAMESPACE_URL, f"item_master/uom/{code}/{unit_code}")
            op.execute(
                f"""
                INSERT INTO {SCHEMA}.uom
                    (id, tenant_id, uom_type_id, code, name, conversion_factor, is_base, rounding_precision,
                     is_active, created_at, updated_at, version)
                VALUES ('{unit_id}', '{SYSTEM_TENANT_ID}', '{type_id}', '{unit_code}', '{unit_name}',
                        {factor}, {str(is_base).lower()}, 4, true, now(), now(), 1)
                ON CONFLICT (tenant_id, uom_type_id, code) DO NOTHING
                """
            )
            if is_base:
                op.execute(
                    f"UPDATE {SCHEMA}.uom_type SET base_uom_id = '{unit_id}' "
                    f"WHERE tenant_id = '{SYSTEM_TENANT_ID}' AND code = '{code}' AND base_uom_id IS NULL"
                )
    for code, name, sort_order in CATEGORY_ROOTS:
        row_id = uuid.uuid5(uuid.NAMESPACE_URL, f"item_master/item_category/{code}")
        op.execute(
            f"""
            INSERT INTO {SCHEMA}.item_category
                (id, tenant_id, name, parent_id, code, is_active, sort_order, created_at, updated_at, version)
            VALUES ('{row_id}', '{SYSTEM_TENANT_ID}', '{name}', NULL, '{code}', true, {sort_order}, now(), now(), 1)
            ON CONFLICT (tenant_id, code) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    _create_updated_at_function()
    for table in t.REVISION_0004_TABLES:
        _create_table(table)
    _install_updated_at_triggers()
    _install_immutability()
    _apply_rls()
    _grants()
    _seed_reference()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
