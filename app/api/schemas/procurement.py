"""Procurement boundary DTOs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page


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
    pass


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
    details: list[dict] | None = None


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
