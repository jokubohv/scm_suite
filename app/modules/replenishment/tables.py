"""SQLAlchemy Core metadata for the 24 ``replenishment`` planning tables (FS-10, DATA-MODEL §13).

Migrations ``0021_planning`` / ``0022_planning_atp`` compile this metadata into explicit PostgreSQL DDL.
All tables are derived/operational planning snapshots: none writes or references ledger/cost tables
(DM-PLAN-01/INV-20); dependency reads go through FS-01/FS-04/FS-06/FS-08/FS-09 contracts.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="replenishment", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

ENGINES = ("MRP", "DRP_BOD")
SUPPLY_TYPES = ("PURCHASE", "WORK_ORDER", "TRANSFER")
LOT_SIZINGS = ("LOT_FOR_LOT", "FIXED_QTY", "MIN_MAX")
DEMAND_METHODS = (
    "LINEAR_REGRESSION", "MOVING_AVERAGE", "SEASONAL_AVERAGE", "SALES_FORECAST",
    "OPEN_DEMAND", "MANUAL",
)
DEMAND_STATUSES = ("DRAFT", "CALCULATED", "APPROVED", "ARCHIVED", "FAILED")
SUPPLY_STATUSES = ("QUEUED", "RUNNING", "CALCULATED", "APPROVED", "FAILED", "CANCELLED")
SUGGESTION_ACTIONS = ("CREATE_PO", "CREATE_WO", "CREATE_TO", "EXPEDITE", "RESCHEDULE", "CANCEL")
SUGGESTION_STATUSES = ("OPEN", "ACCEPTED", "CONVERTED", "DISMISSED", "EXPIRED")
MESSAGE_TYPES = (
    "SHORTAGE", "LATE_RECEIPT", "EXCESS", "EXPEDITE", "DEMAND_CHANGE", "TIME_FENCE",
    "FORECAST_CHANGE", "RULE_MESSAGE",
)
SEVERITIES = ("INFO", "WARN", "CRITICAL")
MESSAGE_STATUSES = ("OPEN", "ACKNOWLEDGED", "RESOLVED", "IGNORED")
FENCE_SCOPES = ("ITEM", "ITEM_CATEGORY", "LOCATION", "GLOBAL")
FENCE_BEHAVIORS = ("FROZEN", "SLUSHED")
DRP_STATUSES = ("DRAFT", "ACTIVE", "INACTIVE")
BOD_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")
ALLOCATION_METHODS = ("PRIORITY", "FAIR_SHARE", "PROPORTIONAL")
ALLOCATION_CRITERIA = (
    "ORDER_PRIORITY", "REQUIRED_BY_DATE", "CUSTOMER_GROUP", "ITEM_CATEGORY", "ORDER_VALUE", "QTY",
)
ALLOCATION_STATUSES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED")
SUPPLY_KINDS = ("ON_HAND", "PO", "WO", "TO")
ALLOCATION_RESULT_STATUSES = ("ALLOCATED", "PARTIAL", "EXCEPTION")
EXCEPTION_TYPES = ("SHORTAGE", "LATE_SUPPLY", "NO_SUPPLY", "INVALID_DEMAND")
EXCEPTION_STATUSES = ("OPEN", "ACKNOWLEDGED", "RESOLVED")
# DATA-MODEL §13 canonical ATP method enum (wins over the spec's descriptive method list).
ATP_METHODS = ("DISCRETE_ATP", "CUMULATIVE_ATP_WITH_LOOK_AHEAD")
AIM_STATUSES = ("RUNNING", "COMPLETED", "FAILED")
AIM_ACTIONS = ("APPLY", "SKIP_NO_CHANGE", "SKIP_MANUAL", "SKIP_DISABLED")


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
        ["tenant_id", local], [f"{ref_schema}.{ref_table}.tenant_id", f"{ref_schema}.{ref_table}.id"],
        name=f"fk_{ref_table}_{local}",
    )


# =====================================================================================
# 0021_planning — config, demand, supply/MRP, DRP, allocation (21 tables)
# =====================================================================================
planning_rule = mk(
    "planning_rule",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("engine", Text, nullable=False),
        Column("allowed_supply_types", ARRAY(Text), nullable=False, server_default=text("'{PURCHASE}'")),
        Column("lot_sizing", Text, nullable=False, server_default=text("'LOT_FOR_LOT'")),
        Column("fixed_order_qty", QTY, nullable=True),
        Column("min_order_qty", QTY, nullable=True),
        Column("max_order_qty", QTY, nullable=True),
        Column("time_fence_days", Integer, nullable=False, server_default=text("0")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("params", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_planning_rule_code"),
        CheckConstraint(f"engine IN {ENGINES!r}".replace("'", "'"), name="ck_planning_rule_engine"),
        CheckConstraint(f"lot_sizing IN {LOT_SIZINGS!r}".replace("'", "'"), name="ck_planning_rule_lot_sizing"),
        CheckConstraint("lot_sizing <> 'FIXED_QTY' OR fixed_order_qty > 0", name="ck_planning_rule_fixed_qty"),
        CheckConstraint("time_fence_days >= 0", name="ck_planning_rule_fence"),
        CheckConstraint("allowed_supply_types <@ ARRAY['PURCHASE','WORK_ORDER','TRANSFER']::text[]", name="ck_planning_rule_supply_types"),
    ],
    indexes=[("active", "is_active")],
)

planning_rule_scope = mk(
    "planning_rule_scope",
    [
        Column("planning_rule_id", UUID_T, nullable=False),
        Column("scope_type", Text, nullable=False),
        Column("scope_id", UUID_T, nullable=True),
        Column("include_descendants", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "planning_rule_id", "scope_type", "scope_id", name="uq_planning_rule_scope"),
        CheckConstraint("scope_type IN ('LOCATION','ITEM','ITEM_CATEGORY','PLANNING_GROUP')", name="ck_planning_rule_scope_type"),
        ForeignKeyConstraint(
            ["tenant_id", "planning_rule_id"], ["replenishment.planning_rule.tenant_id", "replenishment.planning_rule.id"],
            name="fk_planning_rule_scope_rule",
        ),
    ],
    indexes=[("rule", "planning_rule_id")],
)

demand_history = Table(
    "demand_history",
    metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("location_id", UUID_T, nullable=False),
    Column("bucket_date", Date, nullable=False),
    Column("bucket_granularity", Text, nullable=False, server_default=text("'DAY'")),
    Column("qty", QTY, nullable=False),
    Column("source", Text, nullable=False),
    Column("external_ref", Text, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    Column("version", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("id", "bucket_date", name="pk_demand_history"),
    UniqueConstraint(
        "tenant_id", "item_id", "location_id", "bucket_date", "bucket_granularity", "source",
        name="uq_demand_history_bucket",
    ),
    CheckConstraint("bucket_granularity IN ('DAY','WEEK','MONTH')", name="ck_demand_history_granularity"),
    CheckConstraint("source IN ('SALES_ISSUE','FULFILLMENT','ADJUSTMENT','FORECAST','EXTERNAL')", name="ck_demand_history_source"),
    CheckConstraint("qty >= 0", name="ck_demand_history_qty"),
    _tenant_fk("demand_history"),
    _fk("item_id", "item_master", "item"),
    _fk("location_id", "traceability", "location"),
    postgresql_partition_by="RANGE (bucket_date)",
)
Index("ix_demand_history_bucket", demand_history.c.tenant_id, demand_history.c.item_id, demand_history.c.location_id, demand_history.c.bucket_date)
Index("ix_demand_history_brin", demand_history.c.bucket_date, postgresql_using="brin")

time_fence = mk(
    "time_fence",
    [
        Column("scope_type", Text, nullable=False),
        Column("scope_id", UUID_T, nullable=True),
        Column("fence_days", Integer, nullable=False, server_default=text("0")),
        Column("behavior", Text, nullable=False, server_default=text("'SLUSHED'")),
        Column("demand_change_threshold", QTY, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "scope_type", "scope_id", name="uq_time_fence_scope", postgresql_nulls_not_distinct=True),
        CheckConstraint(f"scope_type IN {FENCE_SCOPES!r}".replace("'", "'"), name="ck_time_fence_scope"),
        CheckConstraint(f"behavior IN {FENCE_BEHAVIORS!r}".replace("'", "'"), name="ck_time_fence_behavior"),
        CheckConstraint("fence_days >= 0", name="ck_time_fence_days"),
    ],
    indexes=[("active", "is_active")],
)

demand_plan = mk(
    "demand_plan",
    [
        Column("plan_number", Text, nullable=False),
        Column("method", Text, nullable=False),
        Column("horizon_start", Date, nullable=False),
        Column("horizon_end", Date, nullable=False),
        Column("bucket_granularity", Text, nullable=False, server_default=text("'WEEK'")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("calculated_at", STAMP, nullable=True),
        Column("approved_by", UUID_T, nullable=True),
        Column("approved_at", STAMP, nullable=True),
        Column("planning_rule_id", UUID_T, nullable=True),
        Column("demand_change_threshold", QTY, nullable=True),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("method_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("error_text", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "plan_number", name="uq_demand_plan_number"),
        CheckConstraint(f"method IN {DEMAND_METHODS!r}".replace("'", "'"), name="ck_demand_plan_method"),
        CheckConstraint(f"status IN {DEMAND_STATUSES!r}".replace("'", "'"), name="ck_demand_plan_status"),
        CheckConstraint("horizon_end >= horizon_start", name="ck_demand_plan_dates"),
        CheckConstraint("bucket_granularity IN ('DAY','WEEK','MONTH')", name="ck_demand_plan_granularity"),
        ForeignKeyConstraint(
            ["tenant_id", "planning_rule_id"], ["replenishment.planning_rule.tenant_id", "replenishment.planning_rule.id"],
            name="fk_demand_plan_rule",
        ),
    ],
    indexes=[("status_horizon", "status", "horizon_start")],
)

demand_plan_line = mk(
    "demand_plan_line",
    [
        Column("demand_plan_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bucket_date", Date, nullable=False),
        Column("qty_forecast", QTY, nullable=False, server_default=text("0")),
        Column("qty_manual_override", QTY, nullable=True),
        Column("qty_final", QTY, Computed("COALESCE(qty_manual_override, qty_forecast)", persisted=True), nullable=True),
        Column("method_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("is_overridden", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "demand_plan_id", "item_id", "location_id", "bucket_date", name="uq_demand_plan_line"),
        CheckConstraint("qty_forecast >= 0", name="ck_demand_plan_line_forecast"),
        CheckConstraint("qty_manual_override IS NULL OR qty_manual_override >= 0", name="ck_demand_plan_line_override"),
        ForeignKeyConstraint(
            ["tenant_id", "demand_plan_id"], ["replenishment.demand_plan.tenant_id", "replenishment.demand_plan.id"],
            name="fk_demand_plan_line_plan",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item_bucket", "item_id", "bucket_date")],
)

supply_plan = mk(
    "supply_plan",
    [
        Column("plan_number", Text, nullable=False),
        Column("planning_rule_id", UUID_T, nullable=True),
        Column("horizon_start", Date, nullable=False),
        Column("horizon_end", Date, nullable=False),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False, server_default=text("'QUEUED'")),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
        Column("run_key", Text, nullable=False),
        Column("bucket_granularity", Text, nullable=False, server_default=text("'WEEK'")),
        Column("as_of_date", Date, nullable=False, server_default=text("CURRENT_DATE")),
        Column("input_hash", Text, nullable=True),
        Column("error_text", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "plan_number", name="uq_supply_plan_number"),
        UniqueConstraint("tenant_id", "run_key", name="uq_supply_plan_run_key"),
        CheckConstraint(f"status IN {SUPPLY_STATUSES!r}".replace("'", "'"), name="ck_supply_plan_status"),
        CheckConstraint("horizon_end >= horizon_start", name="ck_supply_plan_dates"),
        ForeignKeyConstraint(
            ["tenant_id", "planning_rule_id"], ["replenishment.planning_rule.tenant_id", "replenishment.planning_rule.id"],
            name="fk_supply_plan_rule",
        ),
    ],
    indexes=[("status_horizon", "status", "horizon_start")],
)

supply_plan_line = mk(
    "supply_plan_line",
    [
        Column("supply_plan_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bucket_date", Date, nullable=False),
        Column("gross_requirement", QTY, nullable=False, server_default=text("0")),
        Column("scheduled_receipts", QTY, nullable=False, server_default=text("0")),
        Column("projected_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("net_requirement", QTY, nullable=False, server_default=text("0")),
        Column("safety_stock", QTY, nullable=False, server_default=text("0")),
        Column("is_shortage", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "supply_plan_id", "item_id", "location_id", "bucket_date", name="uq_supply_plan_line"),
        ForeignKeyConstraint(
            ["tenant_id", "supply_plan_id"], ["replenishment.supply_plan.tenant_id", "replenishment.supply_plan.id"],
            name="fk_supply_plan_line_plan",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item_bucket", "item_id", "bucket_date"), ("shortage", "supply_plan_id", "is_shortage")],
)

supply_plan_suggestion = mk(
    "supply_plan_suggestion",
    [
        Column("supply_plan_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("action_type", Text, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("due_date", Date, nullable=False),
        Column("source_rule_id", UUID_T, nullable=True),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("created_doc_type", Text, nullable=True),
        Column("created_doc_id", UUID_T, nullable=True),
        Column("explanation", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "supply_plan_id", "item_id", "location_id", "action_type", "due_date", name="uq_supply_suggestion_key"),
        CheckConstraint(f"action_type IN {SUGGESTION_ACTIONS!r}".replace("'", "'"), name="ck_suggestion_action"),
        CheckConstraint(f"status IN {SUGGESTION_STATUSES!r}".replace("'", "'"), name="ck_suggestion_status"),
        CheckConstraint("qty > 0", name="ck_suggestion_qty"),
        ForeignKeyConstraint(
            ["tenant_id", "supply_plan_id"], ["replenishment.supply_plan.tenant_id", "replenishment.supply_plan.id"],
            name="fk_suggestion_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_rule_id"], ["replenishment.planning_rule.tenant_id", "replenishment.planning_rule.id"],
            name="fk_suggestion_rule",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("status_due", "status", "due_date"), ("item", "item_id")],
)
Index(
    "uq_supply_suggestion_converted_doc",
    supply_plan_suggestion.c.tenant_id,
    supply_plan_suggestion.c.created_doc_id,
    unique=True,
    postgresql_where=text("created_doc_id IS NOT NULL"),
)

planning_action_message = mk(
    "planning_action_message",
    [
        Column("supply_plan_id", UUID_T, nullable=True),
        Column("supply_plan_suggestion_id", UUID_T, nullable=True),
        Column("message_type", Text, nullable=False),
        Column("severity", Text, nullable=False, server_default=text("'WARN'")),
        Column("item_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=True),
        Column("message", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("context", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        CheckConstraint(f"message_type IN {MESSAGE_TYPES!r}".replace("'", "'"), name="ck_action_message_type"),
        CheckConstraint(f"severity IN {SEVERITIES!r}".replace("'", "'"), name="ck_action_message_severity"),
        CheckConstraint(f"status IN {MESSAGE_STATUSES!r}".replace("'", "'"), name="ck_action_message_status"),
        ForeignKeyConstraint(
            ["tenant_id", "supply_plan_id"], ["replenishment.supply_plan.tenant_id", "replenishment.supply_plan.id"],
            name="fk_action_message_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "supply_plan_suggestion_id"],
            ["replenishment.supply_plan_suggestion.tenant_id", "replenishment.supply_plan_suggestion.id"],
            name="fk_action_message_suggestion",
        ),
    ],
    indexes=[("status_severity", "status", "severity"), ("item", "item_id")],
)

drp_network = mk(
    "drp_network",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_drp_network_code"),
        CheckConstraint(f"status IN {DRP_STATUSES!r}".replace("'", "'"), name="ck_drp_network_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_drp_network_dates"),
    ],
    indexes=[("status", "status")],
)

drp_network_member = mk(
    "drp_network_member",
    [
        Column("drp_network_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("level", Integer, nullable=False, server_default=text("0")),
        Column("parent_member_id", UUID_T, nullable=True),
        Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
        Column("is_source", Boolean, nullable=False, server_default=text("false")),
        Column("priority", Integer, nullable=False, server_default=text("100")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "drp_network_id", "location_id", name="uq_drp_network_member"),
        CheckConstraint("level >= 0", name="ck_drp_member_level"),
        ForeignKeyConstraint(
            ["tenant_id", "drp_network_id"], ["replenishment.drp_network.tenant_id", "replenishment.drp_network.id"],
            name="fk_drp_member_network",
        ),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("location", "location_id"), ("parent", "parent_member_id")],
)

# ``bill_of_distribution`` uses its domain ``version`` as the BOD version number (no separate audit
# ``version`` column): the generic repository increments it exactly like an audit version.
bill_of_distribution = Table(
    "bill_of_distribution",
    metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("drp_network_id", UUID_T, nullable=False),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("created_by", UUID_T, nullable=True),
    Column("updated_by", UUID_T, nullable=True),
    PrimaryKeyConstraint("id", name="pk_bill_of_distribution"),
    UniqueConstraint("tenant_id", "id", name="uq_bill_of_distribution_tenant_id_id"),
    UniqueConstraint("tenant_id", "drp_network_id", "code", "version", name="uq_bod_version"),
    CheckConstraint(f"status IN {BOD_STATUSES!r}".replace("'", "'"), name="ck_bod_status"),
    _tenant_fk("bill_of_distribution"),
    ForeignKeyConstraint(
        ["tenant_id", "drp_network_id"], ["replenishment.drp_network.tenant_id", "replenishment.drp_network.id"],
        name="fk_bod_network",
    ),
)
Index("ix_bill_of_distribution_network", bill_of_distribution.c.tenant_id, bill_of_distribution.c.drp_network_id)
Index("ix_bill_of_distribution_status_effective", bill_of_distribution.c.tenant_id, bill_of_distribution.c.status, bill_of_distribution.c.effective_from)

bill_of_distribution_level = mk(
    "bill_of_distribution_level",
    [
        Column("bill_of_distribution_id", UUID_T, nullable=False),
        Column("level", Integer, nullable=False),
        Column("source_location_id", UUID_T, nullable=False),
        Column("destination_location_id", UUID_T, nullable=False),
        Column("transfer_lead_time_days", Integer, nullable=False, server_default=text("0")),
        Column("is_default_source", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "bill_of_distribution_id", "level", "source_location_id", "destination_location_id",
            name="uq_bod_level_arc",
        ),
        CheckConstraint("source_location_id <> destination_location_id", name="ck_bod_level_distinct"),
        CheckConstraint("level >= 0 AND transfer_lead_time_days >= 0", name="ck_bod_level_numbers"),
        ForeignKeyConstraint(
            ["tenant_id", "bill_of_distribution_id"],
            ["replenishment.bill_of_distribution.tenant_id", "replenishment.bill_of_distribution.id"],
            name="fk_bod_level_bod",
        ),
        _fk("source_location_id", "traceability", "location"),
        _fk("destination_location_id", "traceability", "location"),
    ],
    indexes=[("destination", "destination_location_id")],
)

distribution_category = mk(
    "distribution_category",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_distribution_category_code"),
    ],
    indexes=[("active", "is_active")],
)

distribution_category_member = Table(
    "distribution_category_member",
    metadata,
    Column("tenant_id", UUID_T, nullable=False),
    Column("distribution_category_id", UUID_T, nullable=False),
    Column("item_id", UUID_T, nullable=False),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("tenant_id", "distribution_category_id", "item_id", name="pk_distribution_category_member"),
    _tenant_fk("distribution_category_member"),
    ForeignKeyConstraint(
        ["tenant_id", "distribution_category_id"],
        ["replenishment.distribution_category.tenant_id", "replenishment.distribution_category.id"],
        name="fk_distribution_category_member_category",
    ),
    _fk("item_id", "item_master", "item"),
)
Index("ix_distribution_category_member_item", distribution_category_member.c.tenant_id, distribution_category_member.c.item_id)

allocation_strategy = mk(
    "allocation_strategy",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("method", Text, nullable=False),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("location_scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("include_assembly", Boolean, nullable=False, server_default=text("false")),
        Column("include_kits", Boolean, nullable=False, server_default=text("false")),
        Column("fefo_enabled", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_allocation_strategy_code"),
        CheckConstraint(f"method IN {ALLOCATION_METHODS!r}".replace("'", "'"), name="ck_allocation_strategy_method"),
    ],
    indexes=[("active_priority", "is_active", "priority")],
)

allocation_strategy_rule = mk(
    "allocation_strategy_rule",
    [
        Column("allocation_strategy_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("criterion", Text, nullable=False),
        Column("direction", Text, nullable=False, server_default=text("'ASC'")),
        Column("weight", QTY, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "allocation_strategy_id", "sequence", name="uq_allocation_rule_sequence"),
        CheckConstraint(f"criterion IN {ALLOCATION_CRITERIA!r}".replace("'", "'"), name="ck_allocation_rule_criterion"),
        CheckConstraint("direction IN ('ASC','DESC')", name="ck_allocation_rule_direction"),
        CheckConstraint("weight IS NULL OR weight > 0", name="ck_allocation_rule_weight"),
        ForeignKeyConstraint(
            ["tenant_id", "allocation_strategy_id"],
            ["replenishment.allocation_strategy.tenant_id", "replenishment.allocation_strategy.id"],
            name="fk_allocation_rule_strategy",
        ),
    ],
    indexes=[("strategy", "allocation_strategy_id")],
)

allocation_run = mk(
    "allocation_run",
    [
        Column("run_number", Text, nullable=False),
        Column("allocation_strategy_id", UUID_T, nullable=False),
        Column("as_of_date", Date, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'QUEUED'")),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
        Column("demand_count", Integer, nullable=False, server_default=text("0")),
        Column("supply_count", Integer, nullable=False, server_default=text("0")),
        Column("error_text", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "run_number", name="uq_allocation_run_number"),
        CheckConstraint(f"status IN {ALLOCATION_STATUSES!r}".replace("'", "'"), name="ck_allocation_run_status"),
        ForeignKeyConstraint(
            ["tenant_id", "allocation_strategy_id"],
            ["replenishment.allocation_strategy.tenant_id", "replenishment.allocation_strategy.id"],
            name="fk_allocation_run_strategy",
        ),
    ],
    indexes=[("status_asof", "status", "as_of_date")],
)

allocation_result = mk(
    "allocation_result",
    [
        Column("allocation_run_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("demand_ref", Text, nullable=False),
        Column("demand_line_ref", Text, nullable=False),
        Column("required_by_date", Date, nullable=False),
        Column("supply_type", Text, nullable=False),
        Column("supply_ref", Text, nullable=False),
        Column("qty_allocated", QTY, nullable=False),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("status", Text, nullable=False, server_default=text("'ALLOCATED'")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "allocation_run_id", "demand_ref", "demand_line_ref", "supply_ref",
            name="uq_allocation_result_key",
        ),
        CheckConstraint("qty_allocated > 0", name="ck_allocation_result_qty"),
        CheckConstraint(f"supply_type IN {SUPPLY_KINDS!r}".replace("'", "'"), name="ck_allocation_result_supply_type"),
        CheckConstraint(f"status IN {ALLOCATION_RESULT_STATUSES!r}".replace("'", "'"), name="ck_allocation_result_status"),
        ForeignKeyConstraint(
            ["tenant_id", "allocation_run_id"], ["replenishment.allocation_run.tenant_id", "replenishment.allocation_run.id"],
            name="fk_allocation_result_run",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item_required", "item_id", "required_by_date"), ("demand", "demand_ref")],
)

allocation_exception = mk(
    "allocation_exception",
    [
        Column("allocation_run_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("demand_ref", Text, nullable=False),
        Column("demand_line_ref", Text, nullable=False),
        Column("exception_type", Text, nullable=False),
        Column("shortfall_qty", QTY, nullable=False, server_default=text("0")),
        Column("message", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
    ],
    extra=[
        CheckConstraint(f"exception_type IN {EXCEPTION_TYPES!r}".replace("'", "'"), name="ck_allocation_exception_type"),
        CheckConstraint(f"status IN {EXCEPTION_STATUSES!r}".replace("'", "'"), name="ck_allocation_exception_status"),
        CheckConstraint("shortfall_qty >= 0", name="ck_allocation_exception_shortfall"),
        ForeignKeyConstraint(
            ["tenant_id", "allocation_run_id"], ["replenishment.allocation_run.tenant_id", "replenishment.allocation_run.id"],
            name="fk_allocation_exception_run",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("run_status", "allocation_run_id", "status"), ("demand", "demand_ref")],
)

# =====================================================================================
# 0022_planning_atp — ATP projection + AIM calculations (3 tables)
# =====================================================================================
atp_projection = mk(
    "atp_projection",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bucket_date", Date, nullable=False),
        Column("method", Text, nullable=False),
        Column("policy_version", Text, nullable=False),
        Column("begin_qty", QTY, nullable=False, server_default=text("0")),
        Column("supply_qty", QTY, nullable=False, server_default=text("0")),
        Column("demand_qty", QTY, nullable=False, server_default=text("0")),
        Column("projected_available", QTY, nullable=False, server_default=text("0")),
        Column("earliest_full_date", Date, nullable=True),
        Column("shortage_qty", QTY, nullable=False, server_default=text("0")),
        Column("shortage_date", Date, nullable=True),
        Column("last_rebuilt_at", STAMP, nullable=False, server_default=text("now()")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "location_id", "bucket_date", "method", name="uq_atp_projection_key"
        ),
        CheckConstraint(f"method IN {ATP_METHODS!r}".replace("'", "'"), name="ck_atp_method"),
        CheckConstraint("projected_available >= 0", name="ck_atp_projected_clamped"),
        CheckConstraint("shortage_qty >= 0", name="ck_atp_shortage"),
        CheckConstraint("shortage_date IS NULL OR shortage_qty > 0", name="ck_atp_shortage_date"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item_location_bucket", "item_id", "location_id", "bucket_date")],
)
Index(
    "ix_atp_shortage",
    atp_projection.c.tenant_id,
    atp_projection.c.item_id,
    atp_projection.c.location_id,
    atp_projection.c.shortage_date,
    postgresql_where=text("shortage_qty > 0"),
)

replenishment_calculation = mk(
    "replenishment_calculation",
    [
        Column("run_number", Text, nullable=False),
        Column("calculation_day", Date, nullable=False),
        Column("analysis_interval_days", Integer, nullable=False, server_default=text("30")),
        Column("seasonal_interval_days", Integer, nullable=False, server_default=text("365")),
        Column("formula_version", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'RUNNING'")),
        Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "run_number", name="uq_calculation_number"),
        CheckConstraint(f"status IN {AIM_STATUSES!r}".replace("'", "'"), name="ck_calculation_status"),
        CheckConstraint("formula_version <> ''", name="ck_calculation_formula_version"),
    ],
    indexes=[("status_day", "status", "calculation_day")],
)

replenishment_calculation_line = mk(
    "replenishment_calculation_line",
    [
        Column("replenishment_calculation_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("avg_daily_demand", QTY, nullable=False, server_default=text("0")),
        Column("lead_time_days_used", Integer, nullable=False, server_default=text("0")),
        Column("safety_stock_used", QTY, nullable=False, server_default=text("0")),
        Column("seasonal_factor", QTY, nullable=False, server_default=text("1")),
        Column("reorder_point_calculated", QTY, nullable=False, server_default=text("0")),
        Column("preferred_stock_calculated", QTY, nullable=False, server_default=text("0")),
        Column("qty_to_order", QTY, nullable=False, server_default=text("0")),
        Column("action", Text, nullable=False, server_default=text("'APPLY'")),
        Column("applied", Boolean, nullable=False, server_default=text("false")),
        Column("formula_version", Text, nullable=False),
        Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "replenishment_calculation_id", "item_id", "location_id", name="uq_calculation_line"
        ),
        CheckConstraint(f"action IN {AIM_ACTIONS!r}".replace("'", "'"), name="ck_calculation_line_action"),
        CheckConstraint("avg_daily_demand >= 0 AND safety_stock_used >= 0", name="ck_calculation_line_nonneg"),
        CheckConstraint("reorder_point_calculated >= 0 AND preferred_stock_calculated >= 0", name="ck_calculation_line_qty"),
        ForeignKeyConstraint(
            ["tenant_id", "replenishment_calculation_id"],
            ["replenishment.replenishment_calculation.tenant_id", "replenishment.replenishment_calculation.id"],
            name="fk_calculation_line_run",
        ),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
    ],
    indexes=[("item_location", "item_id", "location_id"), ("action", "action")],
)

REVISION_0021_TABLES: tuple[Table, ...] = (
    planning_rule, planning_rule_scope, demand_history, time_fence, demand_plan, demand_plan_line,
    supply_plan, supply_plan_line, supply_plan_suggestion, planning_action_message, drp_network,
    drp_network_member, bill_of_distribution, bill_of_distribution_level, distribution_category,
    distribution_category_member, allocation_strategy, allocation_strategy_rule, allocation_run,
    allocation_result, allocation_exception,
)

REVISION_0022_TABLES: tuple[Table, ...] = (
    atp_projection, replenishment_calculation, replenishment_calculation_line,
)

ALL_TABLES = REVISION_0021_TABLES + REVISION_0022_TABLES
assert len(REVISION_0021_TABLES) == 21
assert len(ALL_TABLES) == 24
