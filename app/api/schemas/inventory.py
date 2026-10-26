"""Inventory-ledger boundary DTOs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page


class AdjustmentLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_adjustment_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    uom_id: UUID | None = None
    qty_delta: Decimal | None = None
    unit_cost: Decimal | None = None
    value_delta: Decimal | None = None
    reason_code_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class AdjustmentOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str
    status: str
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    reason_code_id: UUID | None = None
    adjustment_account_ref: str | None = None
    adjustment_source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[AdjustmentLineOut] = Field(default_factory=list)


class AdjustmentSummaryOut(APIModel):
    id: UUID
    document_number: str
    status: str
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    adjustment_source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class AdjustmentPage(Page[AdjustmentSummaryOut]):
    pass


class AdjustmentCreateLine(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    qty_delta: Decimal
    unit_cost: Decimal | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    ownership: str | None = None


class AdjustmentCreate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    reason_code_id: UUID | None = None
    document_number: str | None = None
    lines: list[AdjustmentCreateLine]


class AdjustmentUpdate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    reason_code_id: UUID | None = None
    lines: list[AdjustmentCreateLine] | None = None


class TransferLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_transfer_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID
    uom_id: UUID | None = None
    qty: Decimal | None = None
    bin_from_id: UUID | None = None
    bin_to_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class TransferOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str
    status: str
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[TransferLineOut] = Field(default_factory=list)


class TransferSummaryOut(APIModel):
    id: UUID
    document_number: str
    status: str
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class TransferPage(Page[TransferSummaryOut]):
    pass


class TransferCreateLine(APIModel):
    item_id: UUID
    qty: Decimal
    uom_id: UUID | None = None
    bin_from_id: UUID | None = None
    bin_to_id: UUID | None = None
    inventory_status_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    ownership: str | None = None


class TransferCreate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    from_location_id: UUID
    to_location_id: UUID
    document_number: str | None = None
    lines: list[TransferCreateLine]


class TransferUpdate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    lines: list[TransferCreateLine] | None = None


class LedgerActionOut(APIModel):
    id: str | None = None
    status: str | None = None
    transaction_id: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    document_number: str | None = None
