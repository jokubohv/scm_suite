"""SQLAlchemy Core metadata for the 7 ``extensions`` tables (FS-14, DATA-MODEL §16).

Extension records are data/logic only: they never post inventory/order/finance documents. The UPC registry
is authoritative for assignment/retirement history and mirrors the item header through the FS-01 contract.
"""

from __future__ import annotations

from sqlalchemy import (
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

metadata = MetaData(schema="extensions", naming_convention=NAMING_CONVENTION)
UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

REGISTRATION_STATUSES = ("ACTIVE", "EXPIRED", "VOID")
REGISTRATION_SOURCES = ("AUTO", "MANUAL", "IMPORT")
CLAIM_TYPES = ("REFUND", "REPAIR", "REPLACEMENT")
CLAIM_STATUSES = ("OPEN", "UNDER_REVIEW", "APPROVED", "REJECTED", "COMPLETED", "CANCELLED")
ACTION_TYPES = ("CREDIT_MEMO_REF", "REPAIR", "REPLACEMENT", "REJECT", "REFUND")
ACTION_STATUSES = ("PENDING", "DONE", "FAILED")
REPAIR_STATUSES = ("OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED")
UPC_STATUSES = ("ASSIGNED", "RETIRED")
UPC_SOURCES = ("AUTO", "MANUAL")
ELIGIBLE_ITEM_TYPE_CODES = ("INVENTORY", "SERIALIZED", "LOT_NUMBERED", "ASSEMBLY", "MATRIX_CHILD")


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


item_warranty = mk(
    "item_warranty",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("warranty_duration_months", Integer, nullable=False, server_default=text("12")),
        Column("warranty_terms", Text, nullable=True),
        Column("coverage", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("is_default_for_item", Boolean, nullable=False, server_default=text("true")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", name="uq_item_warranty_item"),
        CheckConstraint("warranty_duration_months >= 0", name="ck_item_warranty_duration"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("item", "item_id")],
)

warranty_registration = mk(
    "warranty_registration",
    [
        Column("registration_number", Text, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("serial_id", UUID_T, nullable=True),
        Column("external_customer_ref", Text, nullable=True),
        Column("external_invoice_ref", Text, nullable=True),
        Column("registered_at", STAMP, nullable=False, server_default=text("now()")),
        Column("warranty_start", Date, nullable=False),
        Column("warranty_end", Date, nullable=False),
        Column("source", Text, nullable=False, server_default=text("'MANUAL'")),
        Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
        Column("voided_at", STAMP, nullable=True),
        Column("void_reason", Text, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "registration_number", name="uq_warranty_registration_number"),
        CheckConstraint("warranty_end >= warranty_start", name="ck_warranty_registration_dates"),
        CheckConstraint(f"status IN {REGISTRATION_STATUSES!r}", name="ck_warranty_registration_status"),
        CheckConstraint(f"source IN {REGISTRATION_SOURCES!r}", name="ck_warranty_registration_source"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[
        ("item", "item_id"),
        ("serial", "serial_id"),
        ("customer", "external_customer_ref"),
        ("end", "warranty_end"),
    ],
)
Index(
    "uq_warranty_registration_auto",
    warranty_registration.c.tenant_id,
    warranty_registration.c.item_id,
    warranty_registration.c.serial_id,
    warranty_registration.c.external_invoice_ref,
    unique=True,
    postgresql_where=text("source = 'AUTO' AND external_invoice_ref IS NOT NULL"),
)

warranty_claim = mk(
    "warranty_claim",
    [
        Column("claim_number", Text, nullable=False),
        Column("warranty_registration_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("serial_id", UUID_T, nullable=True),
        Column("claim_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("description", Text, nullable=True),
        Column("received_at", STAMP, nullable=False, server_default=text("now()")),
        Column("validation_result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("vendor_id", UUID_T, nullable=True),
        Column("external_return_auth_ref", Text, nullable=True),
        Column("approved_by", UUID_T, nullable=True),
        Column("approved_at", STAMP, nullable=True),
        Column("rejected_reason", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "claim_number", name="uq_warranty_claim_number"),
        CheckConstraint(f"claim_type IN {CLAIM_TYPES!r}", name="ck_warranty_claim_type"),
        CheckConstraint(f"status IN {CLAIM_STATUSES!r}", name="ck_warranty_claim_status"),
        _fk("warranty_registration_id", "extensions", "warranty_registration", nullable=True),
        _fk("item_id", "item_master", "item"),
        _fk("vendor_id", "procurement", "vendor", nullable=True),
    ],
    indexes=[("status_received", "status", "received_at"), ("item", "item_id"), ("serial", "serial_id")],
)

warranty_claim_action = mk(
    "warranty_claim_action",
    [
        Column("warranty_claim_id", UUID_T, nullable=False),
        Column("sequence", Integer, nullable=False),
        Column("action_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("performed_by", UUID_T, nullable=True),
        Column("performed_at", STAMP, nullable=True),
        Column("notes", Text, nullable=True),
        Column("external_credit_ref", Text, nullable=True),
        Column("replacement_serial_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "warranty_claim_id", "sequence", name="uq_warranty_claim_action_seq"),
        CheckConstraint(f"action_type IN {ACTION_TYPES!r}", name="ck_warranty_claim_action_type"),
        CheckConstraint(f"status IN {ACTION_STATUSES!r}", name="ck_warranty_claim_action_status"),
        _fk("warranty_claim_id", "extensions", "warranty_claim"),
    ],
    indexes=[("claim", "warranty_claim_id")],
)

warranty_repair_order = mk(
    "warranty_repair_order",
    [
        Column("repair_order_number", Text, nullable=False),
        Column("warranty_claim_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("serial_id", UUID_T, nullable=True),
        Column("location_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("labor_cost", MONEY, nullable=False, server_default=text("0")),
        Column("parts_cost", MONEY, nullable=False, server_default=text("0")),
        Column("started_at", STAMP, nullable=True),
        Column("completed_at", STAMP, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "repair_order_number", name="uq_warranty_repair_order_number"),
        CheckConstraint(f"status IN {REPAIR_STATUSES!r}", name="ck_warranty_repair_status"),
        CheckConstraint("labor_cost >= 0 AND parts_cost >= 0", name="ck_warranty_repair_costs"),
        _fk("warranty_claim_id", "extensions", "warranty_claim", nullable=True),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location", nullable=True),
    ],
    indexes=[("claim", "warranty_claim_id"), ("status", "status")],
)

customer_part = mk(
    "customer_part",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("external_customer_ref", Text, nullable=False),
        Column("customer_part_number", Text, nullable=False),
        Column("description", Text, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "external_customer_ref", "customer_part_number", name="uq_customer_part_number"),
        UniqueConstraint("tenant_id", "item_id", "external_customer_ref", name="uq_customer_part_item_customer"),
        CheckConstraint("customer_part_number <> ''", name="ck_customer_part_number"),
        _fk("item_id", "item_master", "item"),
    ],
    indexes=[("item", "item_id"), ("part_number", "customer_part_number")],
)

upc_registry = mk(
    "upc_registry",
    [
        Column("item_id", UUID_T, nullable=True),
        Column("upc_code", Text, nullable=False),
        Column("assignment_date", Date, nullable=False, server_default=text("CURRENT_DATE")),
        Column("retirement_date", Date, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'ASSIGNED'")),
        Column("assignment_source", Text, nullable=False, server_default=text("'AUTO'")),
        Column("checksum_valid", Boolean, nullable=False, server_default=text("true")),
        Column("retirement_reason", Text, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "upc_code", name="uq_upc_registry_code"),
        CheckConstraint("length(upc_code) = 12 AND upc_code ~ '^[0-9]+$'", name="ck_upc_registry_format"),
        CheckConstraint(f"status IN {UPC_STATUSES!r}", name="ck_upc_registry_status"),
        CheckConstraint(f"assignment_source IN {UPC_SOURCES!r}", name="ck_upc_registry_source"),
        CheckConstraint("retirement_date IS NULL OR retirement_date >= assignment_date", name="ck_upc_registry_dates"),
        CheckConstraint("status <> 'RETIRED' OR retirement_date IS NOT NULL", name="ck_upc_registry_retired_date"),
        _fk("item_id", "item_master", "item", nullable=True),
    ],
    indexes=[("status", "status"), ("item", "item_id")],
)
Index(
    "uq_upc_registry_assigned_item",
    upc_registry.c.tenant_id,
    upc_registry.c.item_id,
    unique=True,
    postgresql_where=text("status = 'ASSIGNED'"),
)

REVISION_0028_TABLES: tuple[Table, ...] = (
    item_warranty,
    warranty_registration,
    warranty_claim,
    warranty_claim_action,
    warranty_repair_order,
    customer_part,
    upc_registry,
)
ALL_TABLES = REVISION_0028_TABLES
