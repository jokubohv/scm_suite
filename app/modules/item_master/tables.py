"""SQLAlchemy Core metadata for the 21 `item_master` tables (FS-01, DATA-MODEL §4).

Migrations 0004/0005 compile this metadata into explicit PostgreSQL DDL; module repositories
(`app.modules.item_master.repository`) query these tables with async SQLAlchemy Core. No ORM imports.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="item_master", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)


def _audit(columns: list) -> None:
    columns.extend(
        [
            Column("created_at", STAMP, nullable=False, server_default=text("now()")),
            Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
            Column("created_by", UUID_T, nullable=True),
            Column("updated_by", UUID_T, nullable=True),
            Column("version", Integer, nullable=False, server_default=text("1")),
        ]
    )


def _tenant_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant")


def mk(name: str, columns: list, *, extra: list | None = None, tenant: bool = True) -> Table:
    cols: list = [Column("id", UUID_T, primary_key=True, nullable=False)]
    if tenant:
        cols.append(Column("tenant_id", UUID_T, nullable=False))
    cols.extend(columns)
    _audit(cols)
    constraints: list = list(extra or [])
    if tenant:
        constraints.append(_tenant_fk(name))
        if "id" in {column.name for column in cols}:
            constraints.append(UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"))
    return Table(name, metadata, *cols, *constraints)


# --------------------------------------------------------------------------- taxonomy / UoM --
item_type_definition = Table(
    "item_type_definition",
    metadata,
    Column("id", UUID_T, primary_key=True, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("can_stock", Boolean, nullable=False, server_default=text("false")),
    Column("requires_bom", Boolean, nullable=False, server_default=text("false")),
    Column("requires_numbering", Boolean, nullable=False, server_default=text("false")),
    Column("is_charge_only", Boolean, nullable=False, server_default=text("false")),
    Column("sort_order", Integer, nullable=False, server_default=text("100")),
    Column("is_system", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    UniqueConstraint("tenant_id", "code", name="uq_item_type_definition_code"),
    UniqueConstraint("tenant_id", "id", name="uq_item_type_definition_tenant_id_id"),
    _tenant_fk("item_type_definition"),
)

uom_type = mk(
    "uom_type",
    [
        Column("name", Text, nullable=False),
        Column("code", Text, nullable=False),
        Column("base_uom_id", UUID_T, nullable=True),  # FK added in 0005 after uom exists
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[UniqueConstraint("tenant_id", "code", name="uq_uom_type_code")],
)

uom = mk(
    "uom",
    [
        Column("uom_type_id", UUID_T, nullable=False),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("conversion_factor", QTY, nullable=False, server_default=text("1")),
        Column("is_base", Boolean, nullable=False, server_default=text("false")),
        Column("rounding_precision", Integer, nullable=False, server_default=text("4")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "uom_type_id", "code", name="uq_uom_type_code_scope"),
        ForeignKeyConstraint(["tenant_id", "uom_type_id"], ["item_master.uom_type.tenant_id", "item_master.uom_type.id"], name="fk_uom_type"),
        CheckConstraint("conversion_factor > 0", name="uom_factor_positive"),
    ],
)
Index("ix_uom_type_scope", uom.c.tenant_id, uom.c.uom_type_id)

item = mk(
    "item",
    [
        Column("item_number", Text, nullable=False),
        Column("display_name", Text, nullable=False),
        Column("description", Text, nullable=True),
        Column("item_type_id", UUID_T, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("inactive_at", STAMP, nullable=True),
        Column("matrix_parent_id", UUID_T, nullable=True),
        Column("base_uom_id", UUID_T, nullable=True),
        Column("units_type_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("upc_code", Text, nullable=True),
        Column("external_ref", Text, nullable=True),
        Column("created_from", Text, nullable=False, server_default=text("'MANUAL'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_number", name="uq_item_number"),
        ForeignKeyConstraint(["tenant_id", "item_type_id"], ["item_master.item_type_definition.tenant_id", "item_master.item_type_definition.id"], name="fk_item_type"),
        ForeignKeyConstraint(["tenant_id", "base_uom_id"], ["item_master.uom.tenant_id", "item_master.uom.id"], name="fk_item_base_uom"),
        ForeignKeyConstraint(["tenant_id", "units_type_id"], ["item_master.uom_type.tenant_id", "item_master.uom_type.id"], name="fk_item_units_type"),
        ForeignKeyConstraint(["tenant_id", "item_category_id"], ["item_master.item_category.tenant_id", "item_master.item_category.id"], name="fk_item_category"),
        ForeignKeyConstraint(["tenant_id", "matrix_parent_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_matrix_parent"),
        CheckConstraint("item_number <> ''", name="item_number_not_empty"),
        CheckConstraint("created_from IN ('MANUAL','IMPORT','MATRIX','BOM')", name="item_created_from"),
    ],
)
Index("ix_item_type_active", item.c.tenant_id, item.c.item_type_id, item.c.is_active)
Index("uq_item_upc", item.c.tenant_id, item.c.upc_code, unique=True, postgresql_where=text("upc_code IS NOT NULL"))

item_category = mk(
    "item_category",
    [
        Column("name", Text, nullable=False),
        Column("parent_id", UUID_T, nullable=True),
        Column("code", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("sort_order", Integer, nullable=False, server_default=text("100")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_item_category_code"),
        UniqueConstraint("tenant_id", "parent_id", "name", name="uq_item_category_parent_name"),
        ForeignKeyConstraint(["tenant_id", "parent_id"], ["item_master.item_category.tenant_id", "item_master.item_category.id"], name="fk_item_category_parent"),
    ],
)
Index("ix_item_category_parent", item_category.c.tenant_id, item_category.c.parent_id)

item_category_member = Table(
    "item_category_member",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("category_id", UUID_T, nullable=False),
    Column("is_primary", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", "category_id", name="pk_item_category_member"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_category_member_item"),
    ForeignKeyConstraint(["tenant_id", "category_id"], ["item_master.item_category.tenant_id", "item_master.item_category.id"], name="fk_item_category_member_category"),
    _tenant_fk("item_category_member"),
)
Index("uq_item_category_primary", item_category_member.c.tenant_id, item_category_member.c.item_id, unique=True, postgresql_where=text("is_primary"))
Index("ix_item_category_member_category", item_category_member.c.tenant_id, item_category_member.c.category_id)

# --------------------------------------------------------------------------- extensions ----
item_inventory = Table(
    "item_inventory",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("costing_method", Text, nullable=False, server_default=text("'AVERAGE'")),
    Column("costing_method_locked", Boolean, nullable=False, server_default=text("true")),
    Column("initial_cost", QTY, nullable=True),
    Column("asset_account_ref", Text, nullable=True),
    Column("cogs_account_ref", Text, nullable=True),
    Column("adjustment_account_ref", Text, nullable=True),
    Column("variance_account_ref", Text, nullable=True),
    Column("unbuild_variance_account_ref", Text, nullable=True),
    Column("reorder_point", QTY, nullable=False, server_default=text("0")),
    Column("preferred_stock_level", QTY, nullable=False, server_default=text("0")),
    Column("safety_stock", QTY, nullable=False, server_default=text("0")),
    Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
    Column("seasonal_demand", QTY, nullable=False, server_default=text("0")),
    Column("auto_calculate_planning", Boolean, nullable=False, server_default=text("false")),
    Column("use_bins", Boolean, nullable=False, server_default=text("false")),
    Column("bin_required_on_txn", Boolean, nullable=False, server_default=text("false")),
    Column("use_statuses", Boolean, nullable=False, server_default=text("false")),
    Column("default_status_id", UUID_T, nullable=True),
    Column("allow_negative_inventory", Boolean, nullable=False, server_default=text("true")),
    Column("allow_vendor_consignment", Boolean, nullable=False, server_default=text("false")),
    Column("is_wip", Boolean, nullable=False, server_default=text("false")),
    Column("backflush_default", Boolean, nullable=False, server_default=text("false")),
    Column("yield_percent", QTY, nullable=False, server_default=text("100")),
    Column("round_up_component_qty", Boolean, nullable=False, server_default=text("false")),
    Column("stock_uom_id", UUID_T, nullable=True),
    Column("must_be_whole_base_unit", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", name="pk_item_inventory"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_inventory_item"),
    CheckConstraint("costing_method IN ('AVERAGE','FIFO','LIFO','STANDARD','GROUP_AVERAGE','SPECIFIC')", name="item_inventory_costing_method"),
    CheckConstraint("reorder_point >= 0 AND preferred_stock_level >= 0 AND safety_stock >= 0 AND seasonal_demand >= 0", name="item_inventory_planning_non_negative"),
    CheckConstraint("yield_percent > 0 AND yield_percent <= 100", name="item_inventory_yield"),
    _tenant_fk("item_inventory"),
)
Index("ix_item_inventory_costing", item_inventory.c.tenant_id, item_inventory.c.costing_method)

item_purchasing = Table(
    "item_purchasing",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("preferred_vendor_id", UUID_T, nullable=True),
    Column("purchase_uom_id", UUID_T, nullable=True),
    Column("purchase_price", QTY, nullable=True),
    Column("purchase_price_currency", Text, nullable=True),
    Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
    Column("is_drop_ship", Boolean, nullable=False, server_default=text("false")),
    Column("is_special_order", Boolean, nullable=False, server_default=text("false")),
    Column("country_of_origin", Text, nullable=True),
    Column("harmonized_code", Text, nullable=True),
    Column("purchase_tax_code", Text, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", name="pk_item_purchasing"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_purchasing_item"),
    CheckConstraint("purchase_price IS NULL OR purchase_price >= 0", name="item_purchasing_price"),
    _tenant_fk("item_purchasing"),
)
Index("ix_item_purchasing_vendor", item_purchasing.c.tenant_id, item_purchasing.c.preferred_vendor_id)

item_sales = Table(
    "item_sales",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("sale_uom_id", UUID_T, nullable=True),
    Column("price_level_id", UUID_T, nullable=True),
    Column("is_taxable", Boolean, nullable=False, server_default=text("true")),
    Column("shipping_class", Text, nullable=True),
    Column("weight", QTY, nullable=True),
    Column("weight_uom", Text, nullable=True),
    Column("is_sellable", Boolean, nullable=False, server_default=text("true")),
    Column("is_shippable", Boolean, nullable=False, server_default=text("true")),
    Column("quantity_pricing_schedule_id", UUID_T, nullable=True),
    Column("pricing_group_id", UUID_T, nullable=True),
    Column("matrix_parent_qty_behavior", Text, nullable=False, server_default=text("'LINE'")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", name="pk_item_sales"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_sales_item"),
    CheckConstraint("weight IS NULL OR weight >= 0", name="item_sales_weight"),
    CheckConstraint("matrix_parent_qty_behavior IN ('LINE','OVERALL','PARENT','SCHEDULE')", name="item_sales_qty_behavior"),
    _tenant_fk("item_sales"),
)
Index("ix_item_sales_sellable", item_sales.c.tenant_id, item_sales.c.is_sellable)

item_accounting = Table(
    "item_accounting",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("income_account_ref", Text, nullable=True),
    Column("cogs_account_ref", Text, nullable=True),
    Column("asset_account_ref", Text, nullable=True),
    Column("expense_account_ref", Text, nullable=True),
    Column("variance_account_ref", Text, nullable=True),
    Column("deferred_revenue_account_ref", Text, nullable=True),
    Column("tax_schedule_ref", Text, nullable=True),
    Column("revenue_recognition_template", Text, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", name="pk_item_accounting"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_accounting_item"),
    _tenant_fk("item_accounting"),
)
Index("ix_item_accounting_income", item_accounting.c.tenant_id, item_accounting.c.income_account_ref)

item_location = Table(
    "item_location",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("location_id", UUID_T, nullable=False),
    Column("is_preferred", Boolean, nullable=False, server_default=text("false")),
    Column("reorder_point", QTY, nullable=False, server_default=text("0")),
    Column("preferred_stock_level", QTY, nullable=False, server_default=text("0")),
    Column("safety_stock", QTY, nullable=False, server_default=text("0")),
    Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
    Column("seasonal_demand", QTY, nullable=False, server_default=text("0")),
    Column("auto_calculate_planning", Boolean, nullable=False, server_default=text("false")),
    Column("default_bin_id", UUID_T, nullable=True),
    Column("default_status_id", UUID_T, nullable=True),
    Column("cost_group_override_id", UUID_T, nullable=True),
    Column("is_consigned_allowed", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    PrimaryKeyConstraint("tenant_id", "item_id", "location_id", name="pk_item_location"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_location_item"),
    CheckConstraint("reorder_point >= 0 AND preferred_stock_level >= 0 AND safety_stock >= 0 AND seasonal_demand >= 0", name="item_location_planning_non_negative"),
    _tenant_fk("item_location"),
)
Index("uq_item_location_preferred", item_location.c.tenant_id, item_location.c.item_id, unique=True, postgresql_where=text("is_preferred"))
Index("ix_item_location_location", item_location.c.tenant_id, item_location.c.location_id)

item_bin = Table(
    "item_bin",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("location_id", UUID_T, nullable=False),
    Column("bin_id", UUID_T, nullable=False),
    Column("is_preferred", Boolean, nullable=False, server_default=text("false")),
    Column("is_default_receiving", Boolean, nullable=False, server_default=text("false")),
    Column("is_default_picking", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    PrimaryKeyConstraint("tenant_id", "item_id", "bin_id", name="pk_item_bin"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_bin_item"),
    _tenant_fk("item_bin"),
)
Index("uq_item_bin_preferred", item_bin.c.tenant_id, item_bin.c.item_id, item_bin.c.location_id, unique=True, postgresql_where=text("is_preferred"))
Index("ix_item_bin_bin", item_bin.c.tenant_id, item_bin.c.bin_id)

# --------------------------------------------------------------------------- matrix --------
item_matrix_option = mk(
    "item_matrix_option",
    [
        Column("parent_item_id", UUID_T, nullable=False),
        Column("name", Text, nullable=False),
        Column("code", Text, nullable=False),
        Column("sort_order", Integer, nullable=False, server_default=text("100")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "parent_item_id", "code", name="uq_item_matrix_option_code"),
        ForeignKeyConstraint(["tenant_id", "parent_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_matrix_option_parent"),
    ],
)
Index("ix_item_matrix_option_parent", item_matrix_option.c.tenant_id, item_matrix_option.c.parent_item_id)

item_matrix_option_value = mk(
    "item_matrix_option_value",
    [
        Column("option_id", UUID_T, nullable=False),
        Column("value", Text, nullable=False),
        Column("code", Text, nullable=False),
        Column("sort_order", Integer, nullable=False, server_default=text("100")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "option_id", "code", name="uq_item_matrix_option_value_code"),
        ForeignKeyConstraint(["tenant_id", "option_id"], ["item_master.item_matrix_option.tenant_id", "item_master.item_matrix_option.id"], name="fk_item_matrix_option_value_option"),
    ],
)
Index("ix_item_matrix_option_value_option", item_matrix_option_value.c.tenant_id, item_matrix_option_value.c.option_id)

item_matrix_member = Table(
    "item_matrix_member",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("parent_item_id", UUID_T, nullable=False),
    Column("child_item_id", UUID_T, nullable=False),
    Column("option_values", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "parent_item_id", "child_item_id", name="pk_item_matrix_member"),
    UniqueConstraint("tenant_id", "child_item_id", name="uq_item_matrix_member_child"),
    ForeignKeyConstraint(["tenant_id", "parent_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_matrix_member_parent"),
    ForeignKeyConstraint(["tenant_id", "child_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_matrix_member_child"),
    _tenant_fk("item_matrix_member"),
)
Index("ix_item_matrix_member_child", item_matrix_member.c.tenant_id, item_matrix_member.c.child_item_id)

# --------------------------------------------------------------------------- codes/aliases --
item_barcode = mk(
    "item_barcode",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("barcode", Text, nullable=False),
        Column("barcode_type", Text, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("is_primary", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "barcode", name="uq_item_barcode_value"),
        ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_barcode_item"),
        CheckConstraint("barcode_type IN ('UPC_A','UPC_E','EAN_13','CODE128','GS1')", name="item_barcode_type"),
    ],
)
Index("ix_item_barcode_item", item_barcode.c.tenant_id, item_barcode.c.item_id)
Index("uq_item_barcode_primary", item_barcode.c.tenant_id, item_barcode.c.item_id, unique=True, postgresql_where=text("is_primary"))

item_alias = mk(
    "item_alias",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("alias", Text, nullable=False),
        Column("alias_type", Text, nullable=False, server_default=text("'WMS'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "alias", "alias_type", name="uq_item_alias_scope"),
        ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_alias_item"),
        CheckConstraint("alias_type IN ('WMS','SUPPLIER','CUSTOMER_EXT','LEGACY')", name="item_alias_type"),
    ],
)
Index("ix_item_alias_item", item_alias.c.tenant_id, item_alias.c.item_id)

item_related = mk(
    "item_related",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("related_item_id", UUID_T, nullable=False),
        Column("relation_type", Text, nullable=False, server_default=text("'RELATED'")),
        Column("sort_order", Integer, nullable=False, server_default=text("100")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "related_item_id", "relation_type", name="uq_item_related_scope"),
        ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_related_item"),
        ForeignKeyConstraint(["tenant_id", "related_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_related_related"),
        CheckConstraint("item_id <> related_item_id", name="item_related_not_self"),
        CheckConstraint("relation_type IN ('RELATED','UPSELL','CROSS_SELL','SUBSTITUTE')", name="item_related_type"),
    ],
)
Index("ix_item_related_related", item_related.c.tenant_id, item_related.c.related_item_id)

kit_group_member = mk(
    "kit_group_member",
    [
        Column("parent_item_id", UUID_T, nullable=False),
        Column("member_item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=False),
        Column("is_optional", Boolean, nullable=False, server_default=text("false")),
        Column("sort_order", Integer, nullable=False, server_default=text("100")),
        Column("member_type", Text, nullable=False, server_default=text("'KIT_COMPONENT'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "parent_item_id", "member_item_id", name="uq_kit_group_member_scope"),
        ForeignKeyConstraint(["tenant_id", "parent_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_kit_group_member_parent"),
        ForeignKeyConstraint(["tenant_id", "member_item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_kit_group_member_member"),
        CheckConstraint("qty > 0", name="kit_group_member_qty_positive"),
        CheckConstraint("parent_item_id <> member_item_id", name="kit_group_member_not_self"),
        CheckConstraint("member_type IN ('KIT_COMPONENT','GROUP_MEMBER')", name="kit_group_member_type"),
    ],
)
Index("ix_kit_group_member_member", kit_group_member.c.tenant_id, kit_group_member.c.member_item_id)

# --------------------------------------------------------------------------- UoM (0005) ----
uom_conversion = mk(
    "uom_conversion",
    [
        Column("uom_type_id", UUID_T, nullable=False),
        Column("from_uom_id", UUID_T, nullable=False),
        Column("to_uom_id", UUID_T, nullable=False),
        Column("factor", QTY, nullable=False),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "uom_type_id", "from_uom_id", "to_uom_id", name="uq_uom_conversion_scope"),
        CheckConstraint("factor > 0 AND from_uom_id <> to_uom_id", name="uom_conversion_factor"),
        ForeignKeyConstraint(["tenant_id", "uom_type_id"], ["item_master.uom_type.tenant_id", "item_master.uom_type.id"], name="fk_uom_conversion_type"),
        ForeignKeyConstraint(["tenant_id", "from_uom_id"], ["item_master.uom.tenant_id", "item_master.uom.id"], name="fk_uom_conversion_from"),
        ForeignKeyConstraint(["tenant_id", "to_uom_id"], ["item_master.uom.tenant_id", "item_master.uom.id"], name="fk_uom_conversion_to"),
    ],
)
Index("ix_uom_conversion_to", uom_conversion.c.tenant_id, uom_conversion.c.to_uom_id)

item_uom = Table(
    "item_uom",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("uom_id", UUID_T, nullable=False),
    Column("role", Text, nullable=False),
    Column("conversion_override", QTY, nullable=True),
    Column("is_default_for_role", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("tenant_id", "item_id", "uom_id", "role", name="pk_item_uom"),
    ForeignKeyConstraint(["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_uom_item"),
    ForeignKeyConstraint(["tenant_id", "uom_id"], ["item_master.uom.tenant_id", "item_master.uom.id"], name="fk_item_uom_uom"),
    CheckConstraint("role IN ('STOCK','PURCHASE','SALE','CONSUMPTION','DEFAULT')", name="item_uom_role"),
    CheckConstraint("conversion_override IS NULL OR conversion_override > 0", name="item_uom_conversion_override"),
    _tenant_fk("item_uom"),
)
Index("uq_item_uom_default_role", item_uom.c.tenant_id, item_uom.c.item_id, item_uom.c.role, unique=True, postgresql_where=text("is_default_for_role"))
Index("ix_item_uom_uom", item_uom.c.tenant_id, item_uom.c.uom_id)

# --------------------------------------------------------------------------- revision sets --
REVISION_0004_TABLES: tuple[Table, ...] = (
    item_type_definition,
    uom_type,
    uom,
    item_category,
    item,
    item_category_member,
    item_matrix_option,
    item_matrix_option_value,
    item_matrix_member,
    item_barcode,
    item_alias,
    item_related,
    kit_group_member,
    item_inventory,
    item_purchasing,
    item_sales,
    item_accounting,
    item_location,
    item_bin,
)

REVISION_0005_TABLES: tuple[Table, ...] = (uom_conversion, item_uom)

ALL_TABLES: tuple[Table, ...] = REVISION_0004_TABLES + REVISION_0005_TABLES

assert len(ALL_TABLES) == 21
