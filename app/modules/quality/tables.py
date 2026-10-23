"""SQLAlchemy Core metadata for the 10 `quality` tables (FS-12, DATA-MODEL §14).

Migration ``0026_quality`` compiles this metadata into explicit PostgreSQL DDL; quality repositories
query it with async SQLAlchemy Core. Quality never posts inventory: holds are applied through the
FS-04 `inventory.ledger` T-30 status-change command (DM-QA-02).
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

metadata = MetaData(schema="quality", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

SPEC_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")
RESULT_TYPES = ("NUMERIC", "TEXT", "PASS_FAIL", "CHOICE")
INSPECTION_TYPES = ("INCOMING", "IN_PROCESS", "FINAL", "ON_DEMAND")
SAMPLING_METHODS = ("FULL", "SAMPLE_RANDOM", "SAMPLE_PERCENT")
TRIGGER_EVENTS = ("ITEM_RECEIPT", "WORK_ORDER_COMPLETION", "FULFILLMENT", "ADJUSTMENT", "MANUAL")
ASSIGNMENT_STRATEGIES = ("ROUND_ROBIN", "LEAST_OPEN", "MANUAL")
INSPECTION_STATUSES = ("PENDING", "IN_PROGRESS", "PASSED", "FAILED", "WAIVED", "CANCELLED")
DISPOSITIONS = ("ACCEPT", "REJECT", "HOLD", "REWORK")
COA_STATUSES = ("DRAFT", "ISSUED", "REVOKED", "CANCELLED")


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


def mk(name: str, columns: list, *, extra: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, nullable=False), Column("tenant_id", UUID_T, nullable=False), *columns]
    _audit(cols)
    constraints: list = [
        PrimaryKeyConstraint("id", name=f"pk_{name}"),
        UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"),
        ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant"),
        *(extra or []),
    ]
    return Table(name, metadata, *cols, *constraints)


def _enum(values: tuple[str, ...]) -> str:
    return " IN (" + ",".join(f"'{value}'" for value in values) + ")"


quality_spec = mk(
    "quality_spec",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("spec_version", Integer, nullable=False, server_default=text("1")),
        Column("item_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("effective_from", Date, nullable=True),
        Column("effective_to", Date, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", "spec_version", name="uq_quality_spec_code_version"),
        CheckConstraint("status" + _enum(SPEC_STATUSES), name="ck_quality_spec_status"),
        CheckConstraint("(item_id IS NULL) <> (item_category_id IS NULL)", name="ck_quality_spec_scope_xor"),
        CheckConstraint("length(code) > 0", name="ck_quality_spec_code_nonempty"),
        Index("ix_quality_spec_status", "tenant_id", "status", "effective_from"),
    ],
)

quality_spec_line = mk(
    "quality_spec_line",
    [
        Column("quality_spec_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("characteristic", Text, nullable=False),
        Column("test_method", Text, nullable=True),
        Column("result_type", Text, nullable=False, server_default=text("'NUMERIC'")),
        Column("target_value_text", Text, nullable=True),
        Column("min_value", QTY, nullable=True),
        Column("max_value", QTY, nullable=True),
        Column("uom_id", UUID_T, nullable=True),
        Column("choices", JSONB, nullable=True),
        Column("is_required", Boolean, nullable=False, server_default=text("true")),
        Column("is_internal", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "quality_spec_id", "sequence", name="uq_quality_spec_line_seq"),
        CheckConstraint("result_type" + _enum(RESULT_TYPES), name="ck_quality_spec_line_result_type"),
        CheckConstraint(
            "min_value IS NULL OR max_value IS NULL OR min_value <= max_value", name="ck_quality_spec_line_range"
        ),
        CheckConstraint("sequence > 0", name="ck_quality_spec_line_seq_positive"),
        Index("ix_quality_spec_line_spec", "tenant_id", "quality_spec_id"),
        ForeignKeyConstraint(
            ["tenant_id", "quality_spec_id"],
            ["quality.quality_spec.tenant_id", "quality.quality_spec.id"],
            name="fk_quality_spec_line_spec",
        ),
    ],
)

inspection_plan = mk(
    "inspection_plan",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("item_category_id", UUID_T, nullable=True),
        Column("quality_spec_id", UUID_T, nullable=False),
        Column("inspection_type", Text, nullable=False, server_default=text("'INCOMING'")),
        Column("sampling_method", Text, nullable=False, server_default=text("'FULL'")),
        Column("sample_size", Integer, nullable=False, server_default=text("1")),
        Column("trigger_condition", JSONB, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_inspection_plan_code"),
        CheckConstraint("inspection_type" + _enum(INSPECTION_TYPES), name="ck_inspection_plan_type"),
        CheckConstraint("sampling_method" + _enum(SAMPLING_METHODS), name="ck_inspection_plan_sampling"),
        CheckConstraint("sample_size > 0", name="ck_inspection_plan_sample_size"),
        Index("ix_inspection_plan_active", "tenant_id", "is_active"),
        Index("ix_inspection_plan_item", "tenant_id", "item_id"),
        ForeignKeyConstraint(
            ["tenant_id", "quality_spec_id"],
            ["quality.quality_spec.tenant_id", "quality.quality_spec.id"],
            name="fk_inspection_plan_spec",
        ),
    ],
)

inspection_plan_line = mk(
    "inspection_plan_line",
    [
        Column("inspection_plan_id", UUID_T, nullable=False),
        Column("quality_spec_line_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("is_required", Boolean, nullable=False, server_default=text("true")),
        Column("sample_size_override", Integer, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "inspection_plan_id", "quality_spec_line_id", name="uq_inspection_plan_line_unique"
        ),
        CheckConstraint("sample_size_override IS NULL OR sample_size_override > 0", name="ck_plan_line_override"),
        Index("ix_plan_line_spec_line", "tenant_id", "quality_spec_line_id"),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_plan_id"],
            ["quality.inspection_plan.tenant_id", "quality.inspection_plan.id"],
            name="fk_plan_line_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quality_spec_line_id"],
            ["quality.quality_spec_line.tenant_id", "quality.quality_spec_line.id"],
            name="fk_plan_line_spec_line",
        ),
    ],
)

inspection_queue = mk(
    "inspection_queue",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("location_id", UUID_T, nullable=True),
        Column("trigger_id", UUID_T, nullable=True),
        Column("assignment_strategy", Text, nullable=False, server_default=text("'MANUAL'")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_inspection_queue_code"),
        CheckConstraint("assignment_strategy" + _enum(ASSIGNMENT_STRATEGIES), name="ck_inspection_queue_strategy"),
        Index("ix_inspection_queue_active", "tenant_id", "is_active"),
    ],
)

inspection_trigger = mk(
    "inspection_trigger",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("trigger_event", Text, nullable=False),
        Column("condition", JSONB, nullable=True),
        Column("inspection_plan_id", UUID_T, nullable=True),
        Column("inspection_queue_id", UUID_T, nullable=True),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
        Column("evaluation_counter", BigInteger, nullable=False, server_default=text("0")),
        Column("counter_reset_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_inspection_trigger_code"),
        CheckConstraint("trigger_event" + _enum(TRIGGER_EVENTS), name="ck_inspection_trigger_event"),
        CheckConstraint(
            "inspection_plan_id IS NOT NULL OR inspection_queue_id IS NOT NULL",
            name="ck_inspection_trigger_target",
        ),
        Index("ix_inspection_trigger_event", "tenant_id", "trigger_event", "is_active"),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_plan_id"],
            ["quality.inspection_plan.tenant_id", "quality.inspection_plan.id"],
            name="fk_inspection_trigger_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_queue_id"],
            ["quality.inspection_queue.tenant_id", "quality.inspection_queue.id"],
            name="fk_inspection_trigger_queue",
        ),
    ],
)

inspection = mk(
    "inspection",
    [
        Column("inspection_number", Text, nullable=False),
        Column("inspection_plan_id", UUID_T, nullable=True),
        Column("inspection_queue_id", UUID_T, nullable=True),
        Column("trigger_id", UUID_T, nullable=True),
        Column("source_entity_type", Text, nullable=False, server_default=text("'OTHER'")),
        Column("source_entity_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("inspector_id", UUID_T, nullable=True),
        Column("started_at", STAMP, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("disposition", Text, nullable=True),
        Column("result_summary", JSONB, nullable=True),
        Column("waiver_reason", Text, nullable=True),
        Column("approval_ref", Text, nullable=True),
        Column("cancel_reason", Text, nullable=True),
        Column("spec_version", Integer, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "inspection_number", name="uq_inspection_number"),
        CheckConstraint("status" + _enum(INSPECTION_STATUSES), name="ck_inspection_status"),
        CheckConstraint("disposition IS NULL OR disposition" + _enum(DISPOSITIONS), name="ck_inspection_disposition"),
        CheckConstraint(
            "status NOT IN ('PASSED','FAILED','WAIVED') OR (completed_at IS NOT NULL AND result_summary IS NOT NULL)",
            name="ck_inspection_completed_requires_result",
        ),
        Index("ix_inspection_status", "tenant_id", "status", "created_at"),
        Index("ix_inspection_item", "tenant_id", "item_id"),
        Index("ix_inspection_source", "tenant_id", "source_entity_type", "source_entity_id"),
        Index(
            "uq_inspection_trigger_once",
            "tenant_id",
            "trigger_id",
            "source_entity_type",
            "source_entity_id",
            unique=True,
            postgresql_where=text("trigger_id IS NOT NULL AND source_entity_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_plan_id"],
            ["quality.inspection_plan.tenant_id", "quality.inspection_plan.id"],
            name="fk_inspection_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_queue_id"],
            ["quality.inspection_queue.tenant_id", "quality.inspection_queue.id"],
            name="fk_inspection_queue",
        ),
    ],
)

inspection_result = mk(
    "inspection_result",
    [
        Column("inspection_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("quality_spec_line_id", UUID_T, nullable=True),
        Column("characteristic", Text, nullable=False),
        Column("measured_value_text", Text, nullable=True),
        Column("measured_value_number", QTY, nullable=True),
        Column("is_pass", Boolean, nullable=False, server_default=text("false")),
        Column("recorded_by", UUID_T, nullable=True),
        Column("recorded_at", STAMP, nullable=False, server_default=text("now()")),
        Column("notes", Text, nullable=True),
        Column("attachment_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "inspection_id", "sequence", name="uq_inspection_result_seq"),
        CheckConstraint(
            "(measured_value_text IS NULL) <> (measured_value_number IS NULL)",
            name="ck_inspection_result_one_value",
        ),
        Index("ix_inspection_result_inspection", "tenant_id", "inspection_id"),
        Index("ix_inspection_result_pass", "tenant_id", "is_pass"),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_id"],
            ["quality.inspection.tenant_id", "quality.inspection.id"],
            name="fk_inspection_result_inspection",
        ),
    ],
)

coa = mk(
    "coa",
    [
        Column("coa_number", Text, nullable=False),
        Column("inspection_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("lot_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("issued_at", STAMP, nullable=True),
        Column("issued_by", UUID_T, nullable=True),
        Column("external_customer_ref", Text, nullable=True),
        Column("attachment_id", UUID_T, nullable=True),
        Column("revoked_reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "coa_number", name="uq_coa_number"),
        CheckConstraint("status" + _enum(COA_STATUSES), name="ck_coa_status"),
        CheckConstraint(
            "status <> 'ISSUED' OR (issued_at IS NOT NULL AND issued_by IS NOT NULL)",
            name="ck_coa_issued_requires_stamp",
        ),
        Index("ix_coa_item_lot", "tenant_id", "item_id", "lot_id"),
        Index("ix_coa_status", "tenant_id", "status"),
        ForeignKeyConstraint(
            ["tenant_id", "inspection_id"],
            ["quality.inspection.tenant_id", "quality.inspection.id"],
            name="fk_coa_inspection",
        ),
    ],
)

coa_line = mk(
    "coa_line",
    [
        Column("coa_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("quality_spec_line_id", UUID_T, nullable=True),
        Column("characteristic", Text, nullable=False),
        Column("result_value", Text, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("is_pass", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "coa_id", "sequence", name="uq_coa_line_seq"),
        Index("ix_coa_line_coa", "tenant_id", "coa_id"),
        ForeignKeyConstraint(
            ["tenant_id", "coa_id"], ["quality.coa.tenant_id", "quality.coa.id"], name="fk_coa_line_coa"
        ),
    ],
)

REVISION_0026_TABLES: tuple[Table, ...] = (
    quality_spec,
    quality_spec_line,
    inspection_plan,
    inspection_plan_line,
    inspection_queue,
    inspection_trigger,
    inspection,
    inspection_result,
    coa,
    coa_line,
)

__all__ = [
    "ASSIGNMENT_STRATEGIES",
    "COA_STATUSES",
    "DISPOSITIONS",
    "INSPECTION_STATUSES",
    "INSPECTION_TYPES",
    "RESULT_TYPES",
    "REVISION_0026_TABLES",
    "SAMPLING_METHODS",
    "SPEC_STATUSES",
    "TRIGGER_EVENTS",
    "coa",
    "coa_line",
    "inspection",
    "inspection_plan",
    "inspection_plan_line",
    "inspection_queue",
    "inspection_result",
    "inspection_trigger",
    "metadata",
    "quality_spec",
    "quality_spec_line",
]
