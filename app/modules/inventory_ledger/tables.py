"""SQLAlchemy Core metadata for the 22 `inventory_ledger` tables (FS-04, DATA-MODEL §7).

Migrations 0008–0010 compile this metadata into DDL (transaction/line/detail are partitioned in the
migration; the metadata mirrors the columns for query use). Repositories use async SQLAlchemy Core only.
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
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="inventory_ledger", naming_convention=NAMING_CONVENTION)
UUID_T = UUID(as_uuid=True)
QTY = Numeric(38, 12)
MONEY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

TRANSACTION_TYPES = tuple(f"T-{number:02d}" for number in range(1, 31))
MOVEMENT_TYPES = (
    "RECEIPT", "VENDOR_RETURN", "ADJUSTMENT", "WORKSHEET", "TRANSFER", "TO_FULFILLMENT", "TO_RECEIPT",
    "FULFILLMENT", "RETURN", "KIT_FULFILLMENT", "ASSEMBLY_BUILD", "ASSEMBLY_UNBUILD", "WIP_ISSUE",
    "WIP_COMPLETION", "COUNT_VARIANCE", "CONSIGNED_RECEIPT", "CONSIGNED_OWNERSHIP", "CONSIGNED_ADJUST",
    "STANDARD_REVALUATION", "COGS_ADJUST", "LANDED_COST", "LANDED_COST_EST", "DROP_SHIP", "BIN_TRANSFER",
    "STATUS_CHANGE", "COST_ONLY", "REVALUATION", "VARIANCE", "BOOTSTRAP_DISTRIBUTION", "REVERSAL",
)
DOC_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "POSTED", "REVERSED", "VOID", "CANCELLED", "PARTIALLY_FULFILLED", "FULFILLED", "PARTIALLY_RECEIVED", "RECEIVED", "CLOSED")


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


def mk(name: str, columns: list, *, extra: list | None = None, audit: bool = True, id_pk: bool = True) -> Table:
    cols: list = []
    if id_pk:
        cols.append(Column("id", UUID_T, primary_key=True, nullable=False))
    cols.append(Column("tenant_id", UUID_T, nullable=False))
    cols.extend(columns)
    if audit:
        _audit(cols)
    constraints: list = list(extra or [])
    constraints.append(_tenant_fk(name))
    if id_pk:
        constraints.append(UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"))
    return Table(name, metadata, *cols, *constraints)


# --------------------------------------------------------------------------- postings ----------
inventory_transaction = mk(
    "inventory_transaction",
    [
        Column("transaction_type", Text, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("posting_at", STAMP, nullable=False, server_default=text("now()")),
        Column("ledger_sequence", Integer, nullable=False),
        Column("source_doc_type", Text, nullable=True),
        Column("source_doc_id", UUID_T, nullable=True),
        Column("external_ref", Text, nullable=True),
        Column("reversal_of_transaction_id", UUID_T, nullable=True),
        Column("reversal_reason", Text, nullable=True),
        Column("fiscal_period_id", UUID_T, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", QTY, nullable=False, server_default=text("1")),
        Column("total_qty_base", QTY, nullable=False, server_default=text("0")),
        Column("total_value_base", MONEY, nullable=False, server_default=text("0")),
        Column("is_backdated", Boolean, nullable=False, server_default=text("false")),
        Column("period_shifted", Boolean, nullable=False, server_default=text("false")),
        Column("cost_estimated", Boolean, nullable=False, server_default=text("false")),
        Column("backdated_original_date", Date, nullable=True),
        Column("idempotency_key", Text, nullable=True),
        Column("import_batch_id", UUID_T, nullable=True),
        Column("correlation_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "ledger_sequence", name="uq_txn_ledger_sequence"),
        CheckConstraint("transaction_type IN (" + ",".join(f"'{value}'" for value in TRANSACTION_TYPES) + ")", name="ck_txn_type"),
        Index("ix_txn_date", "tenant_id", "transaction_date"),
        Index("ix_txn_source", "tenant_id", "source_doc_type", "source_doc_id"),
        Index(
            "uq_txn_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_txn_reversal",
            "tenant_id",
            "reversal_of_transaction_id",
            unique=True,
            postgresql_where=text("reversal_of_transaction_id IS NOT NULL"),
        ),
    ],
)

inventory_transaction_line = mk(
    "inventory_transaction_line",
    [
        Column("inventory_transaction_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_class", Text, nullable=False, server_default=text("'ON_HAND'")),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("uom_id", UUID_T, nullable=True),
        Column("qty_uom", QTY, nullable=False),
        Column("conversion_factor", QTY, nullable=False, server_default=text("1")),
        Column("qty_base", QTY, nullable=False),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("value_txn", MONEY, nullable=False, server_default=text("0")),
        Column("unit_cost", MONEY, nullable=True),
        Column("movement_type", Text, nullable=False),
        Column("movement_direction", Text, nullable=False, server_default=text("'IN'")),
        Column("reason_code_id", UUID_T, nullable=True),
        Column("source_line_ref", Text, nullable=True),
        Column("class_ref", Text, nullable=True),
        Column("department_ref", Text, nullable=True),
        Column("subsidiary_ref", Text, nullable=True),
        Column("cost_object_id", UUID_T, nullable=True),
        Column("cost_estimated", Boolean, nullable=False, server_default=text("false")),
        Column("transaction_date", Date, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "inventory_transaction_id", "line_number", name="uq_line_number"),
        ForeignKeyConstraint(["tenant_id", "inventory_transaction_id"], ["inventory_ledger.inventory_transaction.tenant_id", "inventory_ledger.inventory_transaction.id"], name="fk_line_txn"),
        CheckConstraint("qty_class IN ('ON_HAND','IN_TRANSIT')", name="ck_line_qty_class"),
        CheckConstraint("ownership IN ('OWNED','CONSIGNED','IN_TRANSIT')", name="ck_line_ownership"),
        CheckConstraint("movement_type IN (" + ",".join(f"'{value}'" for value in MOVEMENT_TYPES) + ")", name="ck_line_movement_type"),
        Index("ix_line_txn", "tenant_id", "inventory_transaction_id"),
        Index("ix_line_item_date", "tenant_id", "item_id", "transaction_date"),
        Index("ix_line_location_item", "tenant_id", "location_id", "item_id"),
    ],
)

inventory_transaction_detail = mk(
    "inventory_transaction_detail",
    [
        Column("inventory_transaction_line_id", UUID_T, nullable=False),
        Column("inventory_transaction_id", UUID_T, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_base", QTY, nullable=False),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "inventory_transaction_line_id"], ["inventory_ledger.inventory_transaction_line.tenant_id", "inventory_ledger.inventory_transaction_line.id"], name="fk_detail_line"),
        Index("ix_detail_txn", "tenant_id", "inventory_transaction_id"),
        Index("ix_detail_bin", "tenant_id", "bin_id"),
        Index("ix_detail_lot", "tenant_id", "lot_id"),
        Index("ix_detail_serial", "tenant_id", "serial_id"),
    ],
)

# --------------------------------------------------------------------------- balances ----------
inventory_balance = mk(
    "inventory_balance",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_class", Text, nullable=False, server_default=text("'ON_HAND'")),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("qty_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("qty_committed", QTY, nullable=False, server_default=text("0")),
        Column("qty_on_order", QTY, nullable=False, server_default=text("0")),
        Column("qty_backordered", QTY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("last_ledger_sequence", Integer, nullable=True),
        Column("last_posting_at", STAMP, nullable=True),
        Column("qty_available", QTY, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "location_id", "inventory_status_id", "ownership", "qty_class",
            "lot_id", "serial_id", name="uq_balance_key", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("qty_committed >= 0", name="ck_balance_committed"),
        CheckConstraint("qty_on_hand >= 0", name="ck_balance_on_hand"),
        CheckConstraint("serial_id IS NULL OR qty_on_hand <= 1", name="ck_balance_serial"),
        Index("ix_balance_position", "tenant_id", "item_id", "location_id", "inventory_status_id"),
        Index("ix_balance_location", "tenant_id", "location_id", "item_id"),
    ],
)

inventory_balance_bin = mk(
    "inventory_balance_bin",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("qty_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("last_ledger_sequence", Integer, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "location_id", "bin_id", "inventory_status_id", "ownership",
            "lot_id", "serial_id", name="uq_balance_bin_key", postgresql_nulls_not_distinct=True,
        ),
        Index("ix_balance_bin_bin", "tenant_id", "bin_id"),
        Index("ix_balance_bin_item", "tenant_id", "item_id", "location_id"),
    ],
)

inventory_balance_lot = mk(
    "inventory_balance_lot",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("lot_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_on_hand", QTY, nullable=False, server_default=text("0")),
        Column("qty_committed", QTY, nullable=False, server_default=text("0")),
        Column("expiry_date", Date, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id", "item_id", "location_id", "lot_id", "inventory_status_id", "ownership",
            name="uq_balance_lot_key",
        ),
        Index("ix_balance_lot_lot", "tenant_id", "lot_id"),
        Index("ix_balance_lot_expiry", "tenant_id", "item_id", "expiry_date"),
    ],
)

# --------------------------------------------------------------------------- commitments ------
inventory_commitment = mk(
    "inventory_commitment",
    [
        Column("source_type", Text, nullable=False),
        Column("source_line_ref", Text, nullable=False),
        Column("external_ref", Text, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_committed", QTY, nullable=False),
        Column("qty_fulfilled", QTY, nullable=False, server_default=text("0")),
        Column("qty_backordered", QTY, nullable=False, server_default=text("0")),
        Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
        Column("priority", Integer, nullable=False, server_default=text("100")),
        Column("required_by_date", Date, nullable=True),
        Column("committed_at", STAMP, nullable=False, server_default=text("now()")),
        Column("released_at", STAMP, nullable=True),
        Column("released_qty", QTY, nullable=False, server_default=text("0")),
    ],
    extra=[
        CheckConstraint("source_type IN ('SALES_ORDER','WORK_ORDER','TRANSFER_ORDER','OTHER')", name="ck_commitment_source_type"),
        CheckConstraint("status IN ('ACTIVE','PARTIALLY_FULFILLED','FULFILLED','RELEASED')", name="ck_commitment_status"),
        CheckConstraint("qty_committed > 0", name="ck_commitment_qty"),
        CheckConstraint("qty_fulfilled <= qty_committed", name="ck_commitment_fulfilled"),
        UniqueConstraint("tenant_id", "source_type", "source_line_ref", name="uq_commitment_source"),
        Index("ix_commitment_item", "tenant_id", "item_id", "location_id", "status"),
    ],
)

inventory_commitment_allocation = mk(
    "inventory_commitment_allocation",
    [
        Column("commitment_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("allocation_seq", Integer, nullable=False),
        Column("released_at", STAMP, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "commitment_id"], ["inventory_ledger.inventory_commitment.tenant_id", "inventory_ledger.inventory_commitment.id"], name="fk_alloc_commitment"),
        UniqueConstraint("tenant_id", "commitment_id", "allocation_seq", name="pk_alloc"),
        CheckConstraint("qty > 0", name="ck_alloc_qty"),
        Index("ix_alloc_lot", "tenant_id", "lot_id"),
    ],
    id_pk=False,
)

# --------------------------------------------------------------------------- transfers ---------
def _common_doc() -> list:
    return [
        Column("document_number", Text, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("transaction_date", Date, nullable=True),
        Column("memo", Text, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ]

inventory_transfer = mk(
    "inventory_transfer",
    [
        *_common_doc(),
        Column("from_location_id", UUID_T, nullable=False),
        Column("to_location_id", UUID_T, nullable=False),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_inventory_transfer_number"),
        CheckConstraint("from_location_id <> to_location_id", name="ck_transfer_from_to"),
        Index("ix_transfer_from", "tenant_id", "from_location_id", "transaction_date"),
        Index("ix_transfer_to", "tenant_id", "to_location_id"),
    ],
)

inventory_transfer_line = mk(
    "inventory_transfer_line",
    [
        Column("inventory_transfer_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("bin_from_id", UUID_T, nullable=True),
        Column("bin_to_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "inventory_transfer_id"], ["inventory_ledger.inventory_transfer.tenant_id", "inventory_ledger.inventory_transfer.id"], name="fk_transfer_line_header"),
        CheckConstraint("qty > 0", name="ck_transfer_qty"),
        UniqueConstraint("tenant_id", "inventory_transfer_id", "line_number", name="uq_transfer_line"),
        Index("ix_transfer_line_item", "tenant_id", "item_id"),
    ],
)

transfer_order = mk(
    "transfer_order",
    [
        *_common_doc(),
        Column("from_location_id", UUID_T, nullable=False),
        Column("to_location_id", UUID_T, nullable=False),
        Column("fulfilment_status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("receipt_status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("in_transit_ownership", Text, nullable=False, server_default=text("'SOURCE'")),
        Column("transfer_price", MONEY, nullable=True),
        Column("approved_at", STAMP, nullable=True),
        Column("closed_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_transfer_order_number"),
        CheckConstraint("from_location_id <> to_location_id", name="ck_to_from_to"),
        Index("ix_to_from_status", "tenant_id", "from_location_id", "status"),
        Index("ix_to_to_status", "tenant_id", "to_location_id", "status"),
    ],
)

transfer_order_line = mk(
    "transfer_order_line",
    [
        Column("transfer_order_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("qty_requested", QTY, nullable=False),
        Column("qty_committed", QTY, nullable=False, server_default=text("0")),
        Column("qty_fulfilled", QTY, nullable=False, server_default=text("0")),
        Column("qty_in_transit", QTY, nullable=False, server_default=text("0")),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("qty_closed", QTY, nullable=False, server_default=text("0")),
        Column("bin_from_id", UUID_T, nullable=True),
        Column("bin_to_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_ids", ARRAY(UUID_T), nullable=True),
        Column("required_by_date", Date, nullable=True),
        Column("is_closed", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "transfer_order_id"], ["inventory_ledger.transfer_order.tenant_id", "inventory_ledger.transfer_order.id"], name="fk_to_line_header"),
        UniqueConstraint("tenant_id", "transfer_order_id", "line_number", name="uq_to_line"),
        CheckConstraint("qty_requested > 0", name="ck_to_qty_requested"),
        CheckConstraint("qty_fulfilled <= qty_requested", name="ck_to_fulfilled"),
        CheckConstraint("qty_received <= qty_fulfilled", name="ck_to_received"),
        Index("ix_to_line_item", "tenant_id", "item_id", "required_by_date"),
    ],
)

inventory_in_transit = mk(
    "inventory_in_transit",
    [
        Column("transfer_order_line_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("from_location_id", UUID_T, nullable=False),
        Column("to_location_id", UUID_T, nullable=False),
        Column("qty_in_transit", QTY, nullable=False, server_default=text("0")),
        Column("value_base", MONEY, nullable=False, server_default=text("0")),
        Column("fulfilled_at", STAMP, nullable=True),
        Column("expected_receipt_date", Date, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'IN_TRANSIT'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "transfer_order_line_id", name="uq_in_transit_line"),
        CheckConstraint("qty_in_transit >= 0", name="ck_in_transit_qty"),
        CheckConstraint("value_base >= 0", name="ck_in_transit_value"),
        Index("ix_in_transit_to", "tenant_id", "to_location_id", "status"),
        Index("ix_in_transit_item", "tenant_id", "item_id"),
    ],
)

# --------------------------------------------------------------------------- adjustments -------
inventory_adjustment = mk(
    "inventory_adjustment",
    [
        *_common_doc(),
        Column("reason_code_id", UUID_T, nullable=True),
        Column("adjustment_account_ref", Text, nullable=True),
        Column("adjustment_source", Text, nullable=False, server_default=text("'MANUAL'")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_inventory_adjustment_number"),
        CheckConstraint("adjustment_source IN ('MANUAL','IMPORT','COUNT','CONSIGNED')", name="ck_adj_source"),
        Index("ix_adj_date", "tenant_id", "transaction_date"),
    ],
)

inventory_adjustment_line = mk(
    "inventory_adjustment_line",
    [
        Column("inventory_adjustment_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("uom_id", UUID_T, nullable=True),
        Column("qty_delta", QTY, nullable=False),
        Column("unit_cost", MONEY, nullable=True),
        Column("value_delta", MONEY, nullable=True),
        Column("reason_code_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "inventory_adjustment_id"], ["inventory_ledger.inventory_adjustment.tenant_id", "inventory_ledger.inventory_adjustment.id"], name="fk_adj_line_header"),
        UniqueConstraint("tenant_id", "inventory_adjustment_id", "line_number", name="uq_adj_line"),
        CheckConstraint("qty_delta <> 0", name="ck_adj_qty_delta"),
        Index("ix_adj_line_item", "tenant_id", "item_id"),
    ],
)

inventory_worksheet = mk(
    "inventory_worksheet",
    [
        *_common_doc(),
        Column("worksheet_type", Text, nullable=False, server_default=text("'ABSOLUTE'")),
        Column("default_account_ref", Text, nullable=True),
        Column("count_date", Date, nullable=True),
    ],
    extra=[UniqueConstraint("tenant_id", "document_number", name="uq_inventory_worksheet_number")],
)

inventory_worksheet_line = mk(
    "inventory_worksheet_line",
    [
        Column("inventory_worksheet_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("system_qty", QTY, nullable=False, server_default=text("0")),
        Column("new_qty", QTY, nullable=False),
        Column("variance_qty", QTY, nullable=False, server_default=text("0")),
        Column("unit_cost", MONEY, nullable=True),
        Column("reason_code_id", UUID_T, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "inventory_worksheet_id"], ["inventory_ledger.inventory_worksheet.tenant_id", "inventory_ledger.inventory_worksheet.id"], name="fk_ws_line_header"),
        UniqueConstraint("tenant_id", "inventory_worksheet_id", "line_number", name="uq_ws_line"),
        CheckConstraint("new_qty >= 0", name="ck_ws_new_qty"),
        Index("ix_ws_line_item", "tenant_id", "item_id"),
    ],
)

inventory_distribution = mk(
    "inventory_distribution",
    [
        *_common_doc(),
        Column("distribution_mode", Text, nullable=False, server_default=text("'SIMPLE'")),
        Column("bootstrap_mode", Boolean, nullable=False, server_default=text("true")),
        Column("posted_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_inventory_distribution_number"),
        CheckConstraint("distribution_mode IN ('SIMPLE','MANUAL')", name="ck_dist_mode"),
        Index("ix_dist_status", "tenant_id", "status"),
    ],
)

inventory_distribution_line = mk(
    "inventory_distribution_line",
    [
        Column("inventory_distribution_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("to_location_id", UUID_T, nullable=False),
        Column("inventory_status_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("unit_cost", MONEY, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "inventory_distribution_id"], ["inventory_ledger.inventory_distribution.tenant_id", "inventory_ledger.inventory_distribution.id"], name="fk_dist_line_header"),
        UniqueConstraint("tenant_id", "inventory_distribution_id", "line_number", name="uq_dist_line"),
        CheckConstraint("qty > 0", name="ck_dist_qty"),
        Index("ix_dist_line_item", "tenant_id", "item_id"),
    ],
)

inventory_reason_code = mk(
    "inventory_reason_code",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("reason_type", Text, nullable=False),
        Column("account_ref", Text, nullable=True),
        Column("is_system", Boolean, nullable=False, server_default=text("false")),
        Column("is_active", Boolean, nullable=False, server_default=text("true")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "reason_type", "code", name="uq_reason_code"),
        CheckConstraint("reason_type IN ('ADJUSTMENT','COUNT','SCRAP','RETURN','STATUS_CHANGE','TRANSFER')", name="ck_reason_type"),
        Index("ix_reason_active", "tenant_id", "reason_type", "is_active"),
    ],
)

import_batch = mk(
    "import_batch",
    [
        Column("import_type", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'UPLOADED'")),
        Column("filename", Text, nullable=True),
        Column("storage_key", Text, nullable=True),
        Column("row_count", Integer, nullable=False, server_default=text("0")),
        Column("success_count", Integer, nullable=False, server_default=text("0")),
        Column("error_count", Integer, nullable=False, server_default=text("0")),
        Column("idempotency_key", Text, nullable=False),
        Column("started_at", STAMP, nullable=True),
        Column("finished_at", STAMP, nullable=True),
        Column("options", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_import_idem"),
        CheckConstraint("status IN ('UPLOADED','VALIDATING','VALIDATED','POSTING','POSTED','PARTIAL','FAILED')", name="ck_import_status"),
        CheckConstraint("import_type IN ('OPENING_BALANCE','ADJUSTMENT','COUNT','ITEM','PRICE','OPENING_SERIAL')", name="ck_import_type"),
        Index("ix_import_status", "tenant_id", "status", "created_at"),
    ],
)

import_batch_row = mk(
    "import_batch_row",
    [
        Column("import_batch_id", UUID_T, nullable=False),
        Column("row_number", Integer, nullable=False),
        Column("raw_row", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("error_code", Text, nullable=True),
        Column("error_detail", Text, nullable=True),
        Column("entity_type", Text, nullable=True),
        Column("entity_id", UUID_T, nullable=True),
        Column("inventory_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        ForeignKeyConstraint(["tenant_id", "import_batch_id"], ["inventory_ledger.import_batch.tenant_id", "inventory_ledger.import_batch.id"], name="fk_import_row_batch"),
        UniqueConstraint("tenant_id", "import_batch_id", "row_number", name="uq_import_row"),
        CheckConstraint("status IN ('PENDING','VALID','INVALID','POSTED','FAILED')", name="ck_import_row_status"),
        Index("ix_import_row_status", "tenant_id", "import_batch_id", "status"),
    ],
)

REVISION_0008_TABLES: tuple[Table, ...] = (
    inventory_transaction, inventory_transaction_line, inventory_transaction_detail,
    inventory_commitment, inventory_commitment_allocation,
    inventory_transfer, inventory_transfer_line, transfer_order, transfer_order_line, inventory_in_transit,
    inventory_adjustment, inventory_adjustment_line, inventory_worksheet, inventory_worksheet_line,
    inventory_distribution, inventory_distribution_line, inventory_reason_code, import_batch, import_batch_row,
)
REVISION_0010_TABLES: tuple[Table, ...] = (inventory_balance, inventory_balance_bin, inventory_balance_lot)
ALL_TABLES: tuple[Table, ...] = REVISION_0008_TABLES + REVISION_0010_TABLES

assert len(ALL_TABLES) == 22
