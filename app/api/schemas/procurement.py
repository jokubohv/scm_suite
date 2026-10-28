"""Procurement boundary DTOs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from app.api.schemas.common import ActionResult, APIModel, Page


class ReceiptDetailOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_received: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ReceiptLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_receipt_id: UUID | None = None
    line_number: int | None = None
    purchase_order_line_id: UUID | None = None
    item_id: UUID
    description: str | None = None
    qty_expected: Decimal | None = None
    qty_received: Decimal | None = None
    uom_id: UUID | None = None
    unit_cost: Decimal | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    is_landed_cost_estimated: bool | None = None
    expense_account_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    details: list[ReceiptDetailOut] = Field(default_factory=list)


class ReceiptOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str
    purchase_order_id: UUID | None = None
    vendor_id: UUID | None = None
    receipt_type: str | None = None
    status: str
    received_at: datetime | None = None
    location_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    deferred_posting: bool | None = None
    posted_transaction_id: UUID | None = None
    reversal_transaction_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[ReceiptLineOut] = Field(default_factory=list)
    landed_cost: dict | None = None


class ReceiptSummaryOut(APIModel):
    id: UUID
    document_number: str
    purchase_order_id: UUID | None = None
    vendor_id: UUID | None = None
    receipt_type: str | None = None
    status: str
    received_at: datetime | None = None
    location_id: UUID | None = None
    currency_code: str | None = None
    deferred_posting: bool | None = None
    posted_transaction_id: UUID | None = None
    reversal_transaction_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class ReceiptPage(Page[ReceiptSummaryOut]):
    pagination_contract_version: Literal[1] | None = None


class ReceiptDetailIn(APIModel):
    qty_received: Decimal | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None


class ReceiptCreateLine(APIModel):
    purchase_order_line_id: UUID | None = None
    item_id: UUID
    qty_received: Decimal
    unit_cost: Decimal | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    ownership: str | None = None
    details: list[ReceiptDetailIn] | None = None


class ReceiptCreate(APIModel):
    purchase_order_id: UUID | None = None
    vendor_id: UUID | None = None
    receipt_type: Literal["PO", "ASN", "MANUAL", "STANDALONE", "INBOUND_SHIPMENT"] | None = None
    received_at: datetime | None = None
    location_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    deferred_posting: bool | None = None
    memo: str | None = None
    lines: list[ReceiptCreateLine]
    landed_cost_charges: list[dict] | None = None


class ReceiptUpdate(APIModel):
    memo: str | None = None
    location_id: UUID | None = None
    lines: list[ReceiptCreateLine] | None = None


class ReceiptActionOut(APIModel):
    id: str | None = None
    status: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    landed_cost: dict | None = None
    landed_cost_reversal: dict | None = None
    approval_mode: str | None = None


class VendorOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    vendor_number: str
    name: str
    vendor_category_id: UUID | None = None
    currency_code: str | None = None
    payment_terms: str | None = None
    tax_id: str | None = None
    is_1099_eligible: bool | None = None
    credit_limit: Decimal | None = None
    default_expense_account_ref: str | None = None
    default_payable_account_ref: str | None = None
    addresses: list[dict] = Field(default_factory=list)
    primary_contact: dict | None = None
    is_inactive: bool | None = None
    inactive_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorPage(Page[VendorOut]):
    matching_contract_version: Literal[1] = 1


class VendorCreate(APIModel):
    name: str
    vendor_number: str
    vendor_category_id: UUID | None = None
    currency_code: str | None = None
    payment_terms: str | None = None
    tax_id: str | None = None
    is_1099_eligible: bool | None = None
    credit_limit: Decimal | None = None
    default_expense_account_ref: str | None = None
    default_payable_account_ref: str | None = None
    addresses: list[dict] | None = None
    primary_contact: dict | None = None


class VendorUpdate(APIModel):
    name: str | None = None
    vendor_category_id: UUID | None = None
    currency_code: str | None = None
    payment_terms: str | None = None
    tax_id: str | None = None
    is_1099_eligible: bool | None = None
    credit_limit: Decimal | None = None
    default_expense_account_ref: str | None = None
    default_payable_account_ref: str | None = None
    addresses: list[dict] | None = None
    primary_contact: dict | None = None
    version: int | None = None


class PurchaseOrderLineOut(APIModel):
    id: UUID
    purchase_order_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    description: str | None = None
    qty_ordered: Decimal | None = None
    qty_received: Decimal | None = None
    qty_billed: Decimal | None = None
    unit_price: Decimal | None = None
    uom_id: UUID | None = None
    expected_receipt_date: date | None = None
    tax_code_id: UUID | None = None
    line_total: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class PurchaseOrderOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str
    transaction_date: date | None = None
    vendor_id: UUID | None = None
    po_type: str | None = None
    status: str
    ship_to_location_id: UUID | None = None
    bill_to: dict | None = None
    vendor_contract_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    subtotal: Decimal | None = None
    total: Decimal | None = None
    expected_receipt_date: date | None = None
    approval_request_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[PurchaseOrderLineOut] = Field(default_factory=list)


class PurchaseOrderPage(Page[PurchaseOrderOut]):
    pagination_contract_version: Literal[1] | None = None


class PurchaseOrderLineCreate(APIModel):
    item_id: UUID
    qty_ordered: Decimal
    unit_price: Decimal | None = None
    uom_id: UUID | None = None
    expected_receipt_date: date | None = None
    description: str | None = None
    tax_code_id: UUID | None = None
    external_sales_ref: str | None = None
    is_drop_ship: bool | None = None
    is_special_order: bool | None = None


class PurchaseOrderCreate(APIModel):
    vendor_id: UUID
    po_type: str | None = None
    transaction_date: date | None = None
    ship_to_location_id: UUID | None = None
    bill_to: dict | None = None
    vendor_contract_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    expected_receipt_date: date | None = None
    memo: str | None = None
    lines: list[PurchaseOrderLineCreate]


class PurchaseOrderUpdate(APIModel):
    vendor_id: UUID | None = None
    transaction_date: date | None = None
    ship_to_location_id: UUID | None = None
    bill_to: dict | None = None
    expected_receipt_date: date | None = None
    memo: str | None = None
    lines: list[PurchaseOrderLineCreate] | None = None
    version: int | None = None


class VendorBillLineOut(APIModel):
    id: UUID
    vendor_bill_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    description: str | None = None
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    uom_id: UUID | None = None
    amount: Decimal | None = None
    expense_account_ref: str | None = None
    is_landed_cost: bool | None = None
    landed_cost_category_id: UUID | None = None
    line_total: Decimal | None = None
    purchase_order_line_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    tax_code_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class VendorBillMatchSummary(APIModel):
    match_status: str
    tolerance_amount: Decimal | None = None
    tolerance_percent: Decimal | None = None
    exception_codes: list[str] = Field(default_factory=list)
    notes: str | None = None


class VendorBillOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str
    vendor_id: UUID | None = None
    vendor_bill_number: str | None = None
    purchase_order_id: UUID | None = None
    item_receipt_id: UUID | None = None
    status: str
    transaction_date: date | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    subtotal: Decimal | None = None
    total: Decimal | None = None
    amount_paid: Decimal | None = None
    variance_account_ref: str | None = None
    approval_request_id: UUID | None = None
    payment_terms: str | None = None
    posted_transaction_id: UUID | None = None
    match_status: str | None = None
    match: VendorBillMatchSummary | None = None
    exception_codes: list[str] = Field(default_factory=list)
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[VendorBillLineOut] = Field(default_factory=list)
    landed_cost: dict | None = None


class VendorBillPage(Page[VendorBillOut]):
    bill_preparation_contract_version: Literal[1] = 1


class VendorBillLineCreate(APIModel):
    item_id: UUID | None = None
    description: str | None = None
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    uom_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    tax_code_id: UUID | None = None
    is_landed_cost: bool | None = None
    amount: Decimal | None = None


class VendorBillCreate(APIModel):
    vendor_id: UUID
    vendor_bill_number: str | None = None
    purchase_order_id: UUID | None = None
    item_receipt_id: UUID | None = None
    transaction_date: date | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    payment_terms: str | None = None
    variance_account_ref: str | None = None
    memo: str | None = None
    lines: list[VendorBillLineCreate]


class VendorBillUpdate(APIModel):
    vendor_bill_number: str | None = None
    transaction_date: date | None = None
    payment_terms: str | None = None
    variance_account_ref: str | None = None
    memo: str | None = None
    lines: list[VendorBillLineCreate] | None = None
    version: int | None = None


class ProcurementDTO(APIModel):
    """Forward-compatible procurement boundary base: declared fields typed, service additions preserved."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorCategoryOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemVendorOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    vendor_id: UUID | None = None
    vendor_item_code: str | None = None
    is_preferred: bool | None = None
    lead_time_days: int | None = None
    min_order_qty: Decimal | None = None
    purchase_uom_id: UUID | None = None
    currency_code: str | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemVendorPriceOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_vendor_id: UUID | None = None
    qty_min: Decimal | None = None
    qty_max: Decimal | None = None
    unit_price: Decimal | None = None
    currency_code: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorContractOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    contract_number: str | None = None
    vendor_id: UUID | None = None
    contract_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = None
    currency_code: str | None = None
    terms: Any | None = None
    total_committed_amount: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorContractItemOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_contract_id: UUID | None = None
    item_id: UUID | None = None
    contracted_qty: Decimal | None = None
    used_qty: Decimal | None = None
    unit_price: Decimal | None = None
    discount_percent: Decimal | None = None
    uom_id: UUID | None = None
    delivery_schedule: Any | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PurchaseRequisitionOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    transaction_date: date | None = None
    requested_by: UUID | None = None
    department_ref: str | None = None
    status: str | None = None
    estimated_total: Decimal | None = None
    currency_code: str | None = None
    memo: str | None = None
    approval_request_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PurchaseRequisitionLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    purchase_requisition_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    expense_account_ref: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    estimated_unit_price: Decimal | None = None
    uom_id: UUID | None = None
    suggested_vendor_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PurchaseRequestOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    transaction_date: date | None = None
    requested_by: UUID | None = None
    status: str | None = None
    approved_at: datetime | None = None
    currency_code: str | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PurchaseRequestLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    purchase_request_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    expense_account_ref: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    anticipated_unit_price: Decimal | None = None
    purchase_order_line_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class RfqOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    rfq_number: str | None = None
    status: str | None = None
    bid_open_date: date | None = None
    bid_close_date: date | None = None
    currency_code: str | None = None
    awarded_response_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class RfqLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    rfq_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    target_price: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class RfqResponseOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    rfq_id: UUID | None = None
    vendor_id: UUID | None = None
    status: str | None = None
    received_at: datetime | None = None
    currency_code: str | None = None
    total_amount: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class RfqResponseLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    rfq_response_id: UUID | None = None
    rfq_line_id: UUID | None = None
    unit_price: Decimal | None = None
    qty_quoted: Decimal | None = None
    lead_time_days: int | None = None
    is_awarded: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PurchaseOrderReleaseOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    purchase_order_id: UUID | None = None
    release_number: int | None = None
    release_date: date | None = None
    qty_released: Decimal | None = None
    status: str | None = None
    created_po_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemReceiptOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    purchase_order_id: UUID | None = None
    vendor_id: UUID | None = None
    receipt_type: str | None = None
    status: str | None = None
    received_at: datetime | None = None
    location_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    deferred_posting: bool | None = None
    posted_transaction_id: UUID | None = None
    reversal_transaction_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemReceiptLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_receipt_id: UUID | None = None
    line_number: int | None = None
    purchase_order_line_id: UUID | None = None
    item_id: UUID | None = None
    description: str | None = None
    qty_expected: Decimal | None = None
    qty_received: Decimal | None = None
    uom_id: UUID | None = None
    unit_cost: Decimal | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    is_landed_cost_estimated: bool | None = None
    expense_account_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemReceiptDetailOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_received: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorBillVarianceOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_bill_line_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    variance_type: str | None = None
    variance_amount: Decimal | None = None
    variance_qty: Decimal | None = None
    account_ref: str | None = None
    status: str | None = None
    posted_transaction_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorBillMatchOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_bill_id: UUID | None = None
    purchase_order_id: UUID | None = None
    match_status: str | None = None
    tolerance_amount: Decimal | None = None
    tolerance_percent: Decimal | None = None
    exception_codes: list[Any] | None = None
    overridden_by: UUID | None = None
    overridden_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorCreditOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    vendor_id: UUID | None = None
    vendor_credit_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    total: Decimal | None = None
    remaining_amount: Decimal | None = None
    vendor_bill_id: UUID | None = None
    vendor_return_id: UUID | None = None
    posted_transaction_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorCreditLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_credit_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    expense_account_ref: str | None = None
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    item_receipt_line_id: UUID | None = None
    vendor_return_line_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorReturnAuthorizationOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    vendor_id: UUID | None = None
    purchase_order_id: UUID | None = None
    status: str | None = None
    reason_code: str | None = None
    transaction_date: date | None = None
    currency_code: str | None = None
    return_date: date | None = None
    vendor_credit_id: UUID | None = None
    posted_transaction_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class VendorReturnAuthorizationLineOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_return_authorization_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    qty_authorized: Decimal | None = None
    qty_shipped: Decimal | None = None
    qty_credited: Decimal | None = None
    uom_id: UUID | None = None
    unit_cost: Decimal | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    is_closed: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class SpecialOrderLinkOut(ProcurementDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    link_type: str | None = None
    external_sales_ref: str | None = None
    external_sales_line_ref: str | None = None
    item_id: UUID | None = None
    purchase_order_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    qty: Decimal | None = None
    status: str | None = None
    fulfilled_at: datetime | None = None
    posted_transaction_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ProcurementActionOut(ActionResult):
    """Generic action/status response for procurement transitions (typed common action fields)."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorPurchaseHistoryRow(ProcurementDTO):
    vendor_id: UUID | None = None
    purchase_order_id: UUID | None = None
    document_number: str | None = None
    transaction_date: date | None = None
    status: str | None = None
    item_id: UUID | None = None
    qty_ordered: Decimal | None = None
    qty_received: Decimal | None = None
    qty_billed: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    currency_code: str | None = None


class VendorPurchaseHistoryPage(Page[VendorPurchaseHistoryRow]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ReceivedNotBilledRow(ProcurementDTO):
    item_receipt_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    document_number: str | None = None
    vendor_id: UUID | None = None
    purchase_order_id: UUID | None = None
    item_id: UUID | None = None
    qty_received: Decimal | None = None
    unit_cost: Decimal | None = None
    qty_billed: Decimal | None = None
    amount_billed: Decimal | None = None
    received_value: Decimal | None = None
    remaining_qty: Decimal | None = None
    remaining_accrual: Decimal | None = None


class ReceivedNotBilledPage(Page[ReceivedNotBilledRow]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PurchaseOrderStatusOut(ProcurementDTO):
    id: UUID | None = None
    status: str | None = None
    qty_ordered: Decimal | None = None
    qty_received: Decimal | None = None
    qty_billed: Decimal | None = None
    qty_returned: Decimal | None = None
    qty_cancelled: Decimal | None = None
    received_qty: Decimal | None = None
    billed_qty: Decimal | None = None
    open_qty: Decimal | None = None


class PurchaseOrderHistoryRow(ProcurementDTO):
    id: UUID | None = None
    purchase_order_id: UUID | None = None
    event_type: str | None = None
    status: str | None = None
    occurred_at: datetime | None = None
    actor_id: UUID | None = None
    detail: Any | None = None
    created_at: datetime | None = None


class PurchaseOrderHistoryPage(Page[PurchaseOrderHistoryRow]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorBillVariancePage(Page[VendorBillVarianceOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class BillMatchOut(ProcurementDTO):
    id: UUID | None = None
    vendor_bill_id: UUID | None = None
    match_status: str | None = None
    tolerance_amount: Decimal | None = None
    tolerance_percent: Decimal | None = None
    exception_codes: Any | None = None
    notes: str | None = None
    variances: list[VendorBillVarianceOut] | None = None


class VendorCategoryPage(Page[VendorCategoryOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemVendorPage(Page[ItemVendorOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)
    matching_contract_version: Literal[1] = 1


class ItemVendorPricePage(Page[ItemVendorPriceOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorContractPage(Page[VendorContractOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PurchaseRequisitionPage(Page[PurchaseRequisitionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PurchaseRequestPage(Page[PurchaseRequestOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class RfqPage(Page[RfqOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class RfqResponsePage(Page[RfqResponseOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PurchaseOrderReleasePage(Page[PurchaseOrderReleaseOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorCreditPage(Page[VendorCreditOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class VendorReturnPage(Page[VendorReturnAuthorizationOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class SpecialOrderLinkPage(Page[SpecialOrderLinkOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ProcurementRequestDTO(ProcurementDTO):
    """Typed procurement request base: common optional boundary fields; extras pass through."""

    id: Any | None = None
    code: Any | None = None
    name: Any | None = None
    description: Any | None = None
    status: Any | None = None
    reason: Any | None = None
    notes: Any | None = None
    memo: Any | None = None
    message: Any | None = None
    data: Any | None = None
    metadata: Any | None = None
    settings: Any | None = None
    filters: Any | None = None
    lines: Any | None = None
    items: Any | None = None
    rows: Any | None = None
    details: Any | None = None
    members: Any | None = None
    criteria: Any | None = None
    actions: Any | None = None
    breaks: Any | None = None
    allocations: Any | None = None
    overrides: Any | None = None
    attributes: Any | None = None
    payload: Any | None = None
    item_id: Any | None = None
    item_ids: Any | None = None
    location_id: Any | None = None
    vendor_id: Any | None = None
    vendor_category_id: Any | None = None
    customer_id: Any | None = None
    category_id: Any | None = None
    group_id: Any | None = None
    contract_id: Any | None = None
    purchase_order_id: Any | None = None
    purchase_order_line_id: Any | None = None
    item_receipt_id: Any | None = None
    item_receipt_line_id: Any | None = None
    rfq_id: Any | None = None
    rfq_line_id: Any | None = None
    vendor_credit_id: Any | None = None
    vendor_return_id: Any | None = None
    purchase_requisition_id: Any | None = None
    purchase_request_id: Any | None = None
    special_order_link_id: Any | None = None
    currency_code: Any | None = None
    exchange_rate: Any | None = None
    amount: Any | None = None
    amounts: Any | None = None
    qty: Any | None = None
    quantity: Any | None = None
    qty_ordered: Any | None = None
    qty_received: Any | None = None
    unit_price: Any | None = None
    price: Any | None = None
    value: Any | None = None
    percent: Any | None = None
    discount_percent: Any | None = None
    effective_from: Any | None = None
    effective_to: Any | None = None
    start_date: Any | None = None
    end_date: Any | None = None
    transaction_date: Any | None = None
    date: Any | None = None
    as_of: Any | None = None
    external_ref: Any | None = None
    reference: Any | None = None
    approval_ref: Any | None = None
    override_reason: Any | None = None
    reason_code: Any | None = None
    format: Any | None = None
    fields: Any | None = None
    limit: Any | None = None
    cursor: Any | None = None
    is_active: Any | None = None
    is_inactive: Any | None = None
    active: Any | None = None
    approve: Any | None = None
    submit: Any | None = None
    post: Any | None = None
    user_id: Any | None = None
    actor_id: Any | None = None
    actor: Any | None = None
    target: Any | None = None
    target_ref: Any | None = None
    basis: Any | None = None
    allocation_basis: Any | None = None
    priority: Any | None = None
    schedule: Any | None = None
    payment_terms: Any | None = None
    tax_id: Any | None = None
    credit_limit: Any | None = None
    purchase_uom_id: Any | None = None
    lead_time_days: Any | None = None
    min_order_qty: Any | None = None
    vendor_item_code: Any | None = None
    is_preferred: Any | None = None
    contract_number: Any | None = None
    contract_type: Any | None = None
    total_committed_amount: Any | None = None
    terms: Any | None = None
    requested_by: Any | None = None
    department_ref: Any | None = None
    estimated_total: Any | None = None
    suggested_vendor_id: Any | None = None
    anticipated_unit_price: Any | None = None
    bid_open_date: Any | None = None
    bid_close_date: Any | None = None
    awarded_response_id: Any | None = None
    target_price: Any | None = None
    received_at: Any | None = None
    vendor_contract_id: Any | None = None
    ship_to_location_id: Any | None = None
    bill_to: Any | None = None
    expected_receipt_date: Any | None = None
    po_type: Any | None = None
    receipt_type: Any | None = None
    deferred_posting: Any | None = None
    variance_account_ref: Any | None = None
    payment_terms: Any | None = None
    landed_cost_charges: Any | None = None
    is_landed_cost: Any | None = None
    landed_cost_category_id: Any | None = None
    link_type: Any | None = None
    external_sales_ref: Any | None = None
    external_sales_line_ref: Any | None = None
    qty_authorized: Any | None = None
    qty_shipped: Any | None = None
    qty_credited: Any | None = None
    qty_released: Any | None = None
    release_date: Any | None = None
    status_reason: Any | None = None
    memo: Any | None = None
    merge_into_vendor_id: Any | None = None
    source_vendor_id: Any | None = None
    target_vendor_id: Any | None = None
    inactive_reason: Any | None = None
    override: Any | None = None
    force: Any | None = None
    waive: Any | None = None
    qty_billed: Any | None = None
    qty_returned: Any | None = None
    qty_cancelled: Any | None = None
    uom_id: Any | None = None
    serial_ids: Any | None = None
    bin_id: Any | None = None
    lot_id: Any | None = None
    inventory_status_id: Any | None = None
    ownership: Any | None = None
    line_ids: Any | None = None
    po_id: Any | None = None
    request_id: Any | None = None
    requisition_id: Any | None = None
    credit_id: Any | None = None
    return_id: Any | None = None
    bill_id: Any | None = None
    receipt_id: Any | None = None
    vendor_contract_item_id: Any | None = None
    vendor_return_line_id: Any | None = None
    expense_account_ref: Any | None = None


class ProcurementAddItemVendorPriceIn(ProcurementRequestDTO):
    pass


class ProcurementApplyCreditIn(ProcurementRequestDTO):
    applications: Any | None = None


class ProcurementApproveBillIn(ProcurementRequestDTO):
    comment: Any | None = None


class ProcurementApproveContractIn(ProcurementRequestDTO):
    comment: Any | None = None


class ProcurementApprovePurchaseOrderIn(ProcurementRequestDTO):
    comment: Any | None = None


class ProcurementAssociateItemVendorIn(ProcurementRequestDTO):
    pass


class ProcurementAwardRfqIn(ProcurementRequestDTO):
    pass


class ProcurementCancelBillIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementCancelPurchaseOrderIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementCloseContractIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementClosePurchaseOrderIn(ProcurementRequestDTO):
    line_ids: Any | None = None
    reason: Any | None = None


class ProcurementCloseReturnIn(ProcurementRequestDTO):
    line_ids: Any | None = None


class ProcurementConvertPurchaseRequestIn(ProcurementRequestDTO):
    pass


class ProcurementCreateContractIn(ProcurementRequestDTO):
    pass


class ProcurementCreateCreditIn(ProcurementRequestDTO):
    pass


class ProcurementCreatePoReleaseIn(ProcurementRequestDTO):
    pass


class ProcurementCreatePurchaseRequestIn(ProcurementRequestDTO):
    requested_by: Any | None = None


class ProcurementCreateRequisitionIn(ProcurementRequestDTO):
    requested_by: Any | None = None


class ProcurementCreateReturnIn(ProcurementRequestDTO):
    pass


class ProcurementCreateRfqIn(ProcurementRequestDTO):
    pass


class ProcurementCreateSpecialOrderLinkIn(ProcurementRequestDTO):
    pass


class ProcurementCreateVendorCategoryIn(ProcurementRequestDTO):
    pass


class ProcurementCreditReturnIn(ProcurementRequestDTO):
    pass


class ProcurementDecidePurchaseRequestIn(ProcurementRequestDTO):
    decision: Any | None = None
    comment: Any | None = None


class ProcurementDecideRequisitionIn(ProcurementRequestDTO):
    decision: Any | None = None
    comment: Any | None = None


class ProcurementFulfilDropshipIn(ProcurementRequestDTO):
    pass


class ProcurementInactivateVendorIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementMatchBillIn(ProcurementRequestDTO):
    override: Any | None = None
    override_reason: Any | None = None


class ProcurementMergeVendorsIn(ProcurementRequestDTO):
    target_vendor_id: Any | None = None
    target: Any | None = None


class ProcurementOrderRequisitionIn(ProcurementRequestDTO):
    pass


class ProcurementPatchContractIn(ProcurementRequestDTO):
    version: Any | None = None


class ProcurementPatchItemVendorIn(ProcurementRequestDTO):
    pass


class ProcurementPatchRequisitionIn(ProcurementRequestDTO):
    pass


class ProcurementPatchVendorCategoryIn(ProcurementRequestDTO):
    pass


class ProcurementPostBillIn(ProcurementRequestDTO):
    pass


class ProcurementPostCreditIn(ProcurementRequestDTO):
    pass


class ProcurementPostReceiptIn(ProcurementRequestDTO):
    pass


class ProcurementRegisterRfqResponseIn(ProcurementRequestDTO):
    pass


class ProcurementRejectPurchaseOrderIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementReverseReceiptIn(ProcurementRequestDTO):
    reason: Any | None = None


class ProcurementShipReturnIn(ProcurementRequestDTO):
    lines: Any | None = None
