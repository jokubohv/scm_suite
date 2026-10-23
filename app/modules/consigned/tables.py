"""SQLAlchemy Core metadata for the 5 ``consigned`` tables (FS-13, DATA-MODEL §15).

Migration ``0027_consigned`` compiles this metadata into explicit PostgreSQL DDL. Consigned positions are
FS-04 ledger positions with ``ownership=CONSIGNED``; FS-13 never writes inventory/cost ledgers.
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

metadata = MetaData(schema="consigned", naming_convention=NAMING_CONVENTION)
UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

AGREEMENT_STATUSES = ("DRAFT", "ACTIVE", "SUSPENDED", "TERMINATED")
PAYMENT_TRIGGERS = ("ON_CONSUMPTION", "ON_SALE")
TRANSFER_TYPES = ("RECEIPT_TO_CONSIGNED", "CONSUME_ON_SALE", "RETURN_TO_VENDOR", "ADJUST")
TRANSFER_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "POSTED", "REVERSED", "VOID")
OWNERSHIPS = ("OWNED", "CONSIGNED")


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


consignment_agreement = mk(
    "consignment_agreement",
    [
        Column("agreement_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("start_date", Date, nullable=False),
        Column("end_date", Date, nullable=True),
        Column("payment_trigger", Text, nullable=False, server_default=text("'ON_SALE'")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("terms", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("approved_by", UUID_T, nullable=True),
        Column("approved_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "agreement_number", name="uq_consignment_agreement_number"),
        CheckConstraint(f"status IN {AGREEMENT_STATUSES!r}", name="ck_consignment_agreement_status"),
        CheckConstraint(f"payment_trigger IN {PAYMENT_TRIGGERS!r}", name="ck_consignment_agreement_trigger"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_consignment_agreement_dates"),
        ForeignKeyConstraint(["currency_code"], ["platform.currency.code"], name="fk_consignment_agreement_currency"),
        _fk("vendor_id", "procurement", "vendor"),
    ],
    indexes=[("vendor_status", "vendor_id", "status"), ("end_date", "end_date")],
)

consignment_agreement_item = mk(
    "consignment_agreement_item",
    [
        Column("consignment_agreement_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("consigned_status_id", UUID_T, nullable=False),
        Column("owned_status_id", UUID_T, nullable=False),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("min_qty", QTY, nullable=False, server_default=text("0")),
        Column("max_qty", QTY, nullable=True),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "consignment_agreement_id",
            "item_id",
            "location_id",
            name="uq_consignment_agreement_item",
        ),
        CheckConstraint("unit_cost >= 0", name="ck_consignment_agreement_item_cost"),
        CheckConstraint("min_qty >= 0", name="ck_consignment_agreement_item_min"),
        CheckConstraint("max_qty IS NULL OR max_qty >= min_qty", name="ck_consignment_agreement_item_max"),
        _fk("consignment_agreement_id", "consigned", "consignment_agreement"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("consigned_status_id", "traceability", "inventory_status"),
        _fk("owned_status_id", "traceability", "inventory_status"),
    ],
    indexes=[("item", "item_id"), ("location", "location_id")],
)

consignment_ownership_transfer = mk(
    "consignment_ownership_transfer",
    [
        Column("document_number", Text, nullable=False),
        Column("consignment_agreement_id", UUID_T, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("transfer_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("transaction_date", Date, nullable=False),
        Column("external_ref", Text, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("vendor_bill_id", UUID_T, nullable=True),
        Column("reversal_of_transfer_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
        Column("approved_by", UUID_T, nullable=True),
        Column("approved_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_consignment_transfer_number"),
        CheckConstraint(f"transfer_type IN {TRANSFER_TYPES!r}", name="ck_consignment_transfer_type"),
        CheckConstraint(f"status IN {TRANSFER_STATUSES!r}", name="ck_consignment_transfer_status"),
        UniqueConstraint(
            "tenant_id",
            "posted_transaction_id",
            name="uq_consignment_transfer_posted",
            postgresql_nulls_not_distinct=True,
        ),
        _fk("consignment_agreement_id", "consigned", "consignment_agreement"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("vendor_bill_id", "procurement", "vendor_bill", nullable=True),
        _fk("reversal_of_transfer_id", "consigned", "consignment_ownership_transfer", nullable=True),
    ],
    indexes=[
        ("vendor_date", "vendor_id", "transaction_date"),
        ("status", "status"),
        ("agreement", "consignment_agreement_id"),
    ],
)

consignment_ownership_transfer_line = mk(
    "consignment_ownership_transfer_line",
    [
        Column("consignment_ownership_transfer_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("from_ownership", Text, nullable=False),
        Column("to_ownership", Text, nullable=False),
        Column("from_status_id", UUID_T, nullable=True),
        Column("to_status_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("bin_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "consignment_ownership_transfer_id", "line_number", name="uq_consignment_transfer_line"
        ),
        CheckConstraint(
            f"from_ownership IN {OWNERSHIPS!r} AND to_ownership IN {OWNERSHIPS!r}", name="ck_consignment_line_ownership"
        ),
        CheckConstraint("from_ownership <> to_ownership", name="ck_consignment_line_ownership_pair"),
        CheckConstraint("qty > 0", name="ck_consignment_line_qty"),
        CheckConstraint("unit_cost >= 0", name="ck_consignment_line_cost"),
        _fk("consignment_ownership_transfer_id", "consigned", "consignment_ownership_transfer"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("bin_id", "traceability", "bin", nullable=True),
    ],
    indexes=[("item", "item_id"), ("lot", "lot_id")],
)

consignment_consumption = mk(
    "consignment_consumption",
    [
        Column("consignment_agreement_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("consumption_date", Date, nullable=False),
        Column("period_key", Text, nullable=False),
        Column("qty_consumed", QTY, nullable=False),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("source_transfer_id", UUID_T, nullable=True),
        Column("source_transfer_line_id", UUID_T, nullable=True),
        Column("vendor_bill_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "source_transfer_line_id",
            name="uq_consignment_consumption_source",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("qty_consumed > 0", name="ck_consignment_consumption_qty"),
        CheckConstraint("period_key ~ '^[0-9]{4}-[0-9]{2}$'", name="ck_consignment_consumption_period"),
        _fk("consignment_agreement_id", "consigned", "consignment_agreement"),
        _fk("item_id", "item_master", "item"),
        _fk("location_id", "traceability", "location"),
        _fk("source_transfer_id", "consigned", "consignment_ownership_transfer", nullable=True),
        _fk("source_transfer_line_id", "consigned", "consignment_ownership_transfer_line", nullable=True),
        _fk("vendor_bill_id", "procurement", "vendor_bill", nullable=True),
    ],
    indexes=[
        ("agreement_period", "consignment_agreement_id", "period_key"),
        ("item_date", "item_id", "consumption_date"),
    ],
)

REVISION_0027_TABLES: tuple[Table, ...] = (
    consignment_agreement,
    consignment_agreement_item,
    consignment_ownership_transfer,
    consignment_ownership_transfer_line,
    consignment_consumption,
)
ALL_TABLES = REVISION_0027_TABLES
