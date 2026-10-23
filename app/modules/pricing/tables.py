"""SQLAlchemy Core metadata for the 17 ``pricing`` tables (FS-02, DATA-MODEL §5).

Migration ``0006_pricing`` compiles this metadata into explicit PostgreSQL DDL; module repositories
query these tables with async SQLAlchemy Core. No ORM imports and no cross-module ORM coupling: the
item-master read surface goes through :class:`app.modules.pricing.item_port.ItemMasterPort`.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
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

metadata = MetaData(schema="pricing", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
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


def mk(name: str, columns: list, *, extra: list | None = None, pk: list[str] | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False)]
    cols.append(Column("tenant_id", UUID_T, nullable=False))
    cols.extend(columns)
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.append(_tenant_fk(name))
    constraints.append(UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"))
    if pk is None:
        constraints.append(PrimaryKeyConstraint("id", name=f"pk_{name}"))
    else:
        constraints.append(PrimaryKeyConstraint("tenant_id", *pk, name=f"pk_{name}"))
    return Table(name, metadata, *cols, *constraints)


price_level = mk(
    "price_level",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_price_level_code"),
        CheckConstraint("sort_order >= 0", name="ck_price_level_sort"),
    ],
)

item_group = mk(
    "item_group",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[UniqueConstraint("tenant_id", "code", name="uq_item_group_code")],
)

item_group_member = mk(
    "item_group_member",
    [
        Column("item_group_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
    ],
    extra=[
        ForeignKeyConstraint(
            ["tenant_id", "item_group_id"],
            ["pricing.item_group.tenant_id", "pricing.item_group.id"],
            name="fk_item_group_member_group",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["item_master.item.tenant_id", "item_master.item.id"],
            name="fk_item_group_member_item",
        ),
    ],
    pk=["item_group_id", "item_id"],
)

customer_group = mk(
    "customer_group",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
        Column("notes", Text, nullable=True),
    ],
    extra=[UniqueConstraint("tenant_id", "code", name="uq_customer_group_code")],
)

customer_group_member = mk(
    "customer_group_member",
    [
        Column("customer_group_id", UUID_T, nullable=False),
        Column("external_customer_ref", Text, nullable=False),
    ],
    extra=[
        ForeignKeyConstraint(
            ["tenant_id", "customer_group_id"],
            ["pricing.customer_group.tenant_id", "pricing.customer_group.id"],
            name="fk_customer_group_member_group",
        ),
        CheckConstraint("length(external_customer_ref) > 0", name="ck_customer_group_member_ref"),
    ],
    pk=["customer_group_id", "external_customer_ref"],
)

pricing_group = mk(
    "pricing_group",
    [
        Column("item_group_id", UUID_T, nullable=False),
        Column("customer_group_id", UUID_T, nullable=False),
        Column("price_level_id", UUID_T, nullable=False),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("discount_percent", MONEY, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_group_id", "customer_group_id", name="uq_pricing_group_pair"),
        ForeignKeyConstraint(
            ["tenant_id", "item_group_id"],
            ["pricing.item_group.tenant_id", "pricing.item_group.id"],
            name="fk_pricing_group_item_group",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_group_id"],
            ["pricing.customer_group.tenant_id", "pricing.customer_group.id"],
            name="fk_pricing_group_customer_group",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "price_level_id"],
            ["pricing.price_level.tenant_id", "pricing.price_level.id"],
            name="fk_pricing_group_price_level",
        ),
        CheckConstraint(
            "discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)",
            name="ck_pricing_group_discount",
        ),
    ],
)

item_price = mk(
    "item_price",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("price_level_id", UUID_T, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("qty_min", QTY, nullable=False, server_default=text("0")),
        Column("qty_max", QTY, nullable=True),
        Column("unit_price", MONEY, nullable=False),
        Column("cost_plus_basis", Text, nullable=True),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "price_level_id", "currency_code", "qty_min", name="uq_item_price_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_item_price_item"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "price_level_id"],
            ["pricing.price_level.tenant_id", "pricing.price_level.id"],
            name="fk_item_price_price_level",
        ),
        CheckConstraint("unit_price >= 0", name="ck_item_price_nonneg"),
        CheckConstraint("qty_min >= 0", name="ck_item_price_qty_min"),
        CheckConstraint("qty_max IS NULL OR qty_max > qty_min", name="ck_item_price_qty_band"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_item_price_effective"),
        CheckConstraint(
            "cost_plus_basis IS NULL OR cost_plus_basis IN ('AVERAGE','LAST_PURCHASE','STANDARD')",
            name="ck_item_price_cost_basis",
        ),
    ],
)

price_schedule = mk(
    "price_schedule",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("calculation_mode", Text, nullable=False),
        Column("is_marginal", Boolean, nullable=False, server_default=text("false")),
        Column("currency_code", Text, nullable=False),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_price_schedule_code"),
        CheckConstraint(
            "calculation_mode IN ('LINE','OVERALL_ITEM','PARENT','SCHEDULE')", name="ck_price_schedule_mode"
        ),
    ],
)

price_schedule_break = mk(
    "price_schedule_break",
    [
        Column("price_schedule_id", UUID_T, nullable=False),
        Column("qty_from", QTY, nullable=False),
        Column("qty_to", QTY, nullable=True),
        Column("pricing_method", Text, nullable=False),
        Column("value", MONEY, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "price_schedule_id", "qty_from", name="uq_price_schedule_break_from"),
        ForeignKeyConstraint(
            ["tenant_id", "price_schedule_id"],
            ["pricing.price_schedule.tenant_id", "pricing.price_schedule.id"],
            name="fk_price_schedule_break_schedule",
        ),
        CheckConstraint("qty_from >= 0", name="ck_price_schedule_break_from"),
        CheckConstraint("qty_to IS NULL OR qty_to > qty_from", name="ck_price_schedule_break_band"),
        CheckConstraint(
            "pricing_method IN ('DISCOUNT_PERCENT','DISCOUNT_AMOUNT','FIXED_PRICE')",
            name="ck_price_schedule_break_method",
        ),
        CheckConstraint("value >= 0", name="ck_price_schedule_break_value"),
    ],
)

price_rule = mk(
    "price_rule",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("rule_type", Text, nullable=False),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("start_date", Date, nullable=True),
        Column("end_date", Date, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("conflict_policy", Text, nullable=False, server_default=text("'HIGHER_PRIORITY'")),
        Column("currency_code", Text, nullable=True),
        Column("generated_from_update_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_price_rule_code"),
        CheckConstraint("rule_type IN ('PRICE_LEVEL','DISCOUNT','MARKUP','FIXED_PRICE')", name="ck_price_rule_type"),
        CheckConstraint("priority >= 0", name="ck_price_rule_priority"),
        CheckConstraint("conflict_policy IN ('HIGHER_PRIORITY','FIRST_MATCH','STOP')", name="ck_price_rule_conflict"),
        CheckConstraint("start_date IS NULL OR end_date IS NULL OR start_date <= end_date", name="ck_price_rule_dates"),
    ],
)

price_rule_criteria = mk(
    "price_rule_criteria",
    [
        Column("price_rule_id", UUID_T, nullable=False),
        Column("criterion", Text, nullable=False),
        Column("operator", Text, nullable=False),
        Column("value_text", Text, nullable=True),
        Column("value_number", MONEY, nullable=True),
        Column("value_date", Date, nullable=True),
        Column("group_no", Integer, nullable=False, server_default=text("1")),
    ],
    extra=[
        ForeignKeyConstraint(
            ["tenant_id", "price_rule_id"],
            ["pricing.price_rule.tenant_id", "pricing.price_rule.id"],
            name="fk_price_rule_criteria_rule",
        ),
        CheckConstraint(
            "criterion IN ('CUSTOMER','CUSTOMER_GROUP','ITEM','ITEM_COLLECTION','ITEM_CATEGORY','QTY','DATE','CURRENCY')",
            name="ck_price_rule_criteria_criterion",
        ),
        CheckConstraint("operator IN ('EQ','NE','IN','BETWEEN','GTE','LTE')", name="ck_price_rule_criteria_operator"),
        CheckConstraint("group_no >= 1", name="ck_price_rule_criteria_group"),
    ],
)

price_rule_action = mk(
    "price_rule_action",
    [
        Column("price_rule_id", UUID_T, nullable=False),
        Column("action_type", Text, nullable=False),
        Column("price_level_id", UUID_T, nullable=True),
        Column("value", MONEY, nullable=True),
        Column("cost_basis", Text, nullable=True),
        Column("apply_to", Text, nullable=False, server_default=text("'UNIT'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "price_rule_id", name="uq_price_rule_action_rule"),
        ForeignKeyConstraint(
            ["tenant_id", "price_rule_id"],
            ["pricing.price_rule.tenant_id", "pricing.price_rule.id"],
            name="fk_price_rule_action_rule",
        ),
        CheckConstraint(
            "action_type IN ('SET_PRICE_LEVEL','PERCENT_DISCOUNT','AMOUNT_DISCOUNT','PERCENT_MARKUP','AMOUNT_MARKUP','FIXED_PRICE','COST_PLUS')",
            name="ck_price_rule_action_type",
        ),
        CheckConstraint(
            "cost_basis IS NULL OR cost_basis IN ('AVERAGE','LAST_PURCHASE','STANDARD')",
            name="ck_price_rule_action_cost_basis",
        ),
        CheckConstraint("apply_to IN ('UNIT','LINE','TOTAL')", name="ck_price_rule_action_apply_to"),
        CheckConstraint("action_type <> 'COST_PLUS' OR cost_basis IS NOT NULL", name="ck_price_rule_action_cost_plus"),
        CheckConstraint(
            "action_type NOT LIKE 'PERCENT%' OR (value IS NOT NULL AND value >= 0 AND value <= 100)",
            name="ck_price_rule_action_percent",
        ),
    ],
)

item_collection = mk(
    "item_collection",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("criteria", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("is_dynamic", Boolean, nullable=False, server_default=text("false")),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[UniqueConstraint("tenant_id", "code", name="uq_item_collection_code")],
)

item_collection_member = mk(
    "item_collection_member",
    [
        Column("item_collection_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
    ],
    extra=[
        ForeignKeyConstraint(
            ["tenant_id", "item_collection_id"],
            ["pricing.item_collection.tenant_id", "pricing.item_collection.id"],
            name="fk_item_collection_member_collection",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["item_master.item.tenant_id", "item_master.item.id"],
            name="fk_item_collection_member_item",
        ),
    ],
    pk=["item_collection_id", "item_id"],
)

coupon = mk(
    "coupon",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("coupon_type", Text, nullable=False),
        Column("value", MONEY, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("start_date", Date, nullable=False),
        Column("end_date", Date, nullable=False),
        Column("max_uses", Integer, nullable=True),
        Column("uses_count", Integer, nullable=False, server_default=text("0")),
        Column("item_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_coupon_code"),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["item_master.item.tenant_id", "item_master.item.id"], name="fk_coupon_item"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_category_id"],
            ["item_master.item_category.tenant_id", "item_master.item_category.id"],
            name="fk_coupon_item_category",
        ),
        CheckConstraint("coupon_type IN ('PERCENT','AMOUNT')", name="ck_coupon_type"),
        CheckConstraint("value > 0", name="ck_coupon_value"),
        CheckConstraint("coupon_type <> 'PERCENT' OR value <= 100", name="ck_coupon_percent"),
        CheckConstraint("start_date <= end_date", name="ck_coupon_dates"),
        CheckConstraint("max_uses IS NULL OR max_uses > 0", name="ck_coupon_max_uses"),
        CheckConstraint("max_uses IS NULL OR uses_count <= max_uses", name="ck_coupon_uses"),
        CheckConstraint("item_id IS NULL OR item_category_id IS NULL", name="ck_coupon_single_scope"),
    ],
)

price_update = mk(
    "price_update",
    [
        Column("update_type", Text, nullable=False),
        Column("target_ref", Text, nullable=False),
        Column("price_level_id", UUID_T, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("effective_start", Date, nullable=False),
        Column("effective_end", Date, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("applied_at", STAMP, nullable=True),
        Column("scheduled_by", UUID_T, nullable=True),
        Column("overlap_policy", Text, nullable=False, server_default=text("'REJECT'")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "update_type", "target_ref", "price_level_id", "effective_start", name="uq_price_update_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "price_level_id"],
            ["pricing.price_level.tenant_id", "pricing.price_level.id"],
            name="fk_price_update_price_level",
        ),
        CheckConstraint(
            "update_type IN ('ITEM','ITEM_CATEGORY','CUSTOMER','CUSTOMER_GROUP')", name="ck_price_update_type"
        ),
        CheckConstraint(
            "status IN ('PENDING','APPLIED','REVERTED','CANCELLED','SUPERSEDED')", name="ck_price_update_status"
        ),
        CheckConstraint("effective_end IS NULL OR effective_end > effective_start", name="ck_price_update_effective"),
        CheckConstraint("priority >= 0", name="ck_price_update_priority"),
    ],
)

price_update_detail = mk(
    "price_update_detail",
    [
        Column("price_update_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("external_customer_ref", Text, nullable=True),
        Column("customer_group_id", UUID_T, nullable=True),
        Column("new_price", MONEY, nullable=False),
        Column("qty_min", QTY, nullable=False, server_default=text("0")),
        Column("qty_max", QTY, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(
            ["tenant_id", "price_update_id"],
            ["pricing.price_update.tenant_id", "pricing.price_update.id"],
            name="fk_price_update_detail_update",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["item_master.item.tenant_id", "item_master.item.id"],
            name="fk_price_update_detail_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_category_id"],
            ["item_master.item_category.tenant_id", "item_master.item_category.id"],
            name="fk_price_update_detail_category",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_group_id"],
            ["pricing.customer_group.tenant_id", "pricing.customer_group.id"],
            name="fk_price_update_detail_customer_group",
        ),
        CheckConstraint("new_price >= 0", name="ck_price_update_detail_price"),
        CheckConstraint("qty_min >= 0", name="ck_price_update_detail_qty_min"),
        CheckConstraint("qty_max IS NULL OR qty_max > qty_min", name="ck_price_update_detail_band"),
        CheckConstraint(
            "(item_id IS NOT NULL)::int + (item_category_id IS NOT NULL)::int + "
            "(external_customer_ref IS NOT NULL)::int + (customer_group_id IS NOT NULL)::int = 1",
            name="ck_price_update_detail_one_target",
        ),
    ],
)

REVISION_0006_TABLES: tuple[Table, ...] = (
    price_level,
    item_group,
    item_group_member,
    customer_group,
    customer_group_member,
    pricing_group,
    item_price,
    price_schedule,
    price_schedule_break,
    price_rule,
    price_rule_criteria,
    price_rule_action,
    item_collection,
    item_collection_member,
    coupon,
    price_update,
    price_update_detail,
)

__all__ = [table.name for table in REVISION_0006_TABLES] + ["REVISION_0006_TABLES", "metadata"]
