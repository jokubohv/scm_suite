"""SQLAlchemy Core metadata for the 29 ``procurement`` tables (FS-08, DATA-MODEL §10).

Migrations ``0015_procurement_vendors`` / ``0016_procurement_ordering`` / ``0017_procurement_billing``
compile this metadata into explicit PostgreSQL DDL; module services query it with async Core or raw SQL.
No ORM imports: cross-context references use composite tenant FKs by table name (item.master,
traceability.registry, platform.*) and all effects on ledger/costing go through the frozen FS-04/FS-05
contracts (PostingPort), never direct writes.

Deferred FKs closed by the procurement migrations:

* ``item_master.item_purchasing.preferred_vendor_id`` → ``procurement.vendor`` (0015);
* ``costing.landed_cost.vendor_id`` → ``procurement.vendor``; ``costing.landed_cost_allocation.
  item_receipt_line_id`` → ``procurement.item_receipt_line``; ``costing.cost_variance.vendor_bill_line_id``
  → ``procurement.vendor_bill_line`` (0017).
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

metadata = MetaData(schema="procurement", naming_convention=NAMING_CONVENTION)

UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)

PO_TYPES = ("STANDARD", "BLANKET", "DROP_SHIP", "SPECIAL_ORDER")
PO_STATUSES = (
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "PENDING_RECEIPT",
    "PARTIALLY_RECEIVED",
    "RECEIVED",
    "BILLED",
    "CLOSED",
    "CANCELLED",
    "VOID",
)
REQUISITION_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "ORDERED", "REJECTED", "CANCELLED")
REQUEST_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "ORDERED", "CANCELLED")
RFQ_STATUSES = ("DRAFT", "OPEN", "CLOSED", "AWARDED", "CANCELLED")
RFQ_RESPONSE_STATUSES = ("RECEIVED", "UNDER_REVIEW", "AWARDED", "REJECTED")
CONTRACT_TYPES = ("CONTRACT", "BLANKET_ORDER")
CONTRACT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "ACTIVE", "EXPIRED", "CLOSED", "CANCELLED")
RECEIPT_TYPES = ("PO", "INBOUND_SHIPMENT", "TRANSFER", "RA", "STANDALONE")
RECEIPT_STATUSES = ("DRAFT", "PENDING_POSTING", "POSTED", "REVERSED", "VOID")
BILL_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "OPEN", "PAID", "CANCELLED", "VOID")
CREDIT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "OPEN", "APPLIED", "CANCELLED", "VOID")
RETURN_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "SHIPPED", "CREDITED", "CLOSED", "CANCELLED")
MATCH_STATUSES = ("PENDING", "MATCHED", "EXCEPTION", "OVERRIDDEN")
VARIANCE_TYPES = ("QUANTITY", "PRICE", "EXCHANGE_RATE")
LINK_TYPES = ("DROP_SHIP", "SPECIAL_ORDER")
LINK_STATUSES = ("OPEN", "FULFILLED", "CANCELLED")


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


def _self_fk(local: str, ref_table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", local], [f"{ref_table}.tenant_id", f"{ref_table}.id"], name=f"fk_{ref_table}_{local}"
    )


# -------------------------------------------------------------------------------------------------
# 0015_procurement_vendors — vendor master, categories, item-vendor associations/pricing, contracts
# -------------------------------------------------------------------------------------------------
vendor_category = mk(
    "vendor_category",
    [
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "code", name="uq_vendor_category_code"),
        CheckConstraint("code <> ''", name="ck_vendor_category_code"),
    ],
    indexes=[("inactive", "is_inactive")],
)

vendor = mk(
    "vendor",
    [
        Column("vendor_number", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("vendor_category_id", UUID_T, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("payment_terms", Text, nullable=True),
        Column("tax_id", Text, nullable=True),
        Column("is_1099_eligible", Boolean, nullable=False, server_default=text("false")),
        Column("credit_limit", MONEY, nullable=True),
        Column("default_expense_account_ref", Text, nullable=True),
        Column("default_payable_account_ref", Text, nullable=True),
        Column("addresses", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
        Column("primary_contact", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
        Column("inactive_at", STAMP, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_number", name="uq_vendor_number"),
        UniqueConstraint("tenant_id", "name", name="uq_vendor_name"),
        CheckConstraint("credit_limit IS NULL OR credit_limit >= 0", name="ck_vendor_credit_limit"),
        CheckConstraint("currency_code <> ''", name="ck_vendor_currency"),
        ForeignKeyConstraint(["currency_code"], ["platform.currency.code"], name="fk_vendor_currency"),
        _self_fk("vendor_category_id", "vendor_category")
        if False
        else _fk("vendor_category_id", "procurement", "vendor_category"),
    ],
    indexes=[("inactive", "is_inactive"), ("category", "vendor_category_id")],
)

item_vendor = mk(
    "item_vendor",
    [
        Column("item_id", UUID_T, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("vendor_item_code", Text, nullable=True),
        Column("is_preferred", Boolean, nullable=False, server_default=text("false")),
        Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
        Column("min_order_qty", QTY, nullable=False, server_default=text("0")),
        Column("purchase_uom_id", UUID_T, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("is_inactive", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_id", "vendor_id", name="uq_item_vendor"),
        CheckConstraint("min_order_qty >= 0", name="ck_item_vendor_moq"),
        CheckConstraint("lead_time_days >= 0", name="ck_item_vendor_lead_time"),
        _fk("item_id", "item_master", "item"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("purchase_uom_id", "item_master", "uom", nullable=True),
    ],
    indexes=[("vendor", "vendor_id")],
)
Index(
    "uq_item_vendor_preferred",
    item_vendor.c.tenant_id,
    item_vendor.c.item_id,
    unique=True,
    postgresql_where=text("is_preferred AND NOT is_inactive"),
)

item_vendor_price = mk(
    "item_vendor_price",
    [
        Column("item_vendor_id", UUID_T, nullable=False),
        Column("qty_min", QTY, nullable=False, server_default=text("0")),
        Column("qty_max", QTY, nullable=True),
        Column("unit_price", MONEY, nullable=False),
        Column("currency_code", Text, nullable=False),
        Column("effective_from", Date, nullable=False),
        Column("effective_to", Date, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "item_vendor_id",
            "currency_code",
            "qty_min",
            "effective_from",
            name="uq_item_vendor_price_key",
        ),
        CheckConstraint("unit_price >= 0", name="ck_item_vendor_price_price"),
        CheckConstraint("qty_min >= 0", name="ck_item_vendor_price_qty_min"),
        CheckConstraint("qty_max IS NULL OR qty_max >= qty_min", name="ck_item_vendor_price_qty_max"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_item_vendor_price_dates"),
        _fk("item_vendor_id", "procurement", "item_vendor"),
    ],
    indexes=[("item_vendor_effective", "item_vendor_id", "effective_from")],
)

vendor_contract = mk(
    "vendor_contract",
    [
        Column("contract_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("contract_type", Text, nullable=False, server_default=text("'CONTRACT'")),
        Column("start_date", Date, nullable=False),
        Column("end_date", Date, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("terms", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("total_committed_amount", MONEY, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "contract_number", name="uq_vendor_contract_number"),
        CheckConstraint(f"contract_type IN {CONTRACT_TYPES!r}", name="ck_vendor_contract_type"),
        CheckConstraint(f"status IN {CONTRACT_STATUSES!r}", name="ck_vendor_contract_status"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_vendor_contract_dates"),
        CheckConstraint(
            "total_committed_amount IS NULL OR total_committed_amount >= 0", name="ck_vendor_contract_total"
        ),
        _fk("vendor_id", "procurement", "vendor"),
    ],
    indexes=[("vendor_status", "vendor_id", "status"), ("end_date", "end_date")],
)

vendor_contract_item = mk(
    "vendor_contract_item",
    [
        Column("vendor_contract_id", UUID_T, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("contracted_qty", QTY, nullable=False),
        Column("used_qty", QTY, nullable=False, server_default=text("0")),
        Column("unit_price", MONEY, nullable=False),
        Column("discount_percent", MONEY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("delivery_schedule", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_contract_id", "item_id", name="uq_vendor_contract_item"),
        CheckConstraint("contracted_qty > 0", name="ck_contract_item_qty"),
        CheckConstraint("used_qty >= 0 AND used_qty <= contracted_qty", name="ck_contract_item_used"),
        CheckConstraint("unit_price >= 0", name="ck_contract_item_price"),
        _fk("vendor_contract_id", "procurement", "vendor_contract"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom", nullable=True),
    ],
    indexes=[("item", "item_id")],
)


# -------------------------------------------------------------------------------------------------
# 0016_procurement_ordering — requisitions/requests/RFQ/POs/releases
# -------------------------------------------------------------------------------------------------
purchase_requisition = mk(
    "purchase_requisition",
    [
        Column("document_number", Text, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("requested_by", UUID_T, nullable=False),
        Column("department_ref", Text, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("estimated_total", MONEY, nullable=False, server_default=text("0")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("memo", Text, nullable=True),
        Column("approval_request_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_requisition_number"),
        CheckConstraint(f"status IN {REQUISITION_STATUSES!r}", name="ck_requisition_status"),
        CheckConstraint("estimated_total >= 0", name="ck_requisition_total"),
    ],
    indexes=[("status_date", "status", "transaction_date"), ("requested_by", "requested_by")],
)

purchase_requisition_line = mk(
    "purchase_requisition_line",
    [
        Column("purchase_requisition_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("expense_account_ref", Text, nullable=True),
        Column("description", Text, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("estimated_unit_price", MONEY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("suggested_vendor_id", UUID_T, nullable=True),
        Column("purchase_order_line_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "purchase_requisition_id", "line_number", name="uq_requisition_line"),
        CheckConstraint("qty > 0", name="ck_requisition_line_qty"),
        CheckConstraint("estimated_unit_price >= 0", name="ck_requisition_line_price"),
        CheckConstraint("item_id IS NOT NULL OR description IS NOT NULL", name="ck_requisition_line_item_or_desc"),
        _fk("purchase_requisition_id", "procurement", "purchase_requisition"),
        _fk("item_id", "item_master", "item", nullable=True),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("suggested_vendor_id", "procurement", "vendor", nullable=True),
    ],
    indexes=[("item", "item_id")],
)

purchase_request = mk(
    "purchase_request",
    [
        Column("document_number", Text, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("requested_by", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("approved_at", STAMP, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_purchase_request_number"),
        CheckConstraint(f"status IN {REQUEST_STATUSES!r}", name="ck_purchase_request_status"),
    ],
    indexes=[("status", "status")],
)

purchase_request_line = mk(
    "purchase_request_line",
    [
        Column("purchase_request_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("expense_account_ref", Text, nullable=True),
        Column("description", Text, nullable=True),
        Column("qty", QTY, nullable=False),
        Column("anticipated_unit_price", MONEY, nullable=False, server_default=text("0")),
        Column("purchase_order_line_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "purchase_request_id", "line_number", name="uq_purchase_request_line"),
        CheckConstraint("qty > 0", name="ck_purchase_request_line_qty"),
        _fk("purchase_request_id", "procurement", "purchase_request"),
        _fk("item_id", "item_master", "item", nullable=True),
    ],
    indexes=[("item", "item_id")],
)

rfq = mk(
    "rfq",
    [
        Column("rfq_number", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("bid_open_date", Date, nullable=True),
        Column("bid_close_date", Date, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("awarded_response_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "rfq_number", name="uq_rfq_number"),
        CheckConstraint(f"status IN {RFQ_STATUSES!r}", name="ck_rfq_status"),
        CheckConstraint(
            "bid_close_date IS NULL OR bid_open_date IS NULL OR bid_close_date >= bid_open_date", name="ck_rfq_dates"
        ),
    ],
    indexes=[("status_close", "status", "bid_close_date")],
)

rfq_line = mk(
    "rfq_line",
    [
        Column("rfq_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("uom_id", UUID_T, nullable=True),
        Column("target_price", MONEY, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "rfq_id", "line_number", name="uq_rfq_line"),
        CheckConstraint("qty > 0", name="ck_rfq_line_qty"),
        _fk("rfq_id", "procurement", "rfq"),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom", nullable=True),
    ],
    indexes=[("item", "item_id")],
)

rfq_response = mk(
    "rfq_response",
    [
        Column("rfq_id", UUID_T, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'RECEIVED'")),
        Column("received_at", STAMP, nullable=False, server_default=text("now()")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("total_amount", MONEY, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "rfq_id", "vendor_id", name="uq_rfq_response"),
        CheckConstraint(f"status IN {RFQ_RESPONSE_STATUSES!r}", name="ck_rfq_response_status"),
        _fk("rfq_id", "procurement", "rfq"),
        _fk("vendor_id", "procurement", "vendor"),
    ],
    indexes=[("vendor", "vendor_id")],
)

rfq_response_line = mk(
    "rfq_response_line",
    [
        Column("rfq_response_id", UUID_T, nullable=False),
        Column("rfq_line_id", UUID_T, nullable=False),
        Column("unit_price", MONEY, nullable=False),
        Column("qty_quoted", QTY, nullable=False),
        Column("lead_time_days", Integer, nullable=False, server_default=text("0")),
        Column("is_awarded", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "rfq_response_id", "rfq_line_id", name="uq_rfq_response_line"),
        CheckConstraint("unit_price >= 0", name="ck_rfq_response_line_price"),
        CheckConstraint("qty_quoted > 0", name="ck_rfq_response_line_qty"),
        _fk("rfq_response_id", "procurement", "rfq_response"),
        _fk("rfq_line_id", "procurement", "rfq_line"),
    ],
    indexes=[("rfq_line", "rfq_line_id")],
)

purchase_order = mk(
    "purchase_order",
    [
        Column("document_number", Text, nullable=False),
        Column("transaction_date", Date, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("po_type", Text, nullable=False, server_default=text("'STANDARD'")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("ship_to_location_id", UUID_T, nullable=True),
        Column("bill_to", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("vendor_contract_id", UUID_T, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
        Column("subtotal", MONEY, nullable=False, server_default=text("0")),
        Column("total", MONEY, nullable=False, server_default=text("0")),
        Column("expected_receipt_date", Date, nullable=True),
        Column("approval_request_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_purchase_order_number"),
        CheckConstraint(f"status IN {PO_STATUSES!r}", name="ck_purchase_order_status"),
        CheckConstraint(f"po_type IN {PO_TYPES!r}", name="ck_purchase_order_type"),
        CheckConstraint("total >= 0", name="ck_purchase_order_total"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("ship_to_location_id", "traceability", "location", nullable=True),
        _fk("vendor_contract_id", "procurement", "vendor_contract", nullable=True),
    ],
    indexes=[("vendor_status", "vendor_id", "status"), ("status_date", "status", "expected_receipt_date")],
)

purchase_order_line = mk(
    "purchase_order_line",
    [
        Column("purchase_order_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("expense_account_ref", Text, nullable=True),
        Column("description", Text, nullable=True),
        Column("qty_ordered", QTY, nullable=False),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("qty_billed", QTY, nullable=False, server_default=text("0")),
        Column("qty_returned", QTY, nullable=False, server_default=text("0")),
        Column("qty_cancelled", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("unit_price", MONEY, nullable=False, server_default=text("0")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("expected_receipt_date", Date, nullable=True),
        Column("ship_to_location_id", UUID_T, nullable=True),
        Column("is_drop_ship", Boolean, nullable=False, server_default=text("false")),
        Column("is_special_order", Boolean, nullable=False, server_default=text("false")),
        Column("external_sales_ref", Text, nullable=True),
        Column("vendor_contract_item_id", UUID_T, nullable=True),
        Column("is_closed", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "purchase_order_id", "line_number", name="uq_purchase_order_line"),
        CheckConstraint("qty_ordered > 0", name="ck_po_line_qty_ordered"),
        CheckConstraint(
            "qty_received >= 0 AND qty_billed >= 0 AND qty_returned >= 0 AND qty_cancelled >= 0",
            name="ck_po_line_nonneg",
        ),
        CheckConstraint("unit_price >= 0", name="ck_po_line_price"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("item_id", "item_master", "item", nullable=True),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("ship_to_location_id", "traceability", "location", nullable=True),
        _fk("vendor_contract_item_id", "procurement", "vendor_contract_item", nullable=True),
    ],
    indexes=[("po", "purchase_order_id"), ("item_date", "item_id", "expected_receipt_date")],
)

purchase_order_release = mk(
    "purchase_order_release",
    [
        Column("purchase_order_id", UUID_T, nullable=False),
        Column("release_number", Integer, nullable=False),
        Column("release_date", Date, nullable=False),
        Column("qty_released", QTY, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("created_po_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "purchase_order_id", "release_number", name="uq_po_release"),
        CheckConstraint("qty_released > 0", name="ck_po_release_qty"),
        CheckConstraint("status IN ('DRAFT','RELEASED','CLOSED')", name="ck_po_release_status"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("created_po_id", "procurement", "purchase_order", nullable=True),
    ],
    indexes=[("created_po", "created_po_id")],
)


# -------------------------------------------------------------------------------------------------
# 0017_procurement_billing — receipts, bills/match/variances, credits, returns, drop-ship links
# -------------------------------------------------------------------------------------------------
item_receipt = mk(
    "item_receipt",
    [
        Column("document_number", Text, nullable=False),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("vendor_id", UUID_T, nullable=False),
        Column("receipt_type", Text, nullable=False, server_default=text("'PO'")),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("received_at", STAMP, nullable=False, server_default=text("now()")),
        Column("location_id", UUID_T, nullable=True),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
        Column("deferred_posting", Boolean, nullable=False, server_default=text("false")),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("reversal_transaction_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_item_receipt_number"),
        CheckConstraint(f"status IN {RECEIPT_STATUSES!r}", name="ck_item_receipt_status"),
        CheckConstraint(f"receipt_type IN {RECEIPT_TYPES!r}", name="ck_item_receipt_type"),
        _fk("purchase_order_id", "procurement", "purchase_order", nullable=True),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("location_id", "traceability", "location", nullable=True),
    ],
    indexes=[("po", "purchase_order_id"), ("status_received", "status", "received_at")],
)

item_receipt_line = mk(
    "item_receipt_line",
    [
        Column("item_receipt_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("item_id", UUID_T, nullable=False),
        Column("description", Text, nullable=True),
        Column("qty_expected", QTY, nullable=False, server_default=text("0")),
        Column("qty_received", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
        Column("location_id", UUID_T, nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("is_landed_cost_estimated", Boolean, nullable=False, server_default=text("false")),
        Column("expense_account_ref", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "item_receipt_id", "line_number", name="uq_item_receipt_line"),
        CheckConstraint("qty_received >= 0", name="ck_receipt_line_qty"),
        CheckConstraint("unit_cost >= 0", name="ck_receipt_line_cost"),
        _fk("item_receipt_id", "procurement", "item_receipt"),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line", nullable=True),
        _fk("item_id", "item_master", "item"),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("location_id", "traceability", "location", nullable=True),
        _fk("inventory_status_id", "traceability", "inventory_status", nullable=True),
    ],
    indexes=[("item", "item_id"), ("po_line", "purchase_order_line_id")],
)

item_receipt_detail = mk(
    "item_receipt_detail",
    [
        Column("item_receipt_line_id", UUID_T, nullable=False),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("inventory_status_id", UUID_T, nullable=True),
        Column("ownership", Text, nullable=False, server_default=text("'OWNED'")),
        Column("qty_received", QTY, nullable=False),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "item_receipt_line_id",
            "bin_id",
            "lot_id",
            "serial_id",
            "inventory_status_id",
            name="uq_item_receipt_detail",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("qty_received > 0", name="ck_receipt_detail_qty"),
        CheckConstraint("ownership IN ('OWNED','CONSIGNED','IN_TRANSIT')", name="ck_receipt_detail_ownership"),
        _fk("item_receipt_line_id", "procurement", "item_receipt_line"),
    ],
    indexes=[("serial", "serial_id"), ("lot", "lot_id")],
)

vendor_bill = mk(
    "vendor_bill",
    [
        Column("document_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("vendor_bill_number", Text, nullable=False),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("item_receipt_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("transaction_date", Date, nullable=False),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
        Column("subtotal", MONEY, nullable=False, server_default=text("0")),
        Column("total", MONEY, nullable=False, server_default=text("0")),
        Column("amount_paid", MONEY, nullable=False, server_default=text("0")),
        Column("variance_account_ref", Text, nullable=True),
        Column("approval_request_id", UUID_T, nullable=True),
        Column("payment_terms", Text, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_id", "vendor_bill_number", name="uq_vendor_bill_number"),
        UniqueConstraint("tenant_id", "document_number", name="uq_vendor_bill_document"),
        CheckConstraint(f"status IN {BILL_STATUSES!r}", name="ck_vendor_bill_status"),
        CheckConstraint("total >= 0", name="ck_vendor_bill_total"),
        CheckConstraint("amount_paid >= 0", name="ck_vendor_bill_paid"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("purchase_order_id", "procurement", "purchase_order", nullable=True),
        _fk("item_receipt_id", "procurement", "item_receipt", nullable=True),
    ],
    indexes=[("vendor_status", "vendor_id", "status"), ("receipt", "item_receipt_id")],
)

vendor_bill_line = mk(
    "vendor_bill_line",
    [
        Column("vendor_bill_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("expense_account_ref", Text, nullable=True),
        Column("description", Text, nullable=True),
        Column("qty", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("unit_price", MONEY, nullable=False, server_default=text("0")),
        Column("amount", MONEY, nullable=False, server_default=text("0")),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("item_receipt_line_id", UUID_T, nullable=True),
        Column("is_landed_cost", Boolean, nullable=False, server_default=text("false")),
        Column("landed_cost_category_id", UUID_T, nullable=True),
        Column("variance_amount", MONEY, nullable=False, server_default=text("0")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_bill_id", "line_number", name="uq_vendor_bill_line"),
        CheckConstraint("qty >= 0", name="ck_vendor_bill_line_qty"),
        CheckConstraint("amount >= 0", name="ck_vendor_bill_line_amount"),
        _fk("vendor_bill_id", "procurement", "vendor_bill"),
        _fk("item_id", "item_master", "item", nullable=True),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line", nullable=True),
        _fk("item_receipt_line_id", "procurement", "item_receipt_line", nullable=True),
    ],
    indexes=[("item", "item_id"), ("receipt_line", "item_receipt_line_id")],
)

vendor_bill_variance = mk(
    "vendor_bill_variance",
    [
        Column("vendor_bill_line_id", UUID_T, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("item_receipt_line_id", UUID_T, nullable=True),
        Column("variance_type", Text, nullable=False),
        Column("variance_amount", MONEY, nullable=False),
        Column("variance_qty", QTY, nullable=False, server_default=text("0")),
        Column("account_ref", Text, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'POSTED'")),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        CheckConstraint(f"variance_type IN {VARIANCE_TYPES!r}", name="ck_variance_type"),
        CheckConstraint("variance_amount <> 0", name="ck_variance_amount_nonzero"),
        CheckConstraint("status IN ('POSTED','OVERRIDDEN','WAIVED')", name="ck_variance_status"),
        _fk("vendor_bill_line_id", "procurement", "vendor_bill_line"),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line", nullable=True),
        _fk("item_receipt_line_id", "procurement", "item_receipt_line", nullable=True),
    ],
    indexes=[("bill_line", "vendor_bill_line_id")],
)

vendor_bill_match = mk(
    "vendor_bill_match",
    [
        Column("vendor_bill_id", UUID_T, nullable=False),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("match_status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("tolerance_amount", MONEY, nullable=False, server_default=text("0.01")),
        Column("tolerance_percent", MONEY, nullable=False, server_default=text("1.0")),
        Column("exception_codes", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
        Column("overridden_by", UUID_T, nullable=True),
        Column("overridden_at", STAMP, nullable=True),
        Column("notes", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_bill_id", name="uq_vendor_bill_match"),
        CheckConstraint(f"match_status IN {MATCH_STATUSES!r}", name="ck_vendor_bill_match_status"),
        _fk("vendor_bill_id", "procurement", "vendor_bill"),
        _fk("purchase_order_id", "procurement", "purchase_order", nullable=True),
    ],
    indexes=[("status", "match_status"), ("po", "purchase_order_id")],
)

vendor_credit = mk(
    "vendor_credit",
    [
        Column("document_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("vendor_credit_number", Text, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("transaction_date", Date, nullable=False),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
        Column("total", MONEY, nullable=False, server_default=text("0")),
        Column("remaining_amount", MONEY, nullable=False, server_default=text("0")),
        Column("vendor_bill_id", UUID_T, nullable=True),
        Column("vendor_return_id", UUID_T, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_vendor_credit_document"),
        UniqueConstraint("tenant_id", "vendor_id", "vendor_credit_number", name="uq_vendor_credit_number"),
        CheckConstraint(f"status IN {CREDIT_STATUSES!r}", name="ck_vendor_credit_status"),
        CheckConstraint("total >= 0 AND remaining_amount >= 0", name="ck_vendor_credit_total"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("vendor_bill_id", "procurement", "vendor_bill", nullable=True),
    ],
    indexes=[("vendor_status", "vendor_id", "status")],
)

vendor_credit_line = mk(
    "vendor_credit_line",
    [
        Column("vendor_credit_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=True),
        Column("expense_account_ref", Text, nullable=True),
        Column("qty", QTY, nullable=False, server_default=text("0")),
        Column("unit_price", MONEY, nullable=False, server_default=text("0")),
        Column("amount", MONEY, nullable=False, server_default=text("0")),
        Column("item_receipt_line_id", UUID_T, nullable=True),
        Column("vendor_return_line_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_credit_id", "line_number", name="uq_vendor_credit_line"),
        CheckConstraint("amount >= 0", name="ck_vendor_credit_line_amount"),
        _fk("vendor_credit_id", "procurement", "vendor_credit"),
        _fk("item_id", "item_master", "item", nullable=True),
        _fk("item_receipt_line_id", "procurement", "item_receipt_line", nullable=True),
    ],
    indexes=[("item", "item_id")],
)

vendor_return_authorization = mk(
    "vendor_return_authorization",
    [
        Column("document_number", Text, nullable=False),
        Column("vendor_id", UUID_T, nullable=False),
        Column("purchase_order_id", UUID_T, nullable=True),
        Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
        Column("reason_code", Text, nullable=True),
        Column("transaction_date", Date, nullable=False),
        Column("currency_code", Text, nullable=False, server_default=text("'USD'")),
        Column("return_date", Date, nullable=True),
        Column("vendor_credit_id", UUID_T, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
        Column("memo", Text, nullable=True),
    ],
    extra=[
        UniqueConstraint("tenant_id", "document_number", name="uq_vendor_return_number"),
        CheckConstraint(f"status IN {RETURN_STATUSES!r}", name="ck_vendor_return_status"),
        _fk("vendor_id", "procurement", "vendor"),
        _fk("purchase_order_id", "procurement", "purchase_order", nullable=True),
        _fk("vendor_credit_id", "procurement", "vendor_credit", nullable=True),
    ],
    indexes=[("vendor_status", "vendor_id", "status")],
)

vendor_return_authorization_line = mk(
    "vendor_return_authorization_line",
    [
        Column("vendor_return_authorization_id", UUID_T, nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=True),
        Column("item_receipt_line_id", UUID_T, nullable=True),
        Column("qty_authorized", QTY, nullable=False),
        Column("qty_shipped", QTY, nullable=False, server_default=text("0")),
        Column("qty_credited", QTY, nullable=False, server_default=text("0")),
        Column("uom_id", UUID_T, nullable=True),
        Column("unit_cost", MONEY, nullable=False, server_default=text("0")),
        Column("location_id", UUID_T, nullable=True),
        Column("bin_id", UUID_T, nullable=True),
        Column("lot_id", UUID_T, nullable=True),
        Column("serial_id", UUID_T, nullable=True),
        Column("is_closed", Boolean, nullable=False, server_default=text("false")),
    ],
    extra=[
        UniqueConstraint("tenant_id", "vendor_return_authorization_id", "line_number", name="uq_vendor_return_line"),
        CheckConstraint("qty_authorized > 0", name="ck_vendor_return_line_qty"),
        CheckConstraint("qty_shipped >= 0 AND qty_shipped <= qty_authorized", name="ck_vendor_return_line_shipped"),
        CheckConstraint("qty_credited >= 0 AND qty_credited <= qty_shipped", name="ck_vendor_return_line_credited"),
        _fk("vendor_return_authorization_id", "procurement", "vendor_return_authorization"),
        _fk("item_id", "item_master", "item"),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line", nullable=True),
        _fk("item_receipt_line_id", "procurement", "item_receipt_line", nullable=True),
        _fk("uom_id", "item_master", "uom", nullable=True),
        _fk("location_id", "traceability", "location", nullable=True),
    ],
    indexes=[("item", "item_id")],
)

special_order_link = mk(
    "special_order_link",
    [
        Column("link_type", Text, nullable=False),
        Column("external_sales_ref", Text, nullable=False),
        Column("external_sales_line_ref", Text, nullable=False),
        Column("item_id", UUID_T, nullable=False),
        Column("purchase_order_id", UUID_T, nullable=False),
        Column("purchase_order_line_id", UUID_T, nullable=False),
        Column("qty", QTY, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("fulfilled_at", STAMP, nullable=True),
        Column("posted_transaction_id", UUID_T, nullable=True),
    ],
    extra=[
        UniqueConstraint(
            "tenant_id",
            "external_sales_ref",
            "external_sales_line_ref",
            "purchase_order_line_id",
            name="uq_special_order_link",
        ),
        CheckConstraint(f"link_type IN {LINK_TYPES!r}", name="ck_special_order_link_type"),
        CheckConstraint(f"status IN {LINK_STATUSES!r}", name="ck_special_order_link_status"),
        CheckConstraint("qty > 0", name="ck_special_order_link_qty"),
        _fk("item_id", "item_master", "item"),
        _fk("purchase_order_id", "procurement", "purchase_order"),
        _fk("purchase_order_line_id", "procurement", "purchase_order_line"),
    ],
    indexes=[("item", "item_id"), ("po", "purchase_order_id")],
)


REVISION_0015_TABLES: tuple[Table, ...] = (
    vendor_category,
    vendor,
    item_vendor,
    item_vendor_price,
    vendor_contract,
    vendor_contract_item,
)
REVISION_0016_TABLES: tuple[Table, ...] = (
    purchase_requisition,
    purchase_requisition_line,
    purchase_request,
    purchase_request_line,
    rfq,
    rfq_line,
    rfq_response,
    rfq_response_line,
    purchase_order,
    purchase_order_line,
    purchase_order_release,
)
REVISION_0017_TABLES: tuple[Table, ...] = (
    item_receipt,
    item_receipt_line,
    item_receipt_detail,
    vendor_bill,
    vendor_bill_line,
    vendor_bill_variance,
    vendor_bill_match,
    vendor_credit,
    vendor_credit_line,
    vendor_return_authorization,
    vendor_return_authorization_line,
    special_order_link,
)
ALL_TABLES: tuple[Table, ...] = REVISION_0015_TABLES + REVISION_0016_TABLES + REVISION_0017_TABLES

#: Procurement documents are append-only at the *service* level (posted corrections use credits/
#: reversals). No procurement table gets a DB append-only trigger: variance rows are created at match
#: and stamped with ``posted_transaction_id`` when T-06 posts, so they must remain updatable.
APPEND_ONLY_TABLES: tuple[str, ...] = ()
