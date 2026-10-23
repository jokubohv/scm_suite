"""SQLAlchemy Core metadata for the 42 ``manufacturing`` tables (FS-09 + Δ-CFG/Δ-REUSE).

Migrations ``0018_manufacturing_master`` / ``0019_manufacturing_routing`` / ``0020_manufacturing_execution``
compile this metadata into explicit PostgreSQL DDL. Module services query it with async SQL Core; the
compatibility engine and unbuild dispositions follow the Lead-approved addenda (`design/CONFIG-REUSE-ADDENDUM.md`,
EXT/ASSUMPTION). No ORM imports: cross-context references use composite tenant FKs by table name and all
stock/value effects go through the frozen FS-04/FS-05 contracts, never direct writes.
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="manufacturing", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

# ---------------------------------------------------------------- enumerations (DATA-MODEL §12) --
ASSEMBLY_TYPES = ("STANDARD", "WIP", "PHANTOM", "MATRIX_PARENT", "OUTSOURCED")
REVISION_CONTROL = ("NONE", "EFFECTIVE_DATE", "REVISION")
BOM_STATUSES = ("DRAFT", "ACTIVE", "OBSOLETE")
REVISION_STATUSES = ("DRAFT", "ACTIVE", "OBSOLETE")
ASSEMBLY_REVISION_STATUSES = ("ACTIVE", "OBSOLETE")
CONTROL_TYPES = ("REVISION", "EFFECTIVE_DATE")
ROUTING_STATUSES = ("DRAFT", "ACTIVE", "OBSOLETE")
WORK_CENTER_CALENDAR = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
WO_TYPES = ("STANDARD", "WIP", "SPECIAL_ORDER", "OUTSOURCED")
WO_STATUSES = (
    "PLANNED",
    "FIRM_PLANNED",
    "RELEASED",
    "IN_PROCESS",
    "BUILT",
    "CLOSED",
    "CANCELLED",
    "VOID",
)
WO_LINE_STATUSES = ("PLANNED", "RELEASED", "IN_PROCESS", "BUILT", "CLOSED", "CANCELLED")
OPERATION_STATUSES = ("PENDING", "IN_PROGRESS", "COMPLETED", "CANCELLED")
WIP_TRANSACTION_TYPES = ("ISSUE", "COMPLETION", "CLOSE", "REVERSAL")
WIP_STATUSES = ("DRAFT", "POSTED", "REVERSED", "VOID")
BUILD_STATUSES = ("DRAFT", "POSTED", "REVERSED", "VOID")
UNBUILD_DISPOSITIONS = ("RETURN_TO_STOCK", "SCRAP", "HOLD")
UNBUILD_VALUE_POLICIES = ("ORIGINAL_COST", "NRV", "MANUAL")
OUTSOURCED_STATUSES = ("PLANNED", "ORDERED", "IN_PROCESS", "RECEIVED", "CLOSED", "CANCELLED")
OUTSOURCED_CHARGE_STATUSES = ("ESTIMATED", "ACTUAL")
ECO_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "IMPLEMENTED", "CANCELLED")
ECO_CHANGE_TYPES = (
    "ADD_COMPONENT",
    "REMOVE_COMPONENT",
    "CHANGE_QTY",
    "CHANGE_REVISION",
    "CHANGE_EFFECTIVITY",
    "CHANGE_ROUTING",
)
ECO_LINE_STATUSES = ("PROPOSED", "APPROVED", "REJECTED", "APPLIED")
SCHEDULING_METHODS = ("FORWARD", "BACKWARD")
CAPACITY_MODES = ("INFINITE", "FINITE")
SCHEDULING_RUN_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
CAPACITY_STATUSES = ("OK", "OVERLOADED", "UNDERLOADED")
WIP_BALANCE_STATUSES = ("OPEN", "CLOSED", "BALANCING")
COMPAT_DATA_TYPES = ("TEXT", "NUMERIC", "BOOL", "SELECT")
COMPAT_SCOPE_TYPES = ("ASSEMBLY", "BOM_REVISION", "CONFIGURATION", "ORDER", "CUSTOMER", "REGION", "CHANNEL")
COMPAT_RULE_SET_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")
COMPAT_ENFORCEMENT = ("INHERIT", "HARD", "WARN")
COMPAT_RULE_TYPES = (
    "REQUIRES",
    "EXCLUDES",
    "ONE_OF",
    "MIN_QTY",
    "MAX_QTY",
    "CAPACITY",
    "ATTR_EQ",
    "ATTR_RANGE",
    "SUBSET_OF",
)
COMPAT_SUBJECT_TYPES = ("ITEM", "ATTRIBUTE", "COMPONENT_GROUP", "ITEM_SET")
COMPAT_RESULTS = ("VALID", "WARN", "HARD_FAIL", "OVERRIDDEN")
COMPAT_CONTEXTS = ("BOM", "WO", "BUILD", "OUTSOURCED", "ORDER", "ECO", "ATB")
COMPAT_SLOT_TYPES = ("CPU_SOCKET", "PCIe", "DIMM", "DRIVE", "PSU", "CASE", "OTHER")
PROVENANCES = ("MANUAL", "IMPORT", "EXT")

#: Append-only tables (reject UPDATE/DELETE via trigger in the migration).
APPEND_ONLY_TABLES: tuple[str, ...] = ("compat_configuration",)


# --------------------------------------------------------------------------- helpers -----------
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


def mk(
    name: str,
    columns: list,
    *,
    extra: list | None = None,
    indexes: list | None = None,
    pk_columns: tuple[str, ...] = ("id",),
) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = list(extra or [])
    constraints.extend(
        [
            PrimaryKeyConstraint(*pk_columns, name=f"pk_{name}"),
            UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
            ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant"),
        ]
    )
    table = Table(name, metadata, *cols, *constraints)
    for index in indexes or []:
        Index(f"ix_{name}_{index[0]}", table.c.tenant_id, *(table.c[column] for column in index[1:]))
    return table


def _fk(local: str, ref_schema: str, ref_table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", local],
        [f"{ref_schema}.{ref_table}.tenant_id", f"{ref_schema}.{ref_table}.id"],
        name=f"fk_{ref_table}_{local}",
    )


def _doc(columns: list, *, status: str | None = None) -> list:
    """Document standard columns (DATA-MODEL §1.3) used by posting-workflow documents."""
    return [
        Column("document_number", Text, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("status", Text, nullable=False, server_default=text(f"'{status or 'DRAFT'}'")),
        *columns,
    ]


# =================================================================================================
# 0018_manufacturing_master — assembly items, BOM/revisions/components/control (6 tables)
# =================================================================================================
assembly_item = mk(
    "assembly_item",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("assembly_type", Text, nullable=False, server_default=text("'STANDARD'")),
        Column("default_bom_id", UUID_T, nullable=True),
        Column("default_routing_id", UUID_T, nullable=True),
        Column("unbuild_variance_account_ref", Text, nullable=True),
        Column("wip_account_ref", Text, nullable=True),
        Column("build_cost", MONEY, nullable=False, server_default=text("0")),
        Column("revision_control", Text, nullable=False, server_default=text("'NONE'")),
        Column("backflush_default", Boolean, nullable=False, server_default=text("false")),
        Column("component_yield_percent", QTY, nullable=True),
        Column("round_up_component_qty", Boolean, nullable=False, server_default=text("false")),
        Column("is_outsourced", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", name="uq_assembly_item_item"),
        CheckConstraint(f"assembly_type IN {ASSEMBLY_TYPES!r}", name="ck_assembly_item_type"),
        CheckConstraint(f"revision_control IN {REVISION_CONTROL!r}", name="ck_assembly_item_revision_control"),
        CheckConstraint("build_cost >= 0", name="ck_assembly_item_build_cost"),
        CheckConstraint(
            "component_yield_percent IS NULL OR (component_yield_percent > 0 AND component_yield_percent <= 100)",
            name="ck_assembly_item_yield",
        ),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("type", "assembly_type")],
)

bom = mk(
    "bom",
    [
        Column("assembly_item_id", UUID_T, nullable=False),
        Column("bom_code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("effective_from", Date, nullable=True),
        Column("effective_to", Date, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "assembly_item_id", "bom_code", name="uq_bom_code"),
        CheckConstraint(f"status IN {BOM_STATUSES!r}", name="ck_bom_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from", name="ck_bom_dates"
        ),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
    ],
    indexes=[("status", "status"), ("assembly", "assembly_item_id")],
)
Index(
    "uq_bom_default",
    bom.c.tenant_id,
    bom.c.assembly_item_id,
    unique=True,
    postgresql_where=text("is_default"),
)

bom_revision = mk(
    "bom_revision",
    [
        Column("bom_id", UUID_T, nullable=False),
        Column("revision_code", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("approved_at", STAMP, nullable=True),
        Column("approved_by", UUID_T, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "bom_id", "revision_code", name="uq_bom_revision_code"),
        CheckConstraint(f"status IN {REVISION_STATUSES!r}", name="ck_bom_revision_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_bom_revision_dates"),
        _fk("bom_id", "manufacturing", "bom"),
    ],
    indexes=[("bom_status", "bom_id", "status")],
)

bom_component = mk(
    "bom_component",
    [
        Column("bom_revision_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("component_item_id", UUID_T, nullable=False),
        Column("qty_per", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=False),
        Column("component_yield_percent", QTY, nullable=True),
        Column("round_up_component_qty", Boolean, nullable=False, server_default=text("false")),
        Column("is_phantom", Boolean, nullable=False, server_default=text("false")),
        Column("operation_sequence", Integer, nullable=True),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "bom_revision_id", "line_number", name="uq_bom_component_line"),
        CheckConstraint("qty_per > 0", name="ck_bom_component_qty"),
        CheckConstraint("line_number > 0", name="ck_bom_component_line_number"),
        CheckConstraint(
            "component_yield_percent IS NULL OR (component_yield_percent > 0 AND component_yield_percent <= 100)",
            name="ck_bom_component_yield",
        ),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
        _fk("component_item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
    ],
    indexes=[("component", "component_item_id")],
)

bom_component_control = mk(
    "bom_component_control",
    [
        Column("bom_component_id", UUID_T, nullable=False),
        Column("control_type", Text, nullable=False),
        Column("revision_code", Text, nullable=True),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("obsolete_date", Date, nullable=True),
        Column("is_included", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "bom_component_id", "control_type", "effective_from", name="uq_bom_component_control_scope"
        ),
        CheckConstraint(f"control_type IN {CONTROL_TYPES!r}", name="ck_bom_component_control_type"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="ck_bom_component_control_dates"
        ),
        _fk("bom_component_id", "manufacturing", "bom_component"),
    ],
    indexes=[("effective_from", "effective_from")],
)

assembly_revision = mk(
    "assembly_revision",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("revision_code", Text, nullable=False),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "revision_code", name="uq_assembly_revision_code"),
        CheckConstraint(f"status IN {ASSEMBLY_REVISION_STATUSES!r}", name="ck_assembly_revision_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_assembly_revision_dates"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("item_status", "item_id", "status")],
)

# =================================================================================================
# 0019_manufacturing_routing — routings, work centres, calendars, cost templates (9 tables)
# =================================================================================================
routing = mk(
    "routing",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("routing_code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("is_default", Boolean, nullable=False, server_default=text("false")),
        Column("effective_from", Date, nullable=True),
        Column("effective_to", Date, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "routing_code", name="uq_routing_code"),
        CheckConstraint(f"status IN {ROUTING_STATUSES!r}", name="ck_routing_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from", name="ck_routing_dates"
        ),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("item_status", "item_id", "status")],
)
Index(
    "uq_routing_default",
    routing.c.tenant_id,
    routing.c.item_id,
    unique=True,
    postgresql_where=text("is_default"),
)

work_center_group = mk(
    "work_center_group",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_work_center_group_code"),
        CheckConstraint("code <> ''", name="ck_work_center_group_code"),
    ],
    indexes=[("active", "is_active")],
)

work_center_calendar = mk(
    "work_center_calendar",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("timezone", Text, nullable=False, server_default=text("'UTC'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_work_center_calendar_code"),
        CheckConstraint("code <> ''", name="ck_work_center_calendar_code"),
    ],
    indexes=[("active", "is_active")],
)

work_center_calendar_day = mk(
    "work_center_calendar_day",
    [
        Column("calendar_id", UUID_T, nullable=False),
        Column("day_of_week", Integer, nullable=True),
        Column("specific_date", Date, nullable=True),
        Column("shift_code", Text, nullable=False, server_default=text("'DAY'")),
        Column("start_time", DateTime(timezone=False), nullable=False),
        Column("end_time", DateTime(timezone=False), nullable=False),
        Column("capacity_hours", QTY, nullable=False, server_default=text("8")),
        Column("is_working_day", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "calendar_id",
            "day_of_week",
            "specific_date",
            "shift_code",
            name="uq_work_center_calendar_day_scope",
        ),
        CheckConstraint(
            "(day_of_week IS NOT NULL)::int + (specific_date IS NOT NULL)::int = 1",
            name="ck_work_center_calendar_day_one",
        ),
        CheckConstraint("day_of_week IS NULL OR (day_of_week BETWEEN 1 AND 7)", name="ck_work_center_calendar_day_dow"),
        CheckConstraint("capacity_hours >= 0", name="ck_work_center_calendar_day_capacity"),
        _fk("calendar_id", "manufacturing", "work_center_calendar"),
    ],
    indexes=[("calendar", "calendar_id")],
)

work_center = mk(
    "work_center",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("location_id", UUID_T, nullable=True),
        Column("calendar_id", UUID_T, nullable=True),
        Column("group_id", UUID_T, nullable=True),
        Column("cost_per_hour", MONEY, nullable=False, server_default=text("0")),
        Column("capacity_hours_per_day", QTY, nullable=False, server_default=text("8")),
        Column("efficiency_percent", QTY, nullable=False, server_default=text("100")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_work_center_code"),
        CheckConstraint("capacity_hours_per_day > 0", name="ck_work_center_capacity"),
        CheckConstraint("efficiency_percent > 0", name="ck_work_center_efficiency"),
        CheckConstraint("cost_per_hour >= 0", name="ck_work_center_cost"),
        CheckConstraint("code <> ''", name="ck_work_center_code"),
        _fk("location_id", "traceability", "location"),
        _fk("calendar_id", "manufacturing", "work_center_calendar"),
        _fk("group_id", "manufacturing", "work_center_group"),
    ],
    indexes=[("location", "location_id")],
)

routing_step = mk(
    "routing_step",
    [
        Column("routing_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("operation_code", Text, nullable=False),
        Column("description", Text, nullable=True),
        Column("work_center_id", UUID_T, nullable=False),
        Column("setup_time_minutes", QTY, nullable=False, server_default=text("0")),
        Column("run_time_per_unit_minutes", QTY, nullable=False, server_default=text("0")),
        Column("cost_category_id", UUID_T, nullable=True),
        Column("is_outsourced", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "routing_id", "sequence", name="uq_routing_step_sequence"),
        CheckConstraint("sequence > 0", name="ck_routing_step_sequence"),
        CheckConstraint("setup_time_minutes >= 0 AND run_time_per_unit_minutes >= 0", name="ck_routing_step_times"),
        _fk("routing_id", "manufacturing", "routing"),
        _fk("work_center_id", "manufacturing", "work_center"),
        _fk("cost_category_id", "costing", "cost_category"),
    ],
    indexes=[("work_center", "work_center_id")],
)

routing_step_charge = mk(
    "routing_step_charge",
    [
        Column("routing_step_id", UUID_T, nullable=False),
        Column("charge_item_id", UUID_T, nullable=False),
        Column("rate", MONEY, nullable=False, server_default=text("0")),
        Column("rate_uom", Text, nullable=False, server_default=text("'HOUR'")),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "routing_step_id", "charge_item_id", name="uq_routing_step_charge"),
        CheckConstraint("rate >= 0", name="ck_routing_step_charge_rate"),
        _fk("routing_step_id", "manufacturing", "routing_step"),
        _fk("charge_item_id", "item_master", "item"),
        _fk("cost_category_id", "costing", "cost_category"),
    ],
    indexes=[("charge_item", "charge_item_id")],
)

manufacturing_cost_template = mk(
    "manufacturing_cost_template",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("rate", MONEY, nullable=False, server_default=text("0")),
        Column("rate_uom", Text, nullable=False, server_default=text("'HOUR'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_manufacturing_cost_template_code"),
        CheckConstraint("rate >= 0", name="ck_manufacturing_cost_template_rate"),
        CheckConstraint("code <> ''", name="ck_manufacturing_cost_template_code"),
        _fk("cost_category_id", "costing", "cost_category"),
    ],
    indexes=[("cost_category", "cost_category_id")],
)

manufacturing_cost_template_line = mk(
    "manufacturing_cost_template_line",
    [
        Column("template_id", UUID_T, nullable=False),
        Column("work_center_id", UUID_T, nullable=True),
        Column("operation_code", Text, nullable=True),
        Column("rate", MONEY, nullable=False),
        Column("rate_uom", Text, nullable=False, server_default=text("'HOUR'")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "template_id",
            "work_center_id",
            "operation_code",
            "effective_from",
            name="uq_manufacturing_cost_template_line_scope",
        ),
        CheckConstraint("rate >= 0", name="ck_manufacturing_cost_template_line_rate"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_cost_template_line_dates"),
        _fk("template_id", "manufacturing", "manufacturing_cost_template"),
        _fk("work_center_id", "manufacturing", "work_center"),
    ],
    indexes=[("work_center", "work_center_id")],
)

# =================================================================================================
# 0020_manufacturing_execution — WO/WIP/build/unbuild/outsourced/ECO/scheduling/compat (27 tables)
# =================================================================================================
work_order = mk(
    "work_order",
    _doc(
        [
            Column("assembly_item_id", UUID_T, nullable=False),
            Column("wo_type", Text, nullable=False, server_default=text("'STANDARD'")),
            Column("qty_ordered", QTY, nullable=False),
            Column("qty_built", QTY, nullable=False, server_default=text("0")),
            Column("qty_scrapped", QTY, nullable=False, server_default=text("0")),
            Column("uom_id", UUID_T, nullable=False),
            Column("bom_revision_id", UUID_T, nullable=True),
            Column("routing_id", UUID_T, nullable=True),
            Column("wip_location_id", UUID_T, nullable=True),
            Column("start_date", Date, nullable=False),
            Column("end_date", Date, nullable=False),
            Column("parent_work_order_id", UUID_T, nullable=True),
            Column("external_sales_ref", Text, nullable=True),
            Column("firm_planned_at", STAMP, nullable=True),
            Column("released_at", STAMP, nullable=True),
            Column("build_transaction_id", UUID_T, nullable=True),
            Column("memo", Text, nullable=True),
        ],
        status="PLANNED",
    ),
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_work_order_number"),
        CheckConstraint(f"status IN {WO_STATUSES!r}", name="ck_work_order_status"),
        CheckConstraint(f"wo_type IN {WO_TYPES!r}", name="ck_work_order_type"),
        CheckConstraint("qty_ordered > 0", name="ck_work_order_qty"),
        CheckConstraint("qty_built >= 0 AND qty_built <= qty_ordered", name="ck_work_order_built"),
        CheckConstraint("qty_scrapped >= 0", name="ck_work_order_scrapped"),
        CheckConstraint("end_date >= start_date", name="ck_work_order_dates"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
        _fk("routing_id", "manufacturing", "routing"),
        _fk("wip_location_id", "traceability", "location"),
    ],
    indexes=[
        ("status_start", "status", "start_date"),
        ("assembly", "assembly_item_id"),
        ("external_sales_ref", "external_sales_ref"),
    ],
)

work_order_line = mk(
    "work_order_line",
    [
        Column("work_order_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("assembly_item_id", UUID_T, nullable=False),
        Column("qty_ordered", QTY, nullable=False),
        Column("qty_built", QTY, nullable=False, server_default=text("0")),
        Column("qty_scrapped", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=False),
        Column("due_date", Date, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PLANNED'")),
        Column("bom_revision_id", UUID_T, nullable=True),
        Column("routing_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "work_order_id", "line_number", name="uq_work_order_line_number"),
        CheckConstraint(f"status IN {WO_LINE_STATUSES!r}", name="ck_work_order_line_status"),
        CheckConstraint("qty_ordered > 0", name="ck_work_order_line_qty"),
        CheckConstraint("qty_built >= 0 AND qty_built <= qty_ordered", name="ck_work_order_line_built"),
        _fk("work_order_id", "manufacturing", "work_order"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
        _fk("routing_id", "manufacturing", "routing"),
    ],
    indexes=[("assembly", "assembly_item_id")],
)

work_order_component = mk(
    "work_order_component",
    [
        Column("work_order_line_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("component_item_id", UUID_T, nullable=False),
        Column("qty_required", QTY, nullable=False, server_default=text("0")),
        Column("qty_issued", QTY, nullable=False, server_default=text("0")),
        Column("qty_committed", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=False),
        Column("component_yield_percent", QTY, nullable=True),
        Column("round_up_component_qty", Boolean, nullable=False, server_default=text("false")),
        Column("over_issue_tolerance", QTY, nullable=False, server_default=text("0")),
        Column("operation_sequence", Integer, nullable=True),
        Column("is_phantom", Boolean, nullable=False, server_default=text("false")),
        Column("source_bom_component_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "work_order_line_id", "line_number", name="uq_work_order_component_line"),
        CheckConstraint("qty_required >= 0", name="ck_work_order_component_required"),
        CheckConstraint("qty_issued >= 0", name="ck_work_order_component_issued"),
        CheckConstraint(
            "qty_issued <= qty_required * (1 + over_issue_tolerance / 100)", name="ck_work_order_component_tolerance"
        ),
        CheckConstraint("over_issue_tolerance >= 0", name="ck_work_order_component_tolerance_value"),
        _fk("work_order_line_id", "manufacturing", "work_order_line"),
        _fk("component_item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("source_bom_component_id", "manufacturing", "bom_component"),
    ],
    indexes=[("component", "component_item_id"), ("wo_line", "work_order_line_id")],
)

work_order_operation = mk(
    "work_order_operation",
    [
        Column("work_order_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("routing_step_id", UUID_T, nullable=True),
        Column("work_center_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("planned_start", STAMP, nullable=True),
        Column("planned_end", STAMP, nullable=True),
        Column("actual_start", STAMP, nullable=True),
        Column("actual_end", STAMP, nullable=True),
        Column("qty_completed", QTY, nullable=False, server_default=text("0")),
        Column("qty_scrapped", QTY, nullable=False, server_default=text("0")),
        Column("setup_minutes_actual", QTY, nullable=False, server_default=text("0")),
        Column("run_minutes_actual", QTY, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "work_order_id", "sequence", name="uq_work_order_operation_sequence"),
        CheckConstraint(f"status IN {OPERATION_STATUSES!r}", name="ck_work_order_operation_status"),
        CheckConstraint("qty_completed >= 0 AND qty_scrapped >= 0", name="ck_work_order_operation_qty"),
        CheckConstraint("setup_minutes_actual >= 0 AND run_minutes_actual >= 0", name="ck_work_order_operation_times"),
        _fk("work_order_id", "manufacturing", "work_order"),
        _fk("routing_step_id", "manufacturing", "routing_step"),
        _fk("work_center_id", "manufacturing", "work_center"),
    ],
    indexes=[("status_start", "status", "planned_start"), ("work_center", "work_center_id")],
)

operation_completion = mk(
    "operation_completion",
    [
        Column("work_order_operation_id", UUID_T, nullable=False),
        Column("completion_number", Integer, nullable=False),
        Column("qty_completed", QTY, nullable=False, server_default=text("0")),
        Column("qty_scrapped", QTY, nullable=False, server_default=text("0")),
        Column("labor_hours", QTY, nullable=False, server_default=text("0")),
        Column("machine_hours", QTY, nullable=False, server_default=text("0")),
        Column("completed_by", UUID_T, nullable=True),
        Column("completed_at", STAMP, nullable=False, server_default=text("now()")),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "work_order_operation_id", "completion_number", name="uq_operation_completion_number"
        ),
        CheckConstraint("qty_completed >= 0 AND qty_scrapped >= 0", name="ck_operation_completion_qty"),
        CheckConstraint("labor_hours >= 0 AND machine_hours >= 0", name="ck_operation_completion_hours"),
        _fk("work_order_operation_id", "manufacturing", "work_order_operation"),
    ],
    indexes=[("completed_at", "completed_at")],
)

wip_transaction = mk(
    "wip_transaction",
    _doc(
        [
            Column("wip_transaction_type", Text, nullable=False),
            Column("work_order_id", UUID_T, nullable=False),
            Column("is_backflush", Boolean, nullable=False, server_default=text("false")),
            Column("posted_transaction_id", UUID_T, nullable=True),
            Column("memo", Text, nullable=True),
        ]
    ),
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_wip_transaction_number"),
        CheckConstraint(f"wip_transaction_type IN {WIP_TRANSACTION_TYPES!r}", name="ck_wip_transaction_type"),
        CheckConstraint(f"status IN {WIP_STATUSES!r}", name="ck_wip_transaction_status"),
        _fk("work_order_id", "manufacturing", "work_order"),
    ],
    indexes=[("wo_date", "work_order_id", "transaction_date")],
)
Index(
    "uq_wip_transaction_posted",
    wip_transaction.c.tenant_id,
    wip_transaction.c.posted_transaction_id,
    unique=True,
    postgresql_where=text("posted_transaction_id IS NOT NULL"),
)

wip_transaction_line = mk(
    "wip_transaction_line",
    [
        Column("wip_transaction_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("is_assembly_output", Boolean, nullable=False, server_default=text("false")),
        Column("work_order_component_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "wip_transaction_id", "line_number", name="uq_wip_transaction_line_number"),
        CheckConstraint("qty > 0", name="ck_wip_transaction_line_qty"),
        _fk("wip_transaction_id", "manufacturing", "wip_transaction"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("work_order_component_id", "manufacturing", "work_order_component"),
        _fk("lot_id", "traceability", "lot"),
        _fk("location_id", "traceability", "location"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("item", "item_id"), ("wo_component", "work_order_component_id")],
)

assembly_build = mk(
    "assembly_build",
    _doc(
        [
            Column("assembly_item_id", UUID_T, nullable=False),
            Column("work_order_id", UUID_T, nullable=True),
            Column("qty_built", QTY, nullable=False),
            Column("uom_id", UUID_T, nullable=False),
            Column("location_id", UUID_T, nullable=False),
            Column("bom_revision_id", UUID_T, nullable=True),
            Column("build_cost", MONEY, nullable=False, server_default=text("0")),
            Column("lot_id", UUID_T, nullable=True),
            Column("serial_ids", ARRAY(UUID_T), nullable=True),
            Column("is_from_original_build", Boolean, nullable=False, server_default=text("false")),
            Column("posted_transaction_id", UUID_T, nullable=True),
        ]
    ),
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_assembly_build_number"),
        CheckConstraint("qty_built > 0", name="ck_assembly_build_qty"),
        CheckConstraint(f"status IN {BUILD_STATUSES!r}", name="ck_assembly_build_status"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("work_order_id", "manufacturing", "work_order"),
        _fk("uom_id", "item_master", "uom"),
        _fk("location_id", "traceability", "location"),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
        _fk("lot_id", "traceability", "lot"),
    ],
    indexes=[("assembly_date", "assembly_item_id", "transaction_date"), ("wo", "work_order_id")],
)

assembly_build_component = mk(
    "assembly_build_component",
    [
        Column("assembly_build_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("component_item_id", UUID_T, nullable=False),
        Column("qty_consumed", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("source_bom_component_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "assembly_build_id", "line_number", name="uq_assembly_build_component_line"),
        CheckConstraint("qty_consumed > 0", name="ck_assembly_build_component_qty"),
        _fk("assembly_build_id", "manufacturing", "assembly_build"),
        _fk("component_item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("lot_id", "traceability", "lot"),
        _fk("location_id", "traceability", "location"),
        _fk("inventory_status_id", "traceability", "inventory_status"),
        _fk("source_bom_component_id", "manufacturing", "bom_component"),
    ],
    indexes=[("component", "component_item_id")],
)

assembly_unbuild = mk(
    "assembly_unbuild",
    _doc(
        [
            Column("assembly_item_id", UUID_T, nullable=False),
            Column("original_assembly_build_id", UUID_T, nullable=True),
            Column("qty_unbuilt", QTY, nullable=False),
            Column("uom_id", UUID_T, nullable=False),
            Column("location_id", UUID_T, nullable=False),
            Column("reason_code_id", UUID_T, nullable=True),
            Column("unbuild_variance_account_ref", Text, nullable=True),
            Column("bom_revision_id", UUID_T, nullable=True),
            Column("posted_transaction_id", UUID_T, nullable=True),
        ]
    ),
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_assembly_unbuild_number"),
        CheckConstraint("qty_unbuilt > 0", name="ck_assembly_unbuild_qty"),
        CheckConstraint(f"status IN {BUILD_STATUSES!r}", name="ck_assembly_unbuild_status"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("original_assembly_build_id", "manufacturing", "assembly_build"),
        _fk("uom_id", "item_master", "uom"),
        _fk("location_id", "traceability", "location"),
        _fk("reason_code_id", "inventory_ledger", "inventory_reason_code"),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
    ],
    indexes=[("assembly_date", "assembly_item_id", "transaction_date")],
)

assembly_unbuild_component = mk(
    "assembly_unbuild_component",
    [
        Column("assembly_unbuild_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("component_item_id", UUID_T, nullable=False),
        Column("qty_restored", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("variance_value", MONEY, nullable=False, server_default=text("0")),
        Column("location_id", UUID_T, nullable=False),
        Column("disposition", Text, nullable=False, server_default=text("'RETURN_TO_STOCK'")),
        Column("is_used", Boolean, nullable=False, server_default=text("false")),
        Column("condition_code_id", UUID_T, nullable=True),
        Column("return_status_id", UUID_T, nullable=True),
        Column("target_location_id", UUID_T, nullable=True),
        Column("target_bin_id", UUID_T, nullable=True),
        Column("allow_resale", Boolean, nullable=False, server_default=text("false")),
        Column("value_policy", Text, nullable=False, server_default=text("'ORIGINAL_COST'")),
        Column("unit_value", MONEY, nullable=True),
        Column("reason_code_id", UUID_T, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "assembly_unbuild_id", "line_number", name="uq_assembly_unbuild_component_line"),
        CheckConstraint("qty_restored >= 0", name="ck_assembly_unbuild_component_qty"),
        CheckConstraint(f"disposition IN {UNBUILD_DISPOSITIONS!r}", name="ck_assembly_unbuild_component_disposition"),
        CheckConstraint(
            f"value_policy IN {UNBUILD_VALUE_POLICIES!r}", name="ck_assembly_unbuild_component_value_policy"
        ),
        CheckConstraint(
            "value_policy <> 'MANUAL' OR unit_value IS NOT NULL", name="ck_assembly_unbuild_component_manual_value"
        ),
        CheckConstraint(
            "disposition <> 'SCRAP' OR (return_status_id IS NULL AND target_location_id IS NULL AND target_bin_id IS NULL)",
            name="ck_assembly_unbuild_component_scrap",
        ),
        CheckConstraint(
            "NOT is_used OR return_status_id IS NOT NULL", name="ck_assembly_unbuild_component_used_status"
        ),
        _fk("assembly_unbuild_id", "manufacturing", "assembly_unbuild"),
        _fk("component_item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom"),
        _fk("lot_id", "traceability", "lot"),
        _fk("location_id", "traceability", "location"),
        _fk("condition_code_id", "manufacturing", "condition_code"),
        _fk("return_status_id", "traceability", "inventory_status"),
        _fk("target_location_id", "traceability", "location"),
        _fk("target_bin_id", "traceability", "bin"),
        _fk("reason_code_id", "inventory_ledger", "inventory_reason_code"),
    ],
    indexes=[
        ("component", "component_item_id"),
        ("condition", "condition_code_id"),
        ("posted", "posted_transaction_id"),
    ],
)

outsourced_operation = mk(
    "outsourced_operation",
    [
        Column("work_order_id", UUID_T, nullable=True),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("vendor_id", UUID_T, nullable=False),
        Column("operation_code", Text, nullable=False),
        Column("qty_ordered", QTY, nullable=False),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("status", Text, nullable=False, server_default=text("'PLANNED'")),
        Column("assembly_item_id", UUID_T, nullable=False),
        Column("component_location_id", UUID_T, nullable=False),
    ],
    extra=[
        CheckConstraint(f"status IN {OUTSOURCED_STATUSES!r}", name="ck_outsourced_operation_status"),
        CheckConstraint("qty_ordered > 0", name="ck_outsourced_operation_qty"),
        CheckConstraint("qty_received >= 0 AND qty_received <= qty_ordered", name="ck_outsourced_operation_received"),
        _fk("work_order_id", "manufacturing", "work_order"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("component_location_id", "traceability", "location"),
    ],
    indexes=[("vendor_status", "vendor_id", "status"), ("wo", "work_order_id")],
)

outsourced_charge = mk(
    "outsourced_charge",
    [
        Column("outsourced_operation_id", UUID_T, nullable=False),
        Column("charge_item_id", UUID_T, nullable=True),
        Column("cost_category_id", UUID_T, nullable=False),
        Column("amount", MONEY, nullable=False, server_default=text("0")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'ESTIMATED'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "outsourced_operation_id", "charge_item_id", name="uq_outsourced_charge_scope"),
        CheckConstraint(f"status IN {OUTSOURCED_CHARGE_STATUSES!r}", name="ck_outsourced_charge_status"),
        CheckConstraint("amount >= 0", name="ck_outsourced_charge_amount"),
        CheckConstraint("currency_code <> ''", name="ck_outsourced_charge_currency"),
        _fk("outsourced_operation_id", "manufacturing", "outsourced_operation"),
        _fk("charge_item_id", "item_master", "item"),
        _fk("cost_category_id", "costing", "cost_category"),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line"),
    ],
    indexes=[("po_line", "purchase_order_line_id")],
)

eco = mk(
    "eco",
    [
        Column("eco_number", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("effective_date", Date, nullable=False),
        Column("reason", Text, nullable=False),
        Column("approved_by", UUID_T, nullable=True),
        Column("implemented_at", STAMP, nullable=True),
        Column("eco_type_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "eco_number", name="uq_eco_number"),
        CheckConstraint(f"status IN {ECO_STATUSES!r}", name="ck_eco_status"),
        CheckConstraint("reason <> ''", name="ck_eco_reason"),
    ],
    indexes=[("status_date", "status", "effective_date")],
)

eco_line = mk(
    "eco_line",
    [
        Column("eco_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("bom_id", UUID_T, nullable=True),
        Column("bom_component_id", UUID_T, nullable=True),
        Column("change_type", Text, nullable=False),
        Column("old_value", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("new_value", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False, server_default=text("'PROPOSED'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "eco_id", "line_number", name="uq_eco_line_number"),
        CheckConstraint(f"change_type IN {ECO_CHANGE_TYPES!r}", name="ck_eco_line_change_type"),
        CheckConstraint(f"status IN {ECO_LINE_STATUSES!r}", name="ck_eco_line_status"),
        _fk("eco_id", "manufacturing", "eco"),
        _fk("item_id", "item_master", "item"),
        _fk("bom_id", "manufacturing", "bom"),
        _fk("bom_component_id", "manufacturing", "bom_component"),
    ],
    indexes=[("item", "item_id")],
)

scheduling_run = mk(
    "scheduling_run",
    [
        Column("run_number", Text, nullable=False),
        Column("method", Text, nullable=False),
        Column("capacity_mode", Text, nullable=False),
        Column("horizon_start", Date, nullable=False),
        Column("horizon_end", Date, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'QUEUED'")),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
        Column("work_center_scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "run_number", name="uq_scheduling_run_number"),
        CheckConstraint(f"method IN {SCHEDULING_METHODS!r}", name="ck_scheduling_run_method"),
        CheckConstraint(f"capacity_mode IN {CAPACITY_MODES!r}", name="ck_scheduling_run_capacity_mode"),
        CheckConstraint(f"status IN {SCHEDULING_RUN_STATUSES!r}", name="ck_scheduling_run_status"),
        CheckConstraint("horizon_end >= horizon_start", name="ck_scheduling_run_horizon"),
    ],
    indexes=[("status_horizon", "status", "horizon_start")],
)

scheduled_operation = mk(
    "scheduled_operation",
    [
        Column("scheduling_run_id", UUID_T, nullable=False),
        Column("work_order_operation_id", UUID_T, nullable=False),
        Column("work_center_id", UUID_T, nullable=False),
        Column("planned_start", STAMP, nullable=False),
        Column("planned_end", STAMP, nullable=False),
        Column("sequence", Integer, nullable=False, server_default=text("0")),
        Column("is_compressed", Boolean, nullable=False, server_default=text("false")),
        Column("violation_reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "scheduling_run_id", "work_order_operation_id", name="uq_scheduled_operation_scope"
        ),
        CheckConstraint("planned_end > planned_start", name="ck_scheduled_operation_range"),
        _fk("scheduling_run_id", "manufacturing", "scheduling_run"),
        _fk("work_order_operation_id", "manufacturing", "work_order_operation"),
        _fk("work_center_id", "manufacturing", "work_center"),
    ],
    indexes=[("wc_start", "work_center_id", "planned_start")],
)

capacity_plan = mk(
    "capacity_plan",
    [
        Column("work_center_id", UUID_T, nullable=False),
        Column("bucket_date", Date, nullable=False),
        Column("available_hours", QTY, nullable=False, server_default=text("0")),
        Column("load_hours", QTY, nullable=False, server_default=text("0")),
        Column(
            "utilization_percent",
            QTY,
            Computed(
                "CASE WHEN available_hours > 0 THEN load_hours / available_hours * 100 ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
        Column("status", Text, nullable=False, server_default=text("'OK'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "work_center_id", "bucket_date", name="uq_capacity_plan_scope"),
        CheckConstraint(f"status IN {CAPACITY_STATUSES!r}", name="ck_capacity_plan_status"),
        CheckConstraint("available_hours >= 0 AND load_hours >= 0", name="ck_capacity_plan_hours"),
        _fk("work_center_id", "manufacturing", "work_center"),
    ],
    indexes=[("bucket_status", "bucket_date", "status")],
)

work_center_load = mk(
    "work_center_load",
    [
        Column("work_center_id", UUID_T, nullable=False),
        Column("work_order_operation_id", UUID_T, nullable=False),
        Column("load_date", Date, nullable=False),
        Column("load_hours", QTY, nullable=False, server_default=text("0")),
        Column("scheduling_run_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "work_center_id", "work_order_operation_id", "load_date", name="uq_work_center_load_scope"
        ),
        CheckConstraint("load_hours >= 0", name="ck_work_center_load_hours"),
        _fk("work_center_id", "manufacturing", "work_center"),
        _fk("work_order_operation_id", "manufacturing", "work_order_operation"),
        _fk("scheduling_run_id", "manufacturing", "scheduling_run"),
    ],
    indexes=[("wc_date", "work_center_id", "load_date")],
)

work_order_wip_balance = mk(
    "work_order_wip_balance",
    [
        Column("work_order_id", UUID_T, nullable=False),
        Column("qty_issued", QTY, nullable=False, server_default=text("0")),
        Column("value_issued", MONEY, nullable=False, server_default=text("0")),
        Column("qty_completed", QTY, nullable=False, server_default=text("0")),
        Column("value_completed", MONEY, nullable=False, server_default=text("0")),
        Column("wip_value", MONEY, nullable=False, server_default=text("0")),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("last_ledger_sequence", Integer, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "work_order_id", name="uq_work_order_wip_balance_wo"),
        CheckConstraint(f"status IN {WIP_BALANCE_STATUSES!r}", name="ck_work_order_wip_balance_status"),
        _fk("work_order_id", "manufacturing", "work_order"),
    ],
    indexes=[("status", "status")],
)

# ------------------------------------------------------------------------- Δ-CFG compatibility -----
compat_attribute_def = mk(
    "compat_attribute_def",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("data_type", Text, nullable=False),
        Column("unit", Text, nullable=True),
        Column("is_multi_valued", Boolean, nullable=False, server_default=text("false")),
        Column("applicable_item_types", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
        Column("choices", JSONB, nullable=True),
        Column("description", Text, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_compat_attribute_def_code"),
        CheckConstraint(f"data_type IN {COMPAT_DATA_TYPES!r}", name="ck_compat_attribute_def_type"),
        CheckConstraint("data_type <> 'SELECT' OR choices IS NOT NULL", name="ck_compat_attribute_def_choices"),
        CheckConstraint("code <> ''", name="ck_compat_attribute_def_code"),
    ],
    indexes=[("active", "is_active"), ("data_type", "data_type")],
)

compat_attribute_value = mk(
    "compat_attribute_value",
    [
        Column("attribute_def_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("value_text", Text, nullable=True),
        Column("value_number", QTY, nullable=True),
        Column("value_bool", Boolean, nullable=True),
        Column("value_json", JSONB, nullable=True),
        Column("provenance", Text, nullable=False, server_default=text("'MANUAL'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "attribute_def_id", name="uq_compat_attribute_value_scope"),
        CheckConstraint(
            "(value_text IS NOT NULL)::int + (value_number IS NOT NULL)::int + (value_bool IS NOT NULL)::int"
            " + (value_json IS NOT NULL)::int = 1",
            name="ck_compat_attribute_value_one",
        ),
        CheckConstraint(f"provenance IN {PROVENANCES!r}", name="ck_compat_attribute_value_provenance"),
        _fk("attribute_def_id", "manufacturing", "compat_attribute_def"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("attribute", "attribute_def_id"), ("item", "item_id")],
)

compat_rule_set = mk(
    "compat_rule_set",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("scope_type", Text, nullable=False),
        Column("scope_ref_id", UUID_T, nullable=True),
        Column("scope_ref_text", Text, nullable=True),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("enforcement_mode", Text, nullable=False, server_default=text("'INHERIT'")),
        Column("description", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_compat_rule_set_code"),
        CheckConstraint(f"scope_type IN {COMPAT_SCOPE_TYPES!r}", name="ck_compat_rule_set_scope_type"),
        CheckConstraint(f"status IN {COMPAT_RULE_SET_STATUSES!r}", name="ck_compat_rule_set_status"),
        CheckConstraint(f"enforcement_mode IN {COMPAT_ENFORCEMENT!r}", name="ck_compat_rule_set_enforcement"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_compat_rule_set_dates"),
        CheckConstraint("priority >= 0", name="ck_compat_rule_set_priority"),
        CheckConstraint("code <> ''", name="ck_compat_rule_set_code"),
    ],
    indexes=[("scope_status", "scope_type", "status"), ("effective", "effective_from", "effective_to")],
)
Index(
    "ix_compat_rule_set_scope_ref",
    compat_rule_set.c.tenant_id,
    compat_rule_set.c.scope_type,
    compat_rule_set.c.scope_ref_id,
    postgresql_where=text("status = 'ACTIVE'"),
)

compat_rule = mk(
    "compat_rule",
    [
        Column("rule_set_id", UUID_T, nullable=False),
        Column("code", Text, nullable=False),
        Column("rule_type", Text, nullable=False),
        Column("subject_type", Text, nullable=False),
        Column("subject_expr", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("constraint_expr", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("severity", Text, nullable=False, server_default=text("'INHERIT'")),
        Column("message_code", Text, nullable=False, server_default=text("'COMPATIBILITY_VIOLATION'")),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "rule_set_id", "code", name="uq_compat_rule_code"),
        CheckConstraint(f"rule_type IN {COMPAT_RULE_TYPES!r}", name="ck_compat_rule_type"),
        CheckConstraint(f"subject_type IN {COMPAT_SUBJECT_TYPES!r}", name="ck_compat_rule_subject_type"),
        CheckConstraint(f"severity IN {COMPAT_ENFORCEMENT!r}", name="ck_compat_rule_severity"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_compat_rule_dates"),
        CheckConstraint("priority >= 0", name="ck_compat_rule_priority"),
        _fk("rule_set_id", "manufacturing", "compat_rule_set"),
    ],
    indexes=[("rule_set_priority", "rule_set_id", "priority"), ("rule_type", "rule_type", "is_active")],
)

compat_slot_def = mk(
    "compat_slot_def",
    [
        Column("assembly_item_id", UUID_T, nullable=True),
        Column("bom_revision_id", UUID_T, nullable=True),
        Column("slot_code", Text, nullable=False),
        Column("slot_type", Text, nullable=False),
        Column("capacity", QTY, nullable=False),
        Column("unit", Text, nullable=True),
        Column("component_attribute_constraints", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("units_per_component", QTY, nullable=False, server_default=text("1")),
        Column("is_required", Boolean, nullable=False, server_default=text("false")),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "assembly_item_id", "bom_revision_id", "slot_code", name="uq_compat_slot_def_scope"
        ),
        CheckConstraint("assembly_item_id IS NOT NULL OR bom_revision_id IS NOT NULL", name="ck_compat_slot_def_owner"),
        CheckConstraint(f"slot_type IN {COMPAT_SLOT_TYPES!r}", name="ck_compat_slot_def_type"),
        CheckConstraint("capacity > 0", name="ck_compat_slot_def_capacity"),
        CheckConstraint("units_per_component > 0", name="ck_compat_slot_def_units"),
        CheckConstraint("slot_code <> ''", name="ck_compat_slot_def_code"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
        _fk("bom_revision_id", "manufacturing", "bom_revision"),
    ],
    indexes=[("assembly", "assembly_item_id"), ("bom_revision", "bom_revision_id")],
)

compat_configuration = mk(
    "compat_configuration",
    [
        Column("configuration_hash", Text, nullable=False),
        Column("assembly_item_id", UUID_T, nullable=False),
        Column("context", Text, nullable=False),
        Column("context_ref_type", Text, nullable=True),
        Column("context_ref_id", Text, nullable=True),
        Column("components_snapshot", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("attributes_snapshot", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("matched_rule_sets", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("matched_rules", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("rule_set_versions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("result", Text, nullable=False),
        Column("violation_count", Integer, nullable=False, server_default=text("0")),
        Column("override_by", UUID_T, nullable=True),
        Column("override_reason", Text, nullable=True),
        Column("overridden_at", STAMP, nullable=True),
        Column("validated_at", STAMP, nullable=False, server_default=text("now()")),
        Column("requested_by", UUID_T, nullable=True),
    ],
    extra=[
        CheckConstraint(f"context IN {COMPAT_CONTEXTS!r}", name="ck_compat_configuration_context"),
        CheckConstraint(f"result IN {COMPAT_RESULTS!r}", name="ck_compat_configuration_result"),
        CheckConstraint(
            "result <> 'OVERRIDDEN' OR (override_by IS NOT NULL AND override_reason IS NOT NULL)",
            name="ck_compat_configuration_override",
        ),
        CheckConstraint("configuration_hash <> ''", name="ck_compat_configuration_hash"),
        _fk("assembly_item_id", "manufacturing", "assembly_item"),
    ],
    indexes=[
        ("assembly_context", "assembly_item_id", "context", "validated_at"),
        ("result", "result"),
        ("hash", "configuration_hash"),
    ],
    pk_columns=("id", "validated_at"),
)

condition_code = mk(
    "condition_code",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_used", Boolean, nullable=False, server_default=text("false")),
        Column("is_sellable_default", Boolean, nullable=False, server_default=text("false")),
        Column("is_committable_default", Boolean, nullable=False, server_default=text("false")),
        Column("description", Text, nullable=True),
        Column("sort_order", Integer, nullable=False, server_default=text("0")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_condition_code_code"),
        CheckConstraint("code <> ''", name="ck_condition_code_code"),
    ],
    indexes=[("active", "is_active"), ("used", "is_used")],
)

#: Tables created by each FS-09 migration (used by the migration modules).
MASTER_TABLES = (
    assembly_item,
    bom,
    bom_revision,
    bom_component,
    bom_component_control,
    assembly_revision,
)
ROUTING_TABLES = (
    routing,
    work_center_group,
    work_center_calendar,
    work_center_calendar_day,
    work_center,
    routing_step,
    routing_step_charge,
    manufacturing_cost_template,
    manufacturing_cost_template_line,
)
EXECUTION_TABLES = (
    work_order,
    work_order_line,
    work_order_component,
    work_order_operation,
    operation_completion,
    wip_transaction,
    wip_transaction_line,
    assembly_build,
    assembly_build_component,
    assembly_unbuild,
    assembly_unbuild_component,
    outsourced_operation,
    outsourced_charge,
    eco,
    eco_line,
    scheduling_run,
    scheduled_operation,
    capacity_plan,
    work_center_load,
    work_order_wip_balance,
    compat_attribute_def,
    compat_attribute_value,
    compat_rule_set,
    compat_rule,
    compat_slot_def,
    compat_configuration,
    condition_code,
)

ALL_TABLES = MASTER_TABLES + ROUTING_TABLES + EXECUTION_TABLES
