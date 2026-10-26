"""Consigned boundary DTOs (native FastAPI response/request models)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from app.api.schemas.common import APIModel, Page


class ConsignedDTO(APIModel):
    """Domain base: declared columns are typed; forward-compatible service additions pass through."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ConsignmentAgreementOut(ConsignedDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    agreement_number: str | None = None
    vendor_id: UUID | None = None
    status: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    payment_trigger: str | None = None
    currency_code: str | None = None
    terms: Any | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ConsignmentAgreementItemOut(ConsignedDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    consignment_agreement_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    consigned_status_id: UUID | None = None
    owned_status_id: UUID | None = None
    unit_cost: Decimal | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ConsignmentOwnershipTransferOut(ConsignedDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    document_number: str | None = None
    consignment_agreement_id: UUID | None = None
    vendor_id: UUID | None = None
    transfer_type: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    external_ref: str | None = None
    posted_transaction_id: UUID | None = None
    vendor_bill_id: UUID | None = None
    reversal_of_transfer_id: UUID | None = None
    memo: str | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ConsignmentOwnershipTransferLineOut(ConsignedDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    consignment_ownership_transfer_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    from_ownership: str | None = None
    to_ownership: str | None = None
    from_status_id: UUID | None = None
    to_status_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[Any] | None = None
    unit_cost: Decimal | None = None
    value_base: Decimal | None = None
    bin_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ConsignmentConsumptionOut(ConsignedDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    consignment_agreement_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    consumption_date: date | None = None
    period_key: str | None = None
    qty_consumed: Decimal | None = None
    value_base: Decimal | None = None
    source_transfer_id: UUID | None = None
    source_transfer_line_id: UUID | None = None
    vendor_bill_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ConsignedActionOut(ConsignedDTO):
    id: str | None = None
    status: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    vendor_bill_id: str | None = None
    message: str | None = None


class AgreementDetailOut(ConsignedDTO):
    id: UUID | None = None
    agreement_number: str | None = None
    vendor_id: UUID | None = None
    status: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    payment_trigger: str | None = None
    currency_code: str | None = None
    terms: Any | None = None
    version: int | None = None
    items: list[ConsignmentAgreementItemOut] | None = None


class TransferDetailOut(ConsignedDTO):
    id: UUID | None = None
    document_number: str | None = None
    consignment_agreement_id: UUID | None = None
    vendor_id: UUID | None = None
    transfer_type: str | None = None
    status: str | None = None
    transaction_date: date | None = None
    posted_transaction_id: UUID | None = None
    vendor_bill_id: UUID | None = None
    version: int | None = None
    lines: list[ConsignmentOwnershipTransferLineOut] | None = None


class ConsumptionResultOut(ConsignedDTO):
    id: str | None = None
    status: str | None = None
    vendor_bill_id: str | None = None
    posted_transaction_id: str | None = None
    consumption: list[ConsignmentConsumptionOut] | None = None
    lines: list[Any] | None = None


class ConsignedBalancesOut(ConsignedDTO):
    owned: list[Any] | None = None
    consigned: list[Any] | None = None
    inv_27_separated: bool | None = None
    as_of: str | None = None


class ConsignedEligibilityOut(ConsignedDTO):
    eligible: bool | None = None
    reasons: list[Any] | None = None
    agreement_id: str | None = None


class ConsignedGuardOut(ConsignedDTO):
    ok: bool | None = None
    reasons: list[Any] | None = None
    guard_enabled: bool | None = None


class ConsignedSettingOut(ConsignedDTO):
    key: str | None = None
    value: Any | None = None


class ConsignedSettingsOut(ConsignedDTO):
    items: list[ConsignedSettingOut] | None = None


class ConsignmentAgreementPage(Page[ConsignmentAgreementOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ConsignmentAgreementItemPage(Page[ConsignmentAgreementItemOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ConsignmentOwnershipTransferPage(Page[ConsignmentOwnershipTransferOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ConsignmentConsumptionPage(Page[ConsignmentConsumptionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ConsignedRequestDTO(ConsignedDTO):
    """Typed request base: common optional boundary fields; extra fields pass through for forward compatibility."""

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
    customer_id: Any | None = None
    category_id: Any | None = None
    group_id: Any | None = None
    collection_id: Any | None = None
    price_level_id: Any | None = None
    currency_code: Any | None = None
    exchange_rate: Any | None = None
    amount: Any | None = None
    amounts: Any | None = None
    qty: Any | None = None
    quantity: Any | None = None
    qty_min: Any | None = None
    qty_max: Any | None = None
    unit_price: Any | None = None
    price: Any | None = None
    value: Any | None = None
    percent: Any | None = None
    effective_from: Any | None = None
    effective_to: Any | None = None
    effective_start: Any | None = None
    effective_end: Any | None = None
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


class ConsignedActivateAgreementIn(ConsignedRequestDTO):
    pass


class ConsignedAddAgreementItemIn(ConsignedRequestDTO):
    pass


class ConsignedApproveTransferIn(ConsignedRequestDTO):
    approval_ref: Any | None = None


class ConsignedConsumeIn(ConsignedRequestDTO):
    pass


class ConsignedCreateAgreementIn(ConsignedRequestDTO):
    pass


class ConsignedCreateTransferIn(ConsignedRequestDTO):
    pass


class ConsignedPatchAgreementIn(ConsignedRequestDTO):
    version: Any | None = None


class ConsignedPatchAgreementItemIn(ConsignedRequestDTO):
    pass


class ConsignedPatchSettingsIn(ConsignedRequestDTO):
    pass


class ConsignedPostTransferIn(ConsignedRequestDTO):
    external_ref: Any | None = None


class ConsignedReverseTransferIn(ConsignedRequestDTO):
    reason: Any | None = None


class ConsignedSuspendAgreementIn(ConsignedRequestDTO):
    reason: Any | None = None


class ConsignedTerminateAgreementIn(ConsignedRequestDTO):
    reason: Any | None = None
    disposition_plan: Any | None = None


class ConsignedVoidTransferIn(ConsignedRequestDTO):
    reason: Any | None = None
