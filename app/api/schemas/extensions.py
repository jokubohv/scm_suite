"""Extensions boundary DTOs (native FastAPI response/request models)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from app.api.schemas.common import APIModel, Page


class ExtensionsDTO(APIModel):
    """Domain base: declared columns are typed; forward-compatible service additions pass through."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemWarrantyOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    warranty_duration_months: int | None = None
    warranty_terms: str | None = None
    coverage: Any | None = None
    is_default_for_item: bool | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class WarrantyRegistrationOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    registration_number: str | None = None
    item_id: UUID | None = None
    serial_id: UUID | None = None
    external_customer_ref: str | None = None
    external_invoice_ref: str | None = None
    registered_at: datetime | None = None
    warranty_start: date | None = None
    warranty_end: date | None = None
    source: str | None = None
    status: str | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class WarrantyClaimOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    claim_number: str | None = None
    warranty_registration_id: UUID | None = None
    item_id: UUID | None = None
    serial_id: UUID | None = None
    claim_type: str | None = None
    status: str | None = None
    description: str | None = None
    received_at: datetime | None = None
    validation_result: Any | None = None
    vendor_id: UUID | None = None
    external_return_auth_ref: str | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class WarrantyClaimActionOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    warranty_claim_id: UUID | None = None
    sequence: int | None = None
    action_type: str | None = None
    status: str | None = None
    performed_by: UUID | None = None
    performed_at: datetime | None = None
    notes: str | None = None
    external_credit_ref: str | None = None
    replacement_serial_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class WarrantyRepairOrderOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    repair_order_number: str | None = None
    warranty_claim_id: UUID | None = None
    item_id: UUID | None = None
    serial_id: UUID | None = None
    location_id: UUID | None = None
    status: str | None = None
    labor_cost: Decimal | None = None
    parts_cost: Decimal | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CustomerPartOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    external_customer_ref: str | None = None
    customer_part_number: str | None = None
    description: str | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class UpcRegistryOut(ExtensionsDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    upc_code: str | None = None
    assignment_date: date | None = None
    retirement_date: date | None = None
    status: str | None = None
    assignment_source: str | None = None
    checksum_valid: bool | None = None
    retirement_reason: str | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ExtensionActionOut(ExtensionsDTO):
    id: str | None = None
    status: str | None = None
    posted_transaction_id: str | None = None
    message: str | None = None


class ExtensionSettingOut(ExtensionsDTO):
    key: str | None = None
    value: Any | None = None


class ExtensionSettingsOut(ExtensionsDTO):
    items: list[ExtensionSettingOut] | None = None


class WarrantyForItemOut(ExtensionsDTO):
    item_id: str | None = None
    item_number: str | None = None
    warranty: ItemWarrantyOut | None = None


class CoverageOut(ExtensionsDTO):
    covered: bool | None = None
    reason: str | None = None
    registration_id: str | None = None
    warranty_start: str | None = None
    warranty_end: str | None = None
    as_of: str | None = None


class ClaimValidationOut(ExtensionsDTO):
    pass_: bool | None = None
    valid: bool | None = None
    reasons: list[Any] | None = None
    as_of: str | None = None
    override: Any | None = None


class CustomerPartResolveOut(ExtensionsDTO):
    item_id: str | None = None
    item_number: str | None = None
    mapping: CustomerPartOut | None = None


class UpcValidationOut(ExtensionsDTO):
    upc_code: str | None = None
    valid: bool | None = None
    registered: bool | None = None
    status: str | None = None
    reasons: list[Any] | None = None


class UpcMonitoringOut(ExtensionsDTO):
    assigned: int | None = None
    retired: int | None = None
    eligible_items_missing_upc: int | None = None
    checksum_failures: int | None = None
    auto_assign_enabled: bool | None = None


class UpcBatchResultOut(ExtensionsDTO):
    assigned: int | None = None
    skipped: int | None = None
    failed: int | None = None


class ImportResultOut(ExtensionsDTO):
    rows: list[Any] | None = None
    succeeded: int | None = None
    failed: int | None = None


class WarrantyRegistrationPage(Page[WarrantyRegistrationOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class WarrantyClaimPage(Page[WarrantyClaimOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class WarrantyRepairOrderPage(Page[WarrantyRepairOrderOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class WarrantyClaimActionPage(Page[WarrantyClaimActionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CustomerPartPage(Page[CustomerPartOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class UpcRegistryPage(Page[UpcRegistryOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ExtensionsRequestDTO(ExtensionsDTO):
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


class ExtensionsAddClaimActionIn(ExtensionsRequestDTO):
    pass


class ExtensionsApproveClaimIn(ExtensionsRequestDTO):
    approval_ref: Any | None = None
    override_reason: Any | None = None


class ExtensionsAssignBatchIn(ExtensionsRequestDTO):
    limit: Any | None = None


class ExtensionsAssignUpcIn(ExtensionsRequestDTO):
    pass


class ExtensionsCancelClaimIn(ExtensionsRequestDTO):
    reason: Any | None = None


class ExtensionsCompleteClaimIn(ExtensionsRequestDTO):
    notes: Any | None = None


class ExtensionsCreateClaimIn(ExtensionsRequestDTO):
    pass


class ExtensionsCreateCustomerPartIn(ExtensionsRequestDTO):
    pass


class ExtensionsCreateRegistrationIn(ExtensionsRequestDTO):
    pass


class ExtensionsCreateRepairOrderIn(ExtensionsRequestDTO):
    pass


class ExtensionsImportCustomerPartsIn(ExtensionsRequestDTO):
    rows: Any | None = None


class ExtensionsImportRegistrationsIn(ExtensionsRequestDTO):
    rows: Any | None = None


class ExtensionsPatchCustomerPartIn(ExtensionsRequestDTO):
    pass


class ExtensionsPatchRepairOrderIn(ExtensionsRequestDTO):
    pass


class ExtensionsPatchRepairOrderCollectionIn(ExtensionsRequestDTO):
    id: Any | None = None
    repair_id: Any | None = None


class ExtensionsPatchSettingsIn(ExtensionsRequestDTO):
    pass


class ExtensionsPutItemWarrantyIn(ExtensionsRequestDTO):
    pass


class ExtensionsRejectClaimIn(ExtensionsRequestDTO):
    reason: Any | None = None


class ExtensionsRetireUpcIn(ExtensionsRequestDTO):
    pass


class ExtensionsValidateClaimIn(ExtensionsRequestDTO):
    as_of: Any | None = None
    override_ref: Any | None = None


class ExtensionsVoidRegistrationIn(ExtensionsRequestDTO):
    reason: Any | None = None
