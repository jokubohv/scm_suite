"""SQLAlchemy Core metadata for the 38 ``wms`` tables (FS-11, DATA-MODEL §11).

Migrations ``0023_wms_setup`` / ``0024_wms_inbound`` / ``0025_wms_execution`` compile this metadata
into explicit PostgreSQL DDL. Services query it with async Core or raw SQL; cross-context references
use composite tenant FKs by table name (``item_master.item``, ``traceability.location/zone/bin/
inventory_status/lot/serial_number``, ``procurement.vendor/purchase_order/item_receipt``) and every
inventory effect goes through the frozen FS-04 ``PostingPort`` (never direct ledger writes).

Execution follows the FS-11 spec: WMS tasks are server-side containers; a task completion calls the
owning contract to post inventory (receipt through ``procurement.p2p``, bin transfer/status change/
fulfilment through ``inventory.ledger``, work-order build through ``manufacturing.execution`` when
present). ``posted_transaction_id`` columns are the audit trail required by ``DM-WMS-01``.
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
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

metadata = MetaData(schema="wms", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)
DATE_T = Date()

# --------------------------------------------------------------------------------------------
# Closed enums (mirrored in CheckConstraints; services validate the same values)
# --------------------------------------------------------------------------------------------
ORDER_DIRECTIONS = ("INBOUND", "OUTBOUND")
SORT_DIRECTIONS = ("FIFO", "LIFO", "FEFO", "BIN_SEQUENCE")
BIN_TYPES = ("STORAGE", "PICKING", "STAGING", "RECEIVING", "DOCK")
TASK_TYPES = ("PUTAWAY", "PICK", "PACK", "REPLENISHMENT", "STATUS_CHANGE")
PICK_TASK_TYPES = ("ORDER", "WORK_ORDER", "VENDOR_RETURN", "TRANSFER")
TASK_STATUSES = ("PENDING", "ASSIGNED", "IN_PROGRESS", "COMPLETED", "EXCEPTION", "CANCELLED", "REVERSED")
PICK_TASK_STATUSES = ("PENDING", "ASSIGNED", "IN_PROGRESS", "PICKED", "SHORT", "PARTIAL", "CANCELLED", "REVERSED")
PICK_LINE_STATUSES = ("PENDING", "PICKED", "PARTIAL", "ZERO", "CANCELLED")
INBOUND_SHIPMENT_STATUSES = ("DRAFT", "IN_TRANSIT", "ARRIVED", "RECEIVING", "RECEIVED", "CANCELLED")
RECEIVING_ORDER_STATUSES = ("CREATED", "IN_PROGRESS", "COMPLETED", "CANCELLED")
WAVE_STATUSES = ("CREATED", "RELEASED", "IN_PROGRESS", "COMPLETED", "CANCELLED")
WAVE_LINE_STATUSES = ("INCLUDED", "RELEASED", "PICKED", "SHIPPED", "CANCELLED")
SOURCE_TYPES = ("SALES_ORDER", "TRANSFER_ORDER", "WORK_ORDER", "VENDOR_RETURN")
PACK_TASK_STATUSES = ("PENDING", "IN_PROGRESS", "PACKED", "CANCELLED")
PACK_CARTON_TYPES = ("CARTON", "PALLET")
PACK_CARTON_STATUSES = ("OPEN", "CLOSED", "SHIPPED")
SHIPMENT_STATUSES = ("DRAFT", "READY", "SHIPPED", "DELIVERED", "CANCELLED")
SHIP_MANIFEST_STATUSES = ("CREATED", "SENT", "CONFIRMED", "ERROR")
REPLENISHMENT_PLAN_STATUSES = ("DRAFT", "APPROVED", "GENERATING", "GENERATED", "CLOSED")
CART_STATUSES = ("OPEN", "IN_USE", "CLOSED")
SYSTEM_RULE_APPLIES_TO = ("ALL", "WAREHOUSE")
ASSIGNMENT_STRATEGIES = ("ROUND_ROBIN", "LEAST_OPEN", "MANUAL")
STRATEGY_APPLIES_TO = ("LOCATION", "WAREHOUSE")

#: Trigger-maintained tables: WMS documents use plain touch triggers (no append-only tables in WMS;
#: task/document evidence is retained and corrected by reversible status transitions / compensating
#: postings, never by deleting posted work).
APPEND_ONLY_TABLES: tuple[str, ...] = ()


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


def mk(name: str, columns: list, *, extra: list | None = None, indexes: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.extend(
        [
            PrimaryKeyConstraint("id", name=f"pk_{name}"),
            UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
            ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant"),
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


def _uq(name: str, *columns: str) -> UniqueConstraint:
    return UniqueConstraint("tenant_id", *columns, name=f"uq_{name}")


def _ck(name: str, expression: str) -> CheckConstraint:
    return CheckConstraint(expression, name=f"ck_{name}")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


# ============================================================================================
# 0023_wms_setup — warehouse/process configuration, strategies, rules, templates
# ============================================================================================
warehouse = mk(
    "warehouse",
    [
        Column("location_id", UUID_T, nullable=False),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_wms_enabled", Boolean, nullable=False, server_default=text("false")),
        Column("receiving_bin_id", UUID_T, nullable=True),
        Column("staging_bin_id", UUID_T, nullable=True),
        Column("shipping_bin_id", UUID_T, nullable=True),
        Column("default_order_type_id", UUID_T, nullable=True),
        Column("timezone", Text, nullable=False, server_default=text("'UTC'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("warehouse_location", "location_id"),
        _uq("warehouse_code", "code"),
        _ck("warehouse_code", "code <> ''"),
        _fk("location_id", "traceability", "location"),
        _fk("receiving_bin_id", "traceability", "bin"),
        _fk("staging_bin_id", "traceability", "bin"),
        _fk("shipping_bin_id", "traceability", "bin"),
    ],
    indexes=[("location", "location_id"), ("enabled", "is_wms_enabled")],
)

item_process_family = mk(
    "item_process_family",
    [Column("code", Text, nullable=False), Column("name", Text, nullable=False),
     Column("is_active", Boolean, nullable=False, server_default=text("true"))],
    extra=[_uq("item_process_family_code", "code"), _ck("item_process_family_code", "code <> ''")],
    indexes=[("active", "is_active")],
)

item_process_group = mk(
    "item_process_group",
    [
        Column("item_process_family_id", UUID_T, nullable=False),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("item_process_group_code", "item_process_family_id", "code"),
        _fk("item_process_family_id", "wms", "item_process_family"),
    ],
    indexes=[("family", "item_process_family_id")],
)

item_process_assignment = mk(
    "item_process_assignment",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("item_process_group_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("item_process_assignment_scope", "item_id", "location_id"),
        _fk("item_id", "item_master", "item"),
        _fk("item_process_group_id", "wms", "item_process_group"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item", "item_id"), ("group", "item_process_group_id")],
)

item_stock_location = mk(
    "item_stock_location",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("min_qty", QTY, nullable=False, server_default=text("0")),
        Column("max_qty", QTY, nullable=False, server_default=text("0")),
        Column("is_primary", Boolean, nullable=False, server_default=text("false")),
        Column("replenishment_source_bin_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("item_stock_location_scope", "item_id", "location_id", "bin_id"),
        _ck("item_stock_location_range", "min_qty >= 0 AND max_qty >= min_qty"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("bin_id", "traceability", "bin"),
        _fk("replenishment_source_bin_id", "traceability", "bin"),
    ],
    indexes=[("item_location", "item_id", "location_id")],
)

order_type = mk(
    "order_type",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("direction", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[_uq("order_type_code", "code"), _ck("order_type_direction", _in("direction", ORDER_DIRECTIONS))],
)

ucc_code_format = mk(
    "ucc_code_format",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("format_pattern", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[_uq("ucc_code_format_code", "code"), _ck("ucc_code_format_pattern", "format_pattern <> ''")],
)

carrier_service_level = mk(
    "carrier_service_level",
    [
        Column("carrier_code", Text, nullable=False),
        Column("service_code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[_uq("carrier_service_level_key", "carrier_code", "service_code")],
    indexes=[("active", "is_active")],
)

pick_strategy = mk(
    "pick_strategy",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("applies_to_location_id", UUID_T, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("pick_strategy_code", "code"),
        _ck("pick_strategy_priority", "priority >= 0"),
        _fk("applies_to_location_id", "traceability", "location"),
    ],
    indexes=[("priority", "priority")],
)

pick_strategy_rule = mk(
    "pick_strategy_rule",
    [
        Column("pick_strategy_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("zone_id", UUID_T, nullable=True),
        Column("item_process_group_id", UUID_T, nullable=True),
        Column("sort_direction", Text, nullable=False, server_default=text("'BIN_SEQUENCE'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("pick_strategy_rule_seq", "pick_strategy_id", "sequence"),
        _ck("pick_strategy_rule_sort", _in("sort_direction", SORT_DIRECTIONS)),
        _fk("pick_strategy_id", "wms", "pick_strategy"),
        _fk("zone_id", "traceability", "zone"),
        _fk("item_process_group_id", "wms", "item_process_group"),
    ],
    indexes=[("strategy", "pick_strategy_id")],
)

putaway_strategy = mk(
    "putaway_strategy",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("applies_to_location_id", UUID_T, nullable=True),
        Column("staging_bin_id", UUID_T, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("putaway_strategy_code", "code"),
        _ck("putaway_strategy_priority", "priority >= 0"),
        _fk("applies_to_location_id", "traceability", "location"),
        _fk("staging_bin_id", "traceability", "bin"),
    ],
    indexes=[("priority", "priority")],
)

putaway_strategy_rule = mk(
    "putaway_strategy_rule",
    [
        Column("putaway_strategy_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("zone_id", UUID_T, nullable=True),
        Column("item_process_group_id", UUID_T, nullable=True),
        Column("bin_type", Text, nullable=True),
        Column("max_qty_per_bin", QTY, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("putaway_strategy_rule_seq", "putaway_strategy_id", "sequence"),
        _ck("putaway_strategy_rule_bin_type", f"bin_type IS NULL OR {_in('bin_type', BIN_TYPES)}"),
        _ck("putaway_strategy_rule_max", "max_qty_per_bin IS NULL OR max_qty_per_bin > 0"),
        _fk("putaway_strategy_id", "wms", "putaway_strategy"),
        _fk("zone_id", "traceability", "zone"),
        _fk("item_process_group_id", "wms", "item_process_group"),
    ],
    indexes=[("strategy", "putaway_strategy_id")],
)

system_rule = mk(
    "system_rule",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("description", Text, nullable=True),
        Column("default_enabled", Boolean, nullable=False, server_default=text("false")),
        Column("applies_to", Text, nullable=False, server_default=text("'ALL'")),
        Column("value_type", Text, nullable=False, server_default=text("'BOOLEAN'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        _uq("system_rule_code", "code"),
        _ck("system_rule_applies_to", _in("applies_to", SYSTEM_RULE_APPLIES_TO)),
        _ck("system_rule_value_type", "value_type IN ('BOOLEAN','STRING','NUMBER','JSON')"),
    ],
    indexes=[("active", "is_active")],
)

system_rule_setting = mk(
    "system_rule_setting",
    [
        Column("system_rule_id", UUID_T, nullable=False),
        Column("warehouse_id", UUID_T, nullable=False),
        Column("is_enabled", Boolean, nullable=False, server_default=text("true")),
        Column("value", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        _uq("system_rule_setting_scope", "system_rule_id", "warehouse_id"),
        _fk("system_rule_id", "wms", "system_rule"),
        _fk("warehouse_id", "wms", "warehouse"),
    ],
    indexes=[("warehouse", "warehouse_id")],
)

bin_block = mk(
    "bin_block",
    [
        Column("bin_id", UUID_T, nullable=False),
        Column("reason_code_id", UUID_T, nullable=True),
        Column("reason", Text, nullable=True),
        Column("blocked_from", STAMP, nullable=False, server_default=text("now()")),
        Column("blocked_to", STAMP, nullable=True),
        Column("released_at", STAMP, nullable=True),
    ],
    extra=[
        _uq("bin_block_scope", "bin_id", "blocked_from"),
        _fk("bin_id", "traceability", "bin"),
    ],
    indexes=[("bin", "bin_id"), ("released", "released_at")],
)

# ============================================================================================
# 0024_wms_inbound — ASN/inbound shipments, receiving orders, putaway tasks
# ============================================================================================
inbound_shipment = mk(
    "inbound_shipment",
    [
        Column("shipment_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=True),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("expected_arrival_date", DATE_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("carrier_code", Text, nullable=True),
        Column("tracking_number", Text, nullable=True),
        Column("origin_country", Text, nullable=True),
        Column("memo", Text, nullable=True),
        Column("received_at", STAMP, nullable=True),
    ],
    extra=[
        _uq("inbound_shipment_number", "shipment_number"),
        _ck("inbound_shipment_status", _in("status", INBOUND_SHIPMENT_STATUSES)),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status", "status"), ("location", "location_id"), ("expected", "expected_arrival_date")],
)

inbound_shipment_line = mk(
    "inbound_shipment_line",
    [
        Column("inbound_shipment_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("qty_expected", QTY, nullable=False),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("unit_cost", MONEY, nullable=True),
    ],
    extra=[
        _uq("inbound_shipment_line_seq", "inbound_shipment_id", "line_number"),
        _ck("inbound_shipment_line_qty", "qty_expected > 0 AND qty_received >= 0 AND qty_received <= qty_expected"),
        _fk("inbound_shipment_id", "wms", "inbound_shipment"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("lot_id", "traceability", "lot"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("shipment", "inbound_shipment_id"), ("item", "item_id")],
)

receiving_order = mk(
    "receiving_order",
    [
        Column("receiving_order_number", Text, nullable=False),
        Column("inbound_shipment_id", UUID_T, nullable=True),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'CREATED'")),
        Column("started_at", STAMP, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("item_receipt_id", UUID_T, nullable=True),
        Column("deferred_posting", Boolean, nullable=False, server_default=text("false")),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        _uq("receiving_order_number", "receiving_order_number"),
        _ck("receiving_order_status", _in("status", RECEIVING_ORDER_STATUSES)),
        _fk("inbound_shipment_id", "wms", "inbound_shipment"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("location_id", "traceability", "location"),
        _fk("item_receipt_id", "procurement", "item_receipt"),
    ],
    indexes=[("status", "status"), ("location", "location_id"), ("receipt", "item_receipt_id")],
)

receiving_order_line = mk(
    "receiving_order_line",
    [
        Column("receiving_order_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("qty_expected", QTY, nullable=False, server_default=text("0")),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("unit_cost", MONEY, nullable=True),
        Column("uom_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("receiving_order_line_seq", "receiving_order_id", "line_number"),
        _ck("receiving_order_line_qty", "qty_expected >= 0 AND qty_received >= 0"),
        _fk("receiving_order_id", "wms", "receiving_order"),
        _fk("item_id", "item_master", "item"),
        _fk("bin_id", "traceability", "bin"),
        _fk("lot_id", "traceability", "lot"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
        _fk("uom_id", "item_master", "uom"),
    ],
    indexes=[("order", "receiving_order_id"), ("item", "item_id")],
)

putaway_task = mk(
    "putaway_task",
    [
        Column("task_number", Text, nullable=False),
        Column("receiving_order_line_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("source_bin_id", UUID_T, nullable=True),
        Column("suggested_bin_id", UUID_T, nullable=True),
        Column("actual_bin_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("strategy_id", UUID_T, nullable=True),
        Column("strategy_rule_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("assigned_user_id", UUID_T, nullable=True),
        Column("is_cart_putaway", Boolean, nullable=False, server_default=text("false")),
        # Deferred cross-revision reference (warehouse_cart is created by 0025); intentionally no FK.
        Column("cart_id", UUID_T, nullable=True),
        Column("exception_reason", Text, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("putaway_task_number", "task_number"),
        _ck("putaway_task_qty", "qty > 0"),
        _ck("putaway_task_status", _in("status", TASK_STATUSES)),
        _fk("receiving_order_line_id", "wms", "receiving_order_line"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("source_bin_id", "traceability", "bin"),
        _fk("suggested_bin_id", "traceability", "bin"),
        _fk("actual_bin_id", "traceability", "bin"),
        _fk("uom_id", "item_master", "uom"),
        _fk("strategy_id", "wms", "putaway_strategy"),
        _fk("strategy_rule_id", "wms", "putaway_strategy_rule"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_putaway_task_transaction"),
    ],
    indexes=[("status", "status"), ("assignee", "assigned_user_id"), ("location", "location_id")],
)

putaway_task_line = mk(
    "putaway_task_line",
    [
        Column("putaway_task_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("bin_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
    ],
    extra=[
        _uq("putaway_task_line_seq", "putaway_task_id", "line_number"),
        _ck("putaway_task_line_qty", "qty > 0"),
        _ck("putaway_task_line_ownership", _in("ownership", ("OWNED", "CONSIGNED", "IN_TRANSIT"))),
        _fk("putaway_task_id", "wms", "putaway_task"),
        _fk("bin_id", "traceability", "bin"),
        _fk("lot_id", "traceability", "lot"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("task", "putaway_task_id"), ("bin", "bin_id")],
)

# ============================================================================================
# 0025_wms_execution — waves/pick/pack/ship, replenishment, status tasks, carts/sessions
# ============================================================================================
wave = mk(
    "wave",
    [
        Column("wave_number", Text, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'CREATED'")),
        Column("released_at", STAMP, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("release_schedule_id", UUID_T, nullable=True),
        Column("order_count", Integer, nullable=False, server_default=text("0")),
        Column("line_count", Integer, nullable=False, server_default=text("0")),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        _uq("wave_number", "wave_number"),
        _ck("wave_status", _in("status", WAVE_STATUSES)),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status", "status"), ("location", "location_id")],
)

wave_line = mk(
    "wave_line",
    [
        Column("wave_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("source_type", Text, nullable=False),
        Column("source_ref", Text, nullable=False),
        Column("source_line_ref", Text, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("status", Text, nullable=False, server_default=text("'INCLUDED'")),
        Column("pick_task_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("wave_line_seq", "wave_id", "line_number"),
        _uq("wave_line_source", "wave_id", "source_type", "source_ref", "source_line_ref"),
        _ck("wave_line_qty", "qty > 0"),
        _ck("wave_line_status", _in("status", WAVE_LINE_STATUSES)),
        _ck("wave_line_source_type", _in("source_type", SOURCE_TYPES)),
        _fk("wave_id", "wms", "wave"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
    ],
    indexes=[("wave", "wave_id"), ("item", "item_id"), ("source", "source_ref")],
)

wave_release_schedule = mk(
    "wave_release_schedule",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("cron_expr", Text, nullable=False),
        Column("filter", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("location_id", UUID_T, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("last_run_at", STAMP, nullable=True),
        Column("next_run_at", STAMP, nullable=True),
    ],
    extra=[
        _uq("wave_release_schedule_code", "code"),
        _ck("wave_release_schedule_cron", "cron_expr <> ''"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("active_next", "is_active", "next_run_at")],
)

pick_task = mk(
    "pick_task",
    [
        Column("task_number", Text, nullable=False),
        Column("task_type", Text, nullable=False),
        Column("wave_id", UUID_T, nullable=True),
        Column("pick_strategy_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("zone_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("assigned_user_id", UUID_T, nullable=True),
        Column("sequence", Integer, nullable=False, server_default=text("0")),
        Column("priority", Integer, nullable=False, server_default=text("0")),
        Column("cart_id", UUID_T, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("pick_task_number", "task_number"),
        _ck("pick_task_type", _in("task_type", PICK_TASK_TYPES)),
        _ck("pick_task_status", _in("status", PICK_TASK_STATUSES)),
        _fk("wave_id", "wms", "wave"),
        _fk("pick_strategy_id", "wms", "pick_strategy"),
        _fk("location_id", "traceability", "location"),
        _fk("zone_id", "traceability", "zone"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_pick_task_transaction"),
    ],
    indexes=[("status", "status"), ("wave", "wave_id"), ("assignee", "assigned_user_id")],
)

pick_task_line = mk(
    "pick_task_line",
    [
        Column("pick_task_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("source_ref", Text, nullable=False),
        Column("source_line_ref", Text, nullable=True),
        Column("qty_requested", QTY, nullable=False),
        Column("qty_picked", QTY, nullable=False, server_default=text("0")),
        Column("qty_short", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("suggested_bin_id", UUID_T, nullable=True),
        Column("actual_bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
    ],
    extra=[
        _uq("pick_task_line_seq", "pick_task_id", "line_number"),
        _ck("pick_task_line_qty",
            "qty_requested > 0 AND qty_picked >= 0 AND qty_short >= 0 AND qty_picked + qty_short = qty_requested"),
        _ck("pick_task_line_status", _in("status", PICK_LINE_STATUSES)),
        _ck("pick_task_line_ownership", _in("ownership", ("OWNED", "CONSIGNED", "IN_TRANSIT"))),
        _fk("pick_task_id", "wms", "pick_task"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("suggested_bin_id", "traceability", "bin"),
        _fk("actual_bin_id", "traceability", "bin"),
        _fk("lot_id", "traceability", "lot"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("task", "pick_task_id"), ("item", "item_id")],
)

pack_task = mk(
    "pack_task",
    [
        Column("task_number", Text, nullable=False),
        Column("pick_task_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("packed_by", UUID_T, nullable=True),
        Column("packed_at", STAMP, nullable=True),
        Column("package_count", Integer, nullable=False, server_default=text("0")),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        _uq("pack_task_number", "task_number"),
        _ck("pack_task_status", _in("status", PACK_TASK_STATUSES)),
        _fk("pick_task_id", "wms", "pick_task"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status", "status")],
)

pack_carton = mk(
    "pack_carton",
    [
        Column("pack_task_id", UUID_T, nullable=False),
        Column("carton_number", Text, nullable=False),
        Column("carton_type", Text, nullable=False, server_default=text("'CARTON'")),
        Column("tracking_number", Text, nullable=True),
        Column("weight", MONEY, nullable=True),
        Column("weight_uom", Text, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
    ],
    extra=[
        _uq("pack_carton_number", "pack_task_id", "carton_number"),
        _ck("pack_carton_type", _in("carton_type", PACK_CARTON_TYPES)),
        _ck("pack_carton_status", _in("status", PACK_CARTON_STATUSES)),
        _ck("pack_carton_weight", "weight IS NULL OR weight >= 0"),
        _fk("pack_task_id", "wms", "pack_task"),
    ],
    indexes=[("task", "pack_task_id")],
)

pack_carton_line = mk(
    "pack_carton_line",
    [
        Column("pack_carton_id", UUID_T, nullable=False),
        Column("pick_task_line_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
    ],
    extra=[
        _uq("pack_carton_line_scope", "pack_carton_id", "pick_task_line_id"),
        _ck("pack_carton_line_qty", "qty > 0"),
        _fk("pack_carton_id", "wms", "pack_carton"),
        _fk("pick_task_line_id", "wms", "pick_task_line"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("carton", "pack_carton_id")],
)

shipment = mk(
    "shipment",
    [
        Column("shipment_number", Text, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("carrier_code", Text, nullable=True),
        Column("carrier_service_level_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("shipped_at", STAMP, nullable=True),
        Column("tracking_number", Text, nullable=True),
        Column("is_manual_ship", Boolean, nullable=False, server_default=text("false")),
        Column("item_fulfillment_ref", Text, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("shipment_number", "shipment_number"),
        _ck("shipment_status", _in("status", SHIPMENT_STATUSES)),
        _fk("location_id", "traceability", "location"),
        _fk("carrier_service_level_id", "wms", "carrier_service_level"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_shipment_transaction"),
    ],
    indexes=[("status", "status"), ("location", "location_id")],
)

shipment_line = mk(
    "shipment_line",
    [
        Column("shipment_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("source_type", Text, nullable=False),
        Column("source_ref", Text, nullable=False),
        Column("source_line_ref", Text, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("warehouse_shipment_status", Text, nullable=False, server_default=text("'SHIPPED'")),
    ],
    extra=[
        _uq("shipment_line_seq", "shipment_id", "line_number"),
        _ck("shipment_line_qty", "qty > 0"),
        _ck("shipment_line_source", _in("source_type", ("SALES_ORDER", "TRANSFER_ORDER"))),
        _ck("shipment_line_status", "warehouse_shipment_status IN ('SHIPPED','PARTIAL','CANCELLED')"),
        _fk("shipment_id", "wms", "shipment"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("shipment", "shipment_id"), ("source", "source_ref")],
)

ship_manifest = mk(
    "ship_manifest",
    [
        Column("manifest_number", Text, nullable=False),
        Column("shipment_id", UUID_T, nullable=False),
        Column("carrier_code", Text, nullable=True),
        Column("manifest_date", DATE_T, nullable=False, server_default=text("CURRENT_DATE")),
        Column("status", Text, nullable=False, server_default=text("'CREATED'")),
        Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("response", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("error_text", Text, nullable=True),
    ],
    extra=[
        _uq("ship_manifest_number", "manifest_number"),
        _ck("ship_manifest_status", _in("status", SHIP_MANIFEST_STATUSES)),
        _fk("shipment_id", "wms", "shipment"),
    ],
    indexes=[("status", "status")],
)

ship_manifest_line = mk(
    "ship_manifest_line",
    [
        Column("ship_manifest_id", UUID_T, nullable=False),
        Column("carton_number", Text, nullable=True),
        Column("tracking_number", Text, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=True),
        Column("weight", MONEY, nullable=True),
    ],
    extra=[
        _uq("ship_manifest_line_tracking", "ship_manifest_id", "carton_number", "tracking_number"),
        _fk("ship_manifest_id", "wms", "ship_manifest"),
    ],
    indexes=[("manifest", "ship_manifest_id")],
)

replenishment_plan = mk(
    "replenishment_plan",
    [
        Column("plan_number", Text, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("plan_date", DATE_T, nullable=False, server_default=text("CURRENT_DATE")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("trigger", Text, nullable=False, server_default=text("'MANUAL'")),
        Column("task_count", Integer, nullable=False, server_default=text("0")),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        _uq("replenishment_plan_number", "plan_number"),
        _ck("replenishment_plan_status", _in("status", REPLENISHMENT_PLAN_STATUSES)),
        _ck("replenishment_plan_trigger", "trigger IN ('MIN_MAX','MANUAL','REPORT')"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status", "status"), ("location", "location_id")],
)

replenishment_task = mk(
    "replenishment_task",
    [
        Column("task_number", Text, nullable=False),
        Column("replenishment_plan_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("from_bin_id", UUID_T, nullable=False),
        Column("to_bin_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("assigned_user_id", UUID_T, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("replenishment_task_number", "task_number"),
        _ck("replenishment_task_qty", "qty > 0"),
        _ck("replenishment_task_bins", "from_bin_id <> to_bin_id"),
        _ck("replenishment_task_status", _in("status", TASK_STATUSES)),
        _fk("replenishment_plan_id", "wms", "replenishment_plan"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("from_bin_id", "traceability", "bin"),
        _fk("to_bin_id", "traceability", "bin"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_replenishment_task_transaction"),
    ],
    indexes=[("status", "status"), ("item", "item_id")],
)

status_change_task = mk(
    "status_change_task",
    [
        Column("task_number", Text, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=False),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("from_status_id", UUID_T, nullable=False),
        Column("to_status_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("assigned_user_id", UUID_T, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        _uq("status_change_task_number", "task_number"),
        _ck("status_change_task_qty", "qty > 0"),
        _ck("status_change_task_statuses", "from_status_id <> to_status_id"),
        _ck("status_change_task_status", _in("status", TASK_STATUSES)),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("bin_id", "traceability", "bin"),
        _fk("from_status_id", "traceability", "inventory_status"),
        _fk("to_status_id", "traceability", "inventory_status"),
        UniqueConstraint("tenant_id", "posted_transaction_id", name="uq_status_change_task_transaction"),
    ],
    indexes=[("status", "status"), ("item", "item_id")],
)

warehouse_cart = mk(
    "warehouse_cart",
    [
        Column("cart_code", Text, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("assigned_user_id", UUID_T, nullable=True),
        Column("opened_at", STAMP, nullable=False, server_default=text("now()")),
        Column("closed_at", STAMP, nullable=True),
    ],
    extra=[
        _uq("warehouse_cart_code", "cart_code"),
        _ck("warehouse_cart_status", _in("status", CART_STATUSES)),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status", "status")],
)

device_session = mk(
    "device_session",
    [
        Column("warehouse_id", UUID_T, nullable=False),
        Column("user_id", UUID_T, nullable=False),
        Column("session_token_hash", Text, nullable=False),
        Column("started_at", STAMP, nullable=False, server_default=text("now()")),
        Column("last_seen_at", STAMP, nullable=False, server_default=text("now()")),
        Column("ended_at", STAMP, nullable=True),
        Column("client_info", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        _uq("device_session_token", "session_token_hash"),
        _ck("device_session_token_hash", "session_token_hash <> ''"),
        _fk("warehouse_id", "wms", "warehouse"),
    ],
    indexes=[("user", "user_id"), ("last_seen", "last_seen_at")],
)

# --------------------------------------------------------------------------------------------
# Revision grouping (DATA-MODEL §21.1 slots M11)
# --------------------------------------------------------------------------------------------
REVISION_0023_TABLES: tuple[Table, ...] = (
    warehouse,
    item_process_family,
    item_process_group,
    item_process_assignment,
    item_stock_location,
    order_type,
    ucc_code_format,
    carrier_service_level,
    pick_strategy,
    pick_strategy_rule,
    putaway_strategy,
    putaway_strategy_rule,
    system_rule,
    system_rule_setting,
    bin_block,
)
REVISION_0024_TABLES: tuple[Table, ...] = (
    inbound_shipment,
    inbound_shipment_line,
    receiving_order,
    receiving_order_line,
    putaway_task,
    putaway_task_line,
)
REVISION_0025_TABLES: tuple[Table, ...] = (
    wave,
    wave_line,
    wave_release_schedule,
    pick_task,
    pick_task_line,
    pack_task,
    pack_carton,
    pack_carton_line,
    shipment,
    shipment_line,
    ship_manifest,
    ship_manifest_line,
    replenishment_plan,
    replenishment_task,
    status_change_task,
    warehouse_cart,
    device_session,
)
ALL_TABLES: tuple[Table, ...] = REVISION_0023_TABLES + REVISION_0024_TABLES + REVISION_0025_TABLES

#: System-rule catalogue seeded by ``0023_wms_setup`` (per tenant).
SYSTEM_RULE_SEEDS: tuple[tuple[str, str, str, bool, str], ...] = (
    ("STAGING_AT_RECEIVING", "Staging at receiving", "Receiving puts stock into the staging bin before putaway", False, "ALL"),
    ("DEFERRED_RECEIPT_POSTING", "Deferred item receipt posting", "Receiving completes into PENDING_POSTING", False, "ALL"),
    ("AUTO_FIRM_COMMIT_WAVE", "Firm commitments when wave releases", "Commit order lines when the wave releases", False, "ALL"),
    ("ALLOW_OVER_PICK_WO", "Allow over-picking work orders", "Permit qty_picked > qty_requested for work-order picks", False, "ALL"),
    ("PICK_EXPIRED_GUARD", "Reject expired lots at pick", "Expired lots are excluded and raise an exception", True, "ALL"),
    ("CART_REQUIRED_PUTAWAY", "Cart required for putaway", "Putaway tasks require a cart container", False, "WAREHOUSE"),
    ("WAVE_AUTO_RELEASE", "Auto-release scheduled waves", "Wave release schedules release created waves immediately", False, "ALL"),
    ("PUTAWAY_REQUIRE_SCAN_CONFIRM", "Require scan confirmation on putaway", "Directed putaway requires an actual-bin confirmation", True, "WAREHOUSE"),
)

#: Tenant settings seeded by ``0023_wms_setup`` under the existing ``inventory`` namespace.
SETTING_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("max_lines_per_wave", "5000", "number"),
    ("pick_expired_guard_default", "true", "boolean"),
    ("deferred_receipt_posting_default", "false", "boolean"),
    ("assignment_timeout_hours", "8", "number"),
    ("abandon_hours", "24", "number"),
    ("ship_manifest_timeout_minutes", "60", "number"),
    ("device_session_retention_days", "30", "number"),
)

COMMON_TASK_STATUSES = TASK_STATUSES

__all__ = [
    "ALL_TABLES",
    "APPEND_ONLY_TABLES",
    "ASSIGNMENT_STRATEGIES",
    "BIN_TYPES",
    "CART_STATUSES",
    "COMMON_TASK_STATUSES",
    "INBOUND_SHIPMENT_STATUSES",
    "ORDER_DIRECTIONS",
    "PACK_CARTON_STATUSES",
    "PACK_CARTON_TYPES",
    "PACK_TASK_STATUSES",
    "PICK_LINE_STATUSES",
    "PICK_TASK_STATUSES",
    "PICK_TASK_TYPES",
    "RECEIVING_ORDER_STATUSES",
    "REPLENISHMENT_PLAN_STATUSES",
    "REVISION_0023_TABLES",
    "REVISION_0024_TABLES",
    "REVISION_0025_TABLES",
    "SETTING_SEEDS",
    "SHIPMENT_STATUSES",
    "SHIP_MANIFEST_STATUSES",
    "SORT_DIRECTIONS",
    "SOURCE_TYPES",
    "SYSTEM_RULE_SEEDS",
    "TASK_STATUSES",
    "TASK_TYPES",
    "WAVE_LINE_STATUSES",
    "WAVE_STATUSES",
    "metadata",
]
