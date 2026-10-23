"""SQLAlchemy Core metadata for the 25 ``costing`` tables (FS-05, DATA-MODEL §8).

Migration ``0011_costing`` / ``0012_costing_standard`` compile this metadata into explicit PostgreSQL
DDL; module repositories query these tables with async SQLAlchemy Core. No ORM imports and no
cross-module ORM coupling: item/location/currency references are composite tenant FKs by table name and
the item-master/location read surfaces go through the frozen contracts.

Partitioned tables (ARCHITECTURE §3.1/§3.3, DATA-MODEL §1.6):

* ``cost_ledger_entry`` — monthly RANGE partitions on ``effective_date``; PK ``(id, effective_date)``.
* ``cost_layer_consumption`` — monthly RANGE partitions on ``created_at``; PK ``(id, created_at)``.

PostgreSQL requires the partition key in every unique constraint of a partitioned table, therefore the
DATA-MODEL uniqueness keys on these two tables carry the partition column as an extra trailing column
(``uq_entry_line_run`` / ``uq_consumption_seq``); documented in the migration docstring (reported gap).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
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

metadata = MetaData(schema="costing", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

METHODS = ("AVERAGE", "FIFO", "LIFO", "GROUP_AVERAGE", "STANDARD", "SPECIFIC", "LOT")
LAYER_STATUSES = ("OPEN", "EXHAUSTED", "REVALUED")
COST_STATUSES = ("CURRENT", "PENDING_RECALC", "UNDERWATER")
GROUP_STATUSES = ("ACTIVE", "BALANCING", "INACTIVE")
MOVEMENT_TYPES = (
    "IN",
    "OUT",
    "ADJUST",
    "VARIANCE",
    "REVALUE",
    "LANDED",
    "COGS_ADJUST",
    "TRANSFER",
)
CATEGORY_TYPES = ("MATERIAL", "LABOR", "MACHINE", "OVERHEAD", "FREIGHT", "DUTY", "OTHER")
VARIANCE_TYPES = (
    "PURCHASE_PRICE",
    "BILL_QUANTITY",
    "BILL_PRICE",
    "EXCHANGE_RATE",
    "PRODUCTION_QUANTITY",
    "PRODUCTION_COST",
    "UNBUILT",
    "WIP_REVALUATION",
    "ROUNDING",
    "LANDED_COST_ABSORPTION",
    "COGS_ADJUSTMENT",
)
LANDED_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "POSTED", "REVERSED", "VOID")
COGS_ADJUSTMENT_TYPES = ("UNDERWATER_TRUE_UP", "RECALC", "ROUNDING", "RETURN_COST")
REVALUATION_TYPES = ("STANDARD_CHANGE", "COST_CORRECTION")
ALLOCATION_BASES = ("WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE")


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


def mk(name: str, columns: list, *, extra: list | None = None, indexes: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.extend(
        [
            PrimaryKeyConstraint("id", name=f"pk_{name}"),
            UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
            _tenant_fk(name),
        ]
    )
    table = Table(name, metadata, *cols, *constraints)
    for index in indexes or []:
        Index(f"ix_{name}_{index[0]}", table.c.tenant_id, *(table.c[column] for column in index[1:]))
    return table


def _fk(local: str, ref_schema: str, ref_table: str, *, nullable: bool = False) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", local],
        [f"{ref_schema}.{ref_table}.tenant_id", f"{ref_schema}.{ref_table}.id"],
        name=f"fk_{ref_table}_{local}",
    )


# ==============================================================================================
# 0011_costing — cost objects, layers, ledger, categories/templates, variances, COGS,
#                 landed cost, group average (18 tables)
# ==============================================================================================
item_cost = mk(
    "item_cost",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=True),
        Column("location_costing_group_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("costing_method", Text, nullable=False),
        Column("qty_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("avg_cost", MONEY, nullable=False, server_default=text("0")),
        Column("last_purchase_price", MONEY, nullable=True),
        Column("last_cost_ledger_seq", BigInteger, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'CURRENT'")),
        Column("cost_provisional", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "location_id", "location_costing_group_id", "lot_id", "serial_id",
            name="uq_item_cost_key", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"costing_method IN {METHODS!r}".replace("'", "'"), name="ck_item_cost_method"),
        CheckConstraint("qty_on_hand <> 0 OR value_base = 0", name="ck_item_cost_value_exists"),
        CheckConstraint(f"status IN {COST_STATUSES!r}".replace("'", "'"), name="ck_item_cost_status"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location", nullable=True),
        ForeignKeyConstraint(
            ["tenant_id", "location_costing_group_id"],
            ["costing.location_costing_group.tenant_id", "costing.location_costing_group.id"],
            name="fk_item_cost_group",
        ),
    ],
    indexes=[("item_status", "item_id", "status"), ("item_location", "item_id", "location_id")],
)
Index(
    "ix_item_cost_pending",
    item_cost.c.tenant_id,
    item_cost.c.status,
    postgresql_where=text("status <> 'CURRENT'"),
)

cost_layer = mk(
    "cost_layer",
    [
        Column("item_cost_id", UUID_T, nullable=False),
        Column("layer_seq", BigInteger, nullable=False),
        Column("source_inventory_transaction_line_id", UUID_T, nullable=True),
        Column("layer_date", Date, nullable=False),
        Column("qty_original", QTY, nullable=False),
        Column("qty_remaining", QTY, nullable=False),
        Column("unit_cost", MONEY, nullable=False),
        Column("value_remaining", MONEY, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("derived_from_layer_id", UUID_T, nullable=True),
        Column("estimated", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_cost_id", "layer_seq", name="uq_layer_seq"),
        CheckConstraint("qty_remaining >= 0", name="ck_layer_qty_remaining"),
        CheckConstraint("qty_remaining <= qty_original", name="ck_layer_qty_bound"),
        CheckConstraint("value_remaining >= 0", name="ck_layer_value"),
        CheckConstraint(f"status IN {LAYER_STATUSES!r}".replace("'", "'"), name="ck_layer_status"),
        CheckConstraint(
            "(status = 'EXHAUSTED') = (qty_remaining = 0)", name="ck_layer_status_consistent"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_cost_id"], ["costing.item_cost.tenant_id", "costing.item_cost.id"],
            name="fk_layer_item_cost",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "derived_from_layer_id"], ["costing.cost_layer.tenant_id", "costing.cost_layer.id"],
            name="fk_layer_derived",
        ),
    ],
    indexes=[("item_cost_status", "item_cost_id", "status", "layer_date", "layer_seq")],
)

cost_layer_consumption = Table(
    "cost_layer_consumption",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("id", UUID_T, nullable=False),
    Column("cost_layer_id", UUID_T, nullable=False),
    Column("consuming_transaction_line_id", UUID_T, nullable=False),
    Column("qty", QTY, nullable=False),
    Column("value", MONEY, nullable=False),
    Column("consumption_seq", BigInteger, nullable=False),
    Column("cost_ledger_entry_id", UUID_T, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("id", "created_at", name="pk_cost_layer_consumption"),
    UniqueConstraint(
        "tenant_id", "cost_layer_id", "consumption_seq", "created_at", name="uq_consumption_seq"
    ),
    CheckConstraint("qty > 0", name="ck_consumption_qty"),
    CheckConstraint("value >= 0", name="ck_consumption_value"),
    _tenant_fk("cost_layer_consumption"),
    ForeignKeyConstraint(
        ["tenant_id", "cost_layer_id"], ["costing.cost_layer.tenant_id", "costing.cost_layer.id"],
        name="fk_consumption_layer",
    ),
    postgresql_partition_by="RANGE (created_at)",
)
Index("ix_consumption_layer", cost_layer_consumption.c.tenant_id, cost_layer_consumption.c.cost_layer_id)
Index("ix_consumption_line", cost_layer_consumption.c.tenant_id, cost_layer_consumption.c.consuming_transaction_line_id)
Index("ix_consumption_brin", cost_layer_consumption.c.created_at, postgresql_using="brin")

cost_ledger_entry = Table(
    "cost_ledger_entry",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("id", UUID_T, nullable=False),
    Column("entry_seq", BigInteger, nullable=False),
    Column("inventory_transaction_id", UUID_T, nullable=True),
    Column("inventory_transaction_line_id", UUID_T, nullable=True),
    Column("item_cost_id", UUID_T, nullable=False),
    Column("movement_type", Text, nullable=False),
    Column("qty_delta", QTY, nullable=False, server_default=text("0")),
    Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
    Column("value_delta", MONEY, nullable=False, server_default=text("0")),
    Column("costing_method", Text, nullable=False),
    Column("layer_id", UUID_T, nullable=True),
    Column("cost_run_id", UUID_T, nullable=True),
    Column("estimated", Boolean, nullable=False, server_default=text("false")),
    Column("rounding_residual", Boolean, nullable=False, server_default=text("false")),
    Column("effective_date", Date, nullable=False),
    Column("fiscal_period_id", UUID_T, nullable=True),
    Column("period_shifted", Boolean, nullable=False, server_default=text("false")),
    Column("reverses_entry_id", UUID_T, nullable=True),
    Column("adjusts_period_id", UUID_T, nullable=True),
    PrimaryKeyConstraint("id", "effective_date", name="pk_cost_ledger_entry"),
    UniqueConstraint("tenant_id", "entry_seq", "effective_date", name="uq_entry_seq"),
    UniqueConstraint(
        "tenant_id", "inventory_transaction_line_id", "movement_type", "cost_run_id", "effective_date",
        name="uq_entry_line_run", postgresql_nulls_not_distinct=True,
    ),
    CheckConstraint(f"movement_type IN {MOVEMENT_TYPES!r}".replace("'", "'"), name="ck_entry_movement_type"),
    CheckConstraint(f"costing_method IN {METHODS!r}".replace("'", "'"), name="ck_entry_method"),
    _tenant_fk("cost_ledger_entry"),
    ForeignKeyConstraint(
        ["tenant_id", "item_cost_id"], ["costing.item_cost.tenant_id", "costing.item_cost.id"],
        name="fk_entry_item_cost",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "layer_id"], ["costing.cost_layer.tenant_id", "costing.cost_layer.id"],
        name="fk_entry_layer",
    ),
    postgresql_partition_by="RANGE (effective_date)",
)
Index("ix_entry_item_cost", cost_ledger_entry.c.tenant_id, cost_ledger_entry.c.item_cost_id, cost_ledger_entry.c.effective_date)
Index("ix_entry_line", cost_ledger_entry.c.tenant_id, cost_ledger_entry.c.inventory_transaction_line_id)
Index("ix_entry_run", cost_ledger_entry.c.tenant_id, cost_ledger_entry.c.cost_run_id)
Index("ix_entry_brin", cost_ledger_entry.c.effective_date, postgresql_using="brin")

cost_category = mk(
    "cost_category",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("category_type", Text, nullable=False),
        Column("is_rollup_input", Boolean, nullable=False, server_default=text("false")),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_cost_category_code"),
        CheckConstraint(f"category_type IN {CATEGORY_TYPES!r}".replace("'", "'"), name="ck_cost_category_type"),
        CheckConstraint("sort_order >= 0", name="ck_cost_category_sort"),
    ],
    indexes=[("active", "is_active")],
)

cost_template = mk(
    "cost_template",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("item_type_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_cost_template_code"),
        CheckConstraint("NOT (item_type_id IS NOT NULL AND item_category_id IS NOT NULL)", name="ck_cost_template_scope"),
        _fk("item_type_id", "item_master", "item_type_definition", nullable=True),
        _fk("item_category_id", "item_master", "item_category", nullable=True),
    ],
    indexes=[("inactive", "is_inactive")],
)

cost_template_line = mk(
    "cost_template_line",
    [
        Column("cost_template_id", UUID_T, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("percentage", MONEY, nullable=True),
        Column("fixed_amount", MONEY, nullable=True),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "cost_template_id", "cost_category_id", name="uq_cost_template_line"),
        CheckConstraint("(percentage IS NOT NULL) <> (fixed_amount IS NOT NULL)", name="ck_cost_template_line_value"),
        CheckConstraint(
            "COALESCE(percentage, 0) >= 0 AND COALESCE(fixed_amount, 0) >= 0", name="ck_cost_template_line_nonneg"
        ),
        CheckConstraint("sort_order >= 0", name="ck_cost_template_line_sort"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_template_id"],
            ["costing.cost_template.tenant_id", "costing.cost_template.id"], name="fk_template_line_template",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"],
            ["costing.cost_category.tenant_id", "costing.cost_category.id"], name="fk_template_line_category",
        ),
    ],
)

cost_variance = mk(
    "cost_variance",
    [
        Column("variance_type", Text, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("inventory_transaction_id", UUID_T, nullable=True),
        Column("inventory_transaction_line_id", UUID_T, nullable=True),
        Column("vendor_bill_line_id", UUID_T, nullable=True),
        Column("work_order_id", UUID_T, nullable=True),
        Column("amount_base", MONEY, nullable=False),
        Column("account_ref", Text, nullable=False),
        Column("fiscal_period_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        CheckConstraint(f"variance_type IN {VARIANCE_TYPES!r}".replace("'", "'"), name="ck_variance_type"),
        CheckConstraint("amount_base <> 0", name="ck_variance_amount"),
        CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_variance_status"),
        CheckConstraint("account_ref <> ''", name="ck_variance_account"),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "fiscal_period_id"], ["platform.fiscal_period.tenant_id", "platform.fiscal_period.id"],
            name="fk_variance_period",
        ),
    ],
    indexes=[("type_period", "variance_type", "fiscal_period_id"), ("item", "item_id"), ("wo", "work_order_id")],
)

cogs_adjustment = mk(
    "cogs_adjustment",
    [
        Column("adjustment_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("reason", Text, nullable=False),
        Column("source_cost_run_id", UUID_T, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("total_value_base", MONEY, nullable=False, server_default=text("0")),
        Column("document_number", Text, nullable=True),
    ],
    extra=[
        CheckConstraint(f"adjustment_type IN {COGS_ADJUSTMENT_TYPES!r}".replace("'", "'"), name="ck_cogs_adjustment_type"),
        CheckConstraint("status IN ('DRAFT','PENDING_APPROVAL','APPROVED','POSTED','REVERSED')", name="ck_cogs_adjustment_status"),
        CheckConstraint("reason <> ''", name="ck_cogs_adjustment_reason"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_cogs_adjustment_posted", postgresql_nulls_not_distinct=True),
    ],
    indexes=[("type_created", "adjustment_type", "created_at")],
)

cogs_adjustment_line = mk(
    "cogs_adjustment_line",
    [
        Column("cogs_adjustment_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("item_cost_id", UUID_T, nullable=False),
        Column("original_transaction_line_id", UUID_T, nullable=True),
        Column("adjusts_period_id", UUID_T, nullable=True),
        Column("value_delta", MONEY, nullable=False),
        Column("estimated", Boolean, nullable=False, server_default=text("false")),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "cogs_adjustment_id", "item_id", "item_cost_id", "original_transaction_line_id",
            name="uq_cogs_line", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("value_delta <> 0", name="ck_cogs_line_delta"),
        ForeignKeyConstraint(
            ["tenant_id", "cogs_adjustment_id"], ["costing.cogs_adjustment.tenant_id", "costing.cogs_adjustment.id"],
            name="fk_cogs_line_adjustment",
        ),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "item_cost_id"], ["costing.item_cost.tenant_id", "costing.item_cost.id"],
            name="fk_cogs_line_item_cost",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "adjusts_period_id"], ["platform.fiscal_period.tenant_id", "platform.fiscal_period.id"],
            name="fk_cogs_line_period",
        ),
    ],
    indexes=[("item", "item_id"), ("period", "adjusts_period_id")],
)

landed_cost = mk(
    "landed_cost",
    [
        Column("vendor_id", UUID_T, nullable=True),
        Column("vendor_bill_id", UUID_T, nullable=True),
        Column("document_number", Text, nullable=True),
        Column("transaction_date", Date, nullable=False),
        Column("allocation_basis", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("total_amount", MONEY, nullable=False, server_default=text("0")),
        Column("currency_code", Text, nullable=False),
        Column("estimated", Boolean, nullable=False, server_default=text("false")),
        Column("receipt_refs", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("fiscal_period_id", UUID_T, nullable=True),
    ],
    extra=[
        CheckConstraint(f"allocation_basis IN {ALLOCATION_BASES!r}".replace("'", "'"), name="ck_landed_basis"),
        CheckConstraint(f"status IN {LANDED_STATUSES!r}".replace("'", "'"), name="ck_landed_status"),
        CheckConstraint("total_amount >= 0", name="ck_landed_total"),
        ForeignKeyConstraint(["currency_code"], ["platform.currency.code"], name="fk_landed_currency"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_landed_posted", postgresql_nulls_not_distinct=True),
    ],
    indexes=[("vendor_date", "vendor_id", "transaction_date"), ("status", "status")],
)

landed_cost_line = mk(
    "landed_cost_line",
    [
        Column("landed_cost_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("cost_category_id", UUID_T, nullable=True),
        Column("landed_cost_item_id", UUID_T, nullable=True),
        Column("description", Text, nullable=False),
        Column("amount", MONEY, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("allocation_basis_override", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "landed_cost_id", "line_number", name="uq_landed_line"),
        CheckConstraint("amount >= 0", name="ck_landed_line_amount"),
        CheckConstraint(
            "allocation_basis_override IS NULL OR allocation_basis_override IN ('WEIGHT','QUANTITY','AMOUNT','PER_LINE')",
            name="ck_landed_line_basis",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "landed_cost_id"], ["costing.landed_cost.tenant_id", "costing.landed_cost.id"],
            name="fk_landed_line_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"], ["costing.cost_category.tenant_id", "costing.cost_category.id"],
            name="fk_landed_line_category",
        ),
    ],
    indexes=[("category", "cost_category_id")],
)

landed_cost_allocation = mk(
    "landed_cost_allocation",
    [
        Column("landed_cost_id", UUID_T, nullable=False),
        Column("landed_cost_line_id", UUID_T, nullable=False),
        Column("item_receipt_line_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("qty_basis", QTY, nullable=False, server_default=text("0")),
        Column("weight_basis", QTY, nullable=False, server_default=text("0")),
        Column("amount_basis", MONEY, nullable=False, server_default=text("0")),
        Column("allocated_amount", MONEY, nullable=False),
        Column("cost_layer_id", UUID_T, nullable=True),
        Column("cost_ledger_entry_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "landed_cost_line_id", "item_receipt_line_id", name="uq_landed_alloc"
        ),
        CheckConstraint("allocated_amount <> 0", name="ck_landed_alloc_amount"),
        ForeignKeyConstraint(
            ["tenant_id", "landed_cost_line_id"], ["costing.landed_cost_line.tenant_id", "costing.landed_cost_line.id"],
            name="fk_landed_alloc_line",
        ),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("receipt", "item_receipt_line_id"), ("item", "item_id")],
)

estimated_landed_cost_template = mk(
    "estimated_landed_cost_template",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("currency_code", Text, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_elc_template_code"),
        ForeignKeyConstraint(["currency_code"], ["platform.currency.code"], name="fk_elc_currency"),
    ],
    indexes=[("active", "is_active")],
)
Index(
    "uq_elc_template_default",
    estimated_landed_cost_template.c.tenant_id,
    unique=True,
    postgresql_where=text("is_default"),
)

estimated_landed_cost_template_line = mk(
    "estimated_landed_cost_template_line",
    [
        Column("template_id", UUID_T, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("allocation_basis", Text, nullable=False),
        Column("default_amount", MONEY, nullable=True),
        Column("default_percent", MONEY, nullable=True),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "template_id", "cost_category_id", name="uq_elc_template_line"),
        CheckConstraint(
            "(default_amount IS NOT NULL) <> (default_percent IS NOT NULL)", name="ck_elc_line_value"
        ),
        CheckConstraint(f"allocation_basis IN {ALLOCATION_BASES!r}".replace("'", "'"), name="ck_elc_line_basis"),
        CheckConstraint(
            "COALESCE(default_amount, 0) >= 0 AND COALESCE(default_percent, 0) >= 0", name="ck_elc_line_nonneg"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "template_id"],
            ["costing.estimated_landed_cost_template.tenant_id", "costing.estimated_landed_cost_template.id"],
            name="fk_elc_line_template",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"], ["costing.cost_category.tenant_id", "costing.cost_category.id"],
            name="fk_elc_line_category",
        ),
    ],
)

location_costing_group = mk(
    "location_costing_group",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("include_in_transit_value", Boolean, nullable=False, server_default=text("false")),
        Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_group_code"),
        CheckConstraint(f"status IN {GROUP_STATUSES!r}".replace("'", "'"), name="ck_group_status"),
        ForeignKeyConstraint(["currency_code"], ["platform.currency.code"], name="fk_group_currency"),
    ],
    indexes=[("active", "is_active")],
)

location_costing_group_member = mk(
    "location_costing_group_member",
    [
        Column("location_costing_group_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "location_id", name="uq_group_member_location"),
        UniqueConstraint("tenant_id", "location_costing_group_id", "location_id", name="uq_group_member"),
        ForeignKeyConstraint(
            ["tenant_id", "location_costing_group_id"],
            ["costing.location_costing_group.tenant_id", "costing.location_costing_group.id"],
            name="fk_group_member_group",
        ),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("group", "location_costing_group_id")],
)

group_average_balance = mk(
    "group_average_balance",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_costing_group_id", UUID_T, nullable=False),
        Column("qty_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("avg_cost", MONEY, nullable=False, server_default=text("0")),
        Column("last_cost_ledger_seq", BigInteger, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'CURRENT'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "location_costing_group_id", name="uq_group_balance"),
        CheckConstraint("qty_on_hand <> 0 OR value_base = 0", name="ck_group_balance_price"),
        CheckConstraint("status IN ('CURRENT','BALANCING','UNDERWATER')", name="ck_group_balance_status"),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "location_costing_group_id"],
            ["costing.location_costing_group.tenant_id", "costing.location_costing_group.id"],
            name="fk_group_balance_group",
        ),
    ],
    indexes=[("group", "location_costing_group_id")],
)
Index(
    "ix_group_balance_noncurrent",
    group_average_balance.c.tenant_id,
    group_average_balance.c.status,
    postgresql_where=text("status <> 'CURRENT'"),
)

# ==============================================================================================
# 0012_costing_standard — standard costing, rollup, revaluation (7 tables)
# ==============================================================================================
cost_version = mk(
    "cost_version",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PLANNED'")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("activated_at", STAMP, nullable=True),
        Column("activated_by", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_cost_version_code"),
        CheckConstraint("status IN ('PLANNED','ACTIVE','SUPERSEDED')", name="ck_cost_version_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_cost_version_dates"),
    ],
    indexes=[("status_effective", "status", "effective_from")],
)

planned_standard_cost = mk(
    "planned_standard_cost",
    [
        Column("cost_version_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False),
        Column("is_approved", Boolean, nullable=False, server_default=text("false")),
        Column("approved_at", STAMP, nullable=True),
        Column("source", Text, nullable=False, server_default=text("'MANUAL'")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "cost_version_id", "item_id", "cost_category_id", name="uq_planned_standard"
        ),
        CheckConstraint("unit_cost >= 0", name="ck_planned_standard_nonneg"),
        CheckConstraint("source IN ('MANUAL','ROLLUP')", name="ck_planned_standard_source"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_version_id"], ["costing.cost_version.tenant_id", "costing.cost_version.id"],
            name="fk_planned_standard_version",
        ),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"], ["costing.cost_category.tenant_id", "costing.cost_category.id"],
            name="fk_planned_standard_category",
        ),
    ],
    indexes=[("item", "item_id")],
)

standard_cost = mk(
    "standard_cost",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("cost_version_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("source", Text, nullable=False, server_default=text("'MANUAL'")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "cost_category_id", "cost_version_id", name="uq_standard_cost"
        ),
        CheckConstraint("unit_cost >= 0", name="ck_standard_cost_nonneg"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_standard_cost_dates"),
        CheckConstraint("source IN ('MANUAL','ROLLUP','IMPORT')", name="ck_standard_cost_source"),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"], ["costing.cost_category.tenant_id", "costing.cost_category.id"],
            name="fk_standard_cost_category",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_version_id"], ["costing.cost_version.tenant_id", "costing.cost_version.id"],
            name="fk_standard_cost_version",
        ),
    ],
    indexes=[("item_effective", "item_id", "effective_from"), ("version", "cost_version_id")],
)

cost_rollup_run = mk(
    "cost_rollup_run",
    [
        Column("cost_version_id", UUID_T, nullable=False),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False, server_default=text("'QUEUED'")),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
        Column("max_level", Integer, nullable=True),
        Column("error", Text, nullable=True),
        Column("material_cost_type", Text, nullable=False, server_default=text("'PER_ITEM_DEFAULT'")),
        Column("include_conversion_costs", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_rollup_status"),
        CheckConstraint(
            "material_cost_type IN ('PER_COSTING_METHOD','PER_PURCHASE_PRICE','PER_ITEM_DEFAULT')",
            name="ck_rollup_material_cost_type",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_version_id"], ["costing.cost_version.tenant_id", "costing.cost_version.id"],
            name="fk_rollup_run_version",
        ),
    ],
    indexes=[("status_started", "status", "started_at")],
)

cost_rollup_line = mk(
    "cost_rollup_line",
    [
        Column("cost_rollup_run_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("bom_level", Integer, nullable=False),
        Column("old_cost", MONEY, nullable=True),
        Column("new_cost", MONEY, nullable=False),
        Column("delta", MONEY, nullable=False, server_default=text("0")),
        Column("breakdown", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "cost_rollup_run_id", "item_id", "cost_category_id", "bom_level",
            name="uq_rollup_line",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_rollup_run_id"],
            ["costing.cost_rollup_run.tenant_id", "costing.cost_rollup_run.id"], name="fk_rollup_line_run",
        ),
        _fk("item_id", "item_master", "item"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_category_id"], ["costing.cost_category.tenant_id", "costing.cost_category.id"],
            name="fk_rollup_line_category",
        ),
    ],
    indexes=[("item", "item_id")],
)

cost_revaluation = mk(
    "cost_revaluation",
    [
        Column("revaluation_type", Text, nullable=False),
        Column("cost_version_id", UUID_T, nullable=True),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("reason", Text, nullable=False),
        Column("document_number", Text, nullable=True),
    ],
    extra=[
        CheckConstraint(
            f"revaluation_type IN {REVALUATION_TYPES!r}".replace("'", "'"), name="ck_revaluation_type"
        ),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','POSTED','REVERSED','VOID')",
            name="ck_revaluation_status",
        ),
        CheckConstraint("reason <> ''", name="ck_revaluation_reason"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_revaluation_posted", postgresql_nulls_not_distinct=True),
        ForeignKeyConstraint(
            ["tenant_id", "cost_version_id"], ["costing.cost_version.tenant_id", "costing.cost_version.id"],
            name="fk_revaluation_version",
        ),
    ],
    indexes=[("status_created", "status", "created_at")],
)

cost_revaluation_line = mk(
    "cost_revaluation_line",
    [
        Column("cost_revaluation_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=True),
        Column("work_order_id", UUID_T, nullable=True),
        Column("old_cost", MONEY, nullable=False),
        Column("new_cost", MONEY, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("value_delta", MONEY, nullable=False),
        Column("inventory_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "cost_revaluation_id", "item_id", "location_id", "work_order_id",
            name="uq_revaluation_line", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("value_delta <> 0", name="ck_revaluation_delta"),
        CheckConstraint("qty > 0", name="ck_revaluation_qty"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_revaluation_id"],
            ["costing.cost_revaluation.tenant_id", "costing.cost_revaluation.id"], name="fk_revaluation_line_doc",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location", nullable=True),
    ],
    indexes=[("item", "item_id"), ("wo", "work_order_id")],
)

REVISION_0011_TABLES: tuple[Table, ...] = (
    cost_category,
    cost_template,
    cost_template_line,
    location_costing_group,
    location_costing_group_member,
    item_cost,
    group_average_balance,
    cost_layer,
    cost_layer_consumption,
    cost_ledger_entry,
    cost_variance,
    cogs_adjustment,
    cogs_adjustment_line,
    landed_cost,
    landed_cost_line,
    landed_cost_allocation,
    estimated_landed_cost_template,
    estimated_landed_cost_template_line,
)

REVISION_0012_TABLES: tuple[Table, ...] = (
    cost_version,
    planned_standard_cost,
    standard_cost,
    cost_rollup_run,
    cost_rollup_line,
    cost_revaluation,
    cost_revaluation_line,
)

ALL_TABLES: tuple[Table, ...] = REVISION_0011_TABLES + REVISION_0012_TABLES
PARTITIONED_TABLES: tuple[Table, ...] = (cost_layer_consumption, cost_ledger_entry)

assert len(ALL_TABLES) == 25
