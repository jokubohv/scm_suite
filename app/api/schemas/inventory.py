"""Inventory-ledger boundary DTOs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
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


# =====================================================================================
# Transactions / posting
# =====================================================================================
class TransactionLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_transaction_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_class: str | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    uom_id: UUID | None = None
    qty_uom: Decimal | None = None
    conversion_factor: Decimal | None = None
    qty_base: Decimal | None = None
    value_base: Decimal | None = None
    value_txn: Decimal | None = None
    unit_cost: Decimal | None = None
    movement_type: str | None = None
    movement_direction: str | None = None
    reason_code_id: UUID | None = None
    source_line_ref: str | None = None
    class_ref: str | None = None
    department_ref: str | None = None
    subsidiary_ref: str | None = None
    cost_object_id: UUID | None = None
    cost_estimated: bool | None = None
    transaction_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class TransactionDetailOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_transaction_id: UUID | None = None
    inventory_transaction_line_id: UUID | None = None
    transaction_date: date | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_base: Decimal | None = None
    value_base: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class TransactionOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    transaction_type: str | None = None
    transaction_date: date | None = None
    posting_at: datetime | None = None
    ledger_sequence: int | None = None
    source_doc_type: str | None = None
    source_doc_id: UUID | None = None
    external_ref: str | None = None
    reversal_of_transaction_id: UUID | None = None
    reversal_reason: str | None = None
    fiscal_period_id: UUID | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    total_qty_base: Decimal | None = None
    total_value_base: Decimal | None = None
    is_backdated: bool | None = None
    period_shifted: bool | None = None
    cost_estimated: bool | None = None
    backdated_original_date: date | None = None
    idempotency_key: str | None = None
    import_batch_id: UUID | None = None
    correlation_id: UUID | None = None
    memo: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class TransactionPage(Page[TransactionOut]):
    pass


class TransactionLinePage(Page[TransactionLineOut]):
    pass


class TransactionDetailPage(Page[TransactionDetailOut]):
    pass


class PostingDetailIn(APIModel):
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty: Decimal | None = None
    qty_base: Decimal | None = None
    value_base: Decimal | None = None


class PostingMovementIn(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_class: str | None = None
    qty: Decimal | None = None
    qty_base: Decimal | None = None
    qty_uom: Decimal | None = None
    conversion_factor: Decimal | None = None
    value_base: Decimal | None = None
    value_txn: Decimal | None = None
    unit_cost: Decimal | None = None
    movement_type: str | None = None
    movement_direction: str | None = None
    reason_code_id: UUID | None = None
    source_line_ref: str | None = None
    class_ref: str | None = None
    department_ref: str | None = None
    subsidiary_ref: str | None = None
    cost_object_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    uom_id: UUID | None = None
    details: list[PostingDetailIn] | None = None


class PostingCommandIn(APIModel):
    posting_type: str | None = None
    transaction_type: str | None = None
    transaction_date: date | None = None
    source_doc_type: str | None = None
    source_doc_id: UUID | None = None
    external_ref: str | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    correlation_id: UUID | None = None
    import_batch_id: UUID | None = None
    memo: str | None = None
    negative_policy_override: bool | None = None
    allow_unallocated: bool | None = None
    movements: list[PostingMovementIn] | None = None
    lines: list[PostingMovementIn] | None = None
    reversal_of_transaction_id: UUID | None = None
    reversal_reason: str | None = None


class ReasonRequest(APIModel):
    reason: str | None = None


class AdjustmentPostRequest(APIModel):
    transaction_date: date | None = None


# =====================================================================================
# Worksheets / transfer orders
# =====================================================================================
class WorksheetLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_worksheet_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    system_qty: Decimal | None = None
    new_qty: Decimal | None = None
    variance_qty: Decimal | None = None
    unit_cost: Decimal | None = None
    reason_code_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class WorksheetOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    worksheet_type: str | None = None
    default_account_ref: str | None = None
    count_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[WorksheetLineOut] = Field(default_factory=list)


class WorksheetSummaryOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    worksheet_type: str | None = None
    count_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class WorksheetPage(Page[WorksheetSummaryOut]):
    pass


class WorksheetLineIn(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    new_qty: Decimal
    unit_cost: Decimal | None = None
    reason_code_id: UUID | None = None


class WorksheetCreate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    document_number: str | None = None
    count_date: date | None = None
    lines: list[WorksheetLineIn]


class WorksheetUpdate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    worksheet_type: str | None = None
    count_date: date | None = None
    default_account_ref: str | None = None
    lines: list[WorksheetLineIn] | None = None


class TransferOrderLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    transfer_order_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    uom_id: UUID | None = None
    qty_requested: Decimal | None = None
    qty_committed: Decimal | None = None
    qty_fulfilled: Decimal | None = None
    qty_in_transit: Decimal | None = None
    qty_received: Decimal | None = None
    qty_closed: Decimal | None = None
    bin_from_id: UUID | None = None
    bin_to_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    required_by_date: date | None = None
    is_closed: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class TransferOrderOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    fulfilment_status: str | None = None
    receipt_status: str | None = None
    in_transit_ownership: str | None = None
    transfer_price: Decimal | None = None
    approved_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[TransferOrderLineOut] = Field(default_factory=list)


class TransferOrderSummaryOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    fulfilment_status: str | None = None
    receipt_status: str | None = None
    in_transit_ownership: str | None = None
    transfer_price: Decimal | None = None
    approved_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class TransferOrderPage(Page[TransferOrderSummaryOut]):
    pass


class TransferOrderLinePage(Page[TransferOrderLineOut]):
    pass


class TransferOrderLineIn(APIModel):
    item_id: UUID
    qty_requested: Decimal
    uom_id: UUID | None = None
    bin_from_id: UUID | None = None
    bin_to_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    required_by_date: date | None = None


class TransferOrderCreate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    document_number: str | None = None
    from_location_id: UUID
    to_location_id: UUID
    in_transit_ownership: str | None = None
    transfer_price: Decimal | None = None
    lines: list[TransferOrderLineIn]


class TransferOrderUpdate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    in_transit_ownership: str | None = None
    transfer_price: Decimal | None = None
    lines: list[TransferOrderLineIn] | None = None


class TransferOrderStageLine(APIModel):
    line_number: int
    qty: Decimal


class TransferOrderStageRequest(APIModel):
    lines: list[TransferOrderStageLine] = Field(default_factory=list)


class CloseRequest(APIModel):
    reason: str | None = None


# =====================================================================================
# Commitments / fulfilments / returns
# =====================================================================================
class CommitmentOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    source_type: str | None = None
    source_line_ref: str | None = None
    external_ref: str | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_committed: Decimal | None = None
    qty_fulfilled: Decimal | None = None
    qty_backordered: Decimal | None = None
    status: str | None = None
    priority: int | None = None
    required_by_date: date | None = None
    committed_at: datetime | None = None
    released_at: datetime | None = None
    released_qty: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class CommitmentPage(Page[CommitmentOut]):
    pass


class CommitmentCreate(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    qty: Decimal
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    external_ref: str | None = None
    expires_at: datetime | None = None
    priority: int | None = None
    required_by_date: date | None = None


class CommitmentReleaseRequest(APIModel):
    qty: Decimal | None = None
    reason: str | None = None


class CommitmentReallocateRequest(APIModel):
    item_id: UUID
    location_id: UUID


class ReallocateOut(APIModel):
    moved: list[str] = Field(default_factory=list)
    status: str | None = None


class FulfilmentLineIn(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    qty: Decimal
    unit_cost: Decimal | None = None
    commitment_id: UUID | None = None


class FulfilmentCreate(APIModel):
    source_type: str | None = None
    source_ref: str | None = None
    location_id: UUID | None = None
    transaction_date: date | None = None
    external_ref: str | None = None
    lines: list[FulfilmentLineIn] = Field(default_factory=list)


class FulfilmentResultOut(APIModel):
    transaction_id: str | None = None
    fulfilled_qty: Decimal | None = None
    backordered_qty: Decimal | None = None


class ReturnLineIn(APIModel):
    item_id: UUID
    location_id: UUID
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    qty: Decimal
    unit_cost: Decimal | None = None


class ReturnCreate(APIModel):
    return_type: str | None = None
    location_id: UUID | None = None
    transaction_date: date | None = None
    external_ref: str | None = None
    memo: str | None = None
    lines: list[ReturnLineIn] = Field(default_factory=list)


class ReturnResultOut(APIModel):
    transaction_id: str | None = None


# =====================================================================================
# Balances / availability / in-transit
# =====================================================================================
class BalanceOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_class: str | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    qty_on_hand: Decimal | None = None
    qty_committed: Decimal | None = None
    qty_on_order: Decimal | None = None
    qty_backordered: Decimal | None = None
    value_base: Decimal | None = None
    last_ledger_sequence: int | None = None
    last_posting_at: datetime | None = None
    qty_available: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class BalancePage(Page[BalanceOut]):
    pass


class BalanceBinOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    qty_on_hand: Decimal | None = None
    last_ledger_sequence: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class BalanceBinPage(Page[BalanceBinOut]):
    pass


class BalanceLotOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    lot_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    qty_on_hand: Decimal | None = None
    qty_committed: Decimal | None = None
    expiry_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class BalanceLotPage(Page[BalanceLotOut]):
    pass


class AvailabilityOut(APIModel):
    item_id: str | None = None
    location_id: str | None = None
    on_hand: Decimal | None = None
    committed: Decimal | None = None
    available: Decimal | None = None
    on_order: Decimal | None = None
    backordered: Decimal | None = None
    in_transit: Decimal | None = None


class InTransitOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    transfer_order_line_id: UUID | None = None
    item_id: UUID | None = None
    from_location_id: UUID | None = None
    to_location_id: UUID | None = None
    qty_in_transit: Decimal | None = None
    value_base: Decimal | None = None
    fulfilled_at: datetime | None = None
    expected_receipt_date: date | None = None
    status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class InTransitPage(Page[InTransitOut]):
    pass


# =====================================================================================
# Distributions / imports / reason codes / bulk edits
# =====================================================================================
class DistributionLineOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    inventory_distribution_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    to_location_id: UUID | None = None
    inventory_status_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    qty: Decimal | None = None
    unit_cost: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class DistributionOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    distribution_mode: str | None = None
    bootstrap_mode: bool | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None
    lines: list[DistributionLineOut] = Field(default_factory=list)


class DistributionSummaryOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    document_number: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    memo: str | None = None
    posted_transaction_id: UUID | None = None
    distribution_mode: str | None = None
    bootstrap_mode: bool | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class DistributionPage(Page[DistributionSummaryOut]):
    pass


class DistributionLineIn(APIModel):
    item_id: UUID
    to_location_id: UUID
    inventory_status_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    qty: Decimal
    unit_cost: Decimal | None = None


class DistributionCreate(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    document_number: str | None = None
    distribution_mode: str | None = None
    bootstrap_mode: bool | None = None
    lines: list[DistributionLineIn]


class ImportBatchOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    import_type: str | None = None
    status: str | None = None
    filename: str | None = None
    storage_key: str | None = None
    row_count: int | None = None
    success_count: int | None = None
    error_count: int | None = None
    idempotency_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    options: Any | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class ImportBatchPage(Page[ImportBatchOut]):
    pass


class ImportRowOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    import_batch_id: UUID | None = None
    row_number: int | None = None
    raw_row: Any | None = None
    status: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    entity_type: str | None = None
    entity_id: UUID | None = None
    inventory_transaction_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class ImportRowPage(Page[ImportRowOut]):
    pass


class ImportBatchCreate(APIModel):
    import_type: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    filename: str | None = None
    file_name: str | None = None
    storage_key: str | None = None
    options: dict[str, Any] | None = None
    memo: str | None = None


class ImportValidationOut(APIModel):
    status: str | None = None
    valid_count: int | None = None
    error_count: int | None = None


class ImportPostResultOut(APIModel):
    status: str | None = None
    posted_count: int | None = None
    failed_count: int | None = None


class ReasonCodeOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    reason_type: str | None = None
    account_ref: str | None = None
    is_system: bool | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class ReasonCodePage(Page[ReasonCodeOut]):
    pass


class ReasonCodeCreate(APIModel):
    code: str
    name: str | None = None
    reason_type: str | None = None
    account_ref: str | None = None
    is_active: bool | None = None


class ReasonCodeUpdate(APIModel):
    name: str | None = None
    account_ref: str | None = None
    is_active: bool | None = None


class AdjustmentPatchLine(AdjustmentCreateLine):
    serial_id: UUID | None = None


class AdjustmentPatch(APIModel):
    transaction_date: date | None = None
    memo: str | None = None
    reason_code_id: UUID | None = None
    adjustment_source: str | None = None
    lines: list[AdjustmentPatchLine] | None = None


class BulkPatchIn(APIModel):
    ids: list[UUID] = Field(default_factory=list)
    patch: dict[str, Any] = Field(default_factory=dict)
    versions: dict[str, Any] | None = None


class AdjustmentBulkOut(APIModel):
    items: list[AdjustmentOut] = Field(default_factory=list)
    updated: int = 0


class TransferBulkOut(APIModel):
    items: list[TransferOut] = Field(default_factory=list)
    updated: int = 0


class WorksheetBulkOut(APIModel):
    items: list[WorksheetOut] = Field(default_factory=list)
    updated: int = 0


class TransferOrderBulkOut(APIModel):
    items: list[TransferOrderOut] = Field(default_factory=list)
    updated: int = 0
