"""Costing boundary DTOs (native FastAPI response/request models)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from app.api.schemas.common import APIModel, Page


class CostingDTO(APIModel):
    """Domain base: declared columns are typed; forward-compatible service additions pass through."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemCostOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    location_costing_group_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    costing_method: str | None = None
    qty_on_hand: Decimal | None = None
    value_base: Decimal | None = None
    avg_cost: Decimal | None = None
    last_purchase_price: Decimal | None = None
    last_cost_ledger_seq: int | None = None
    status: str | None = None
    cost_provisional: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostLayerOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_cost_id: UUID | None = None
    layer_seq: int | None = None
    source_inventory_transaction_line_id: UUID | None = None
    layer_date: date | None = None
    qty_original: Decimal | None = None
    qty_remaining: Decimal | None = None
    unit_cost: Decimal | None = None
    value_remaining: Decimal | None = None
    status: str | None = None
    derived_from_layer_id: UUID | None = None
    estimated: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostLayerConsumptionOut(CostingDTO):
    tenant_id: UUID | None = None
    id: UUID | None = None
    cost_layer_id: UUID | None = None
    consuming_transaction_line_id: UUID | None = None
    qty: Decimal | None = None
    value: Decimal | None = None
    consumption_seq: int | None = None
    cost_ledger_entry_id: UUID | None = None
    created_at: datetime | None = None


class CostLedgerEntryOut(CostingDTO):
    tenant_id: UUID | None = None
    id: UUID | None = None
    entry_seq: int | None = None
    inventory_transaction_id: UUID | None = None
    inventory_transaction_line_id: UUID | None = None
    item_cost_id: UUID | None = None
    movement_type: str | None = None
    qty_delta: Decimal | None = None
    unit_cost: Decimal | None = None
    value_delta: Decimal | None = None
    costing_method: str | None = None
    layer_id: UUID | None = None
    cost_run_id: UUID | None = None
    estimated: bool | None = None
    rounding_residual: bool | None = None
    effective_date: date | None = None
    fiscal_period_id: UUID | None = None
    period_shifted: bool | None = None
    reverses_entry_id: UUID | None = None
    adjusts_period_id: UUID | None = None


class CostCategoryOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    category_type: str | None = None
    is_rollup_input: bool | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostTemplateOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    item_type_id: UUID | None = None
    item_category_id: UUID | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostTemplateLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cost_template_id: UUID | None = None
    cost_category_id: UUID | None = None
    percentage: Decimal | None = None
    fixed_amount: Decimal | None = None
    sort_order: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostVarianceOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    variance_type: str | None = None
    item_id: UUID | None = None
    inventory_transaction_id: UUID | None = None
    inventory_transaction_line_id: UUID | None = None
    vendor_bill_line_id: UUID | None = None
    work_order_id: UUID | None = None
    amount_base: Decimal | None = None
    account_ref: str | None = None
    fiscal_period_id: UUID | None = None
    status: str | None = None
    detail: Any | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CogsAdjustmentOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    adjustment_type: str | None = None
    status: str | None = None
    reason: str | None = None
    source_cost_run_id: UUID | None = None
    posted_transaction_id: UUID | None = None
    total_value_base: Decimal | None = None
    document_number: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CogsAdjustmentLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cogs_adjustment_id: UUID | None = None
    item_id: UUID | None = None
    item_cost_id: UUID | None = None
    original_transaction_line_id: UUID | None = None
    adjusts_period_id: UUID | None = None
    value_delta: Decimal | None = None
    estimated: bool | None = None
    posted_transaction_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class LandedCostOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    vendor_id: UUID | None = None
    vendor_bill_id: UUID | None = None
    document_number: str | None = None
    transaction_date: date | None = None
    allocation_basis: str | None = None
    status: str | None = None
    total_amount: Decimal | None = None
    currency_code: str | None = None
    estimated: bool | None = None
    receipt_refs: list[Any] | Any | None = None
    posted_transaction_id: UUID | None = None
    fiscal_period_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class LandedCostLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    landed_cost_id: UUID | None = None
    line_number: int | None = None
    cost_category_id: UUID | None = None
    landed_cost_item_id: UUID | None = None
    description: str | None = None
    amount: Decimal | None = None
    currency_code: str | None = None
    allocation_basis_override: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class LandedCostAllocationOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    landed_cost_id: UUID | None = None
    landed_cost_line_id: UUID | None = None
    item_receipt_line_id: UUID | None = None
    item_id: UUID | None = None
    qty_basis: Decimal | None = None
    weight_basis: Decimal | None = None
    amount_basis: Decimal | None = None
    allocated_amount: Decimal | None = None
    cost_layer_id: UUID | None = None
    cost_ledger_entry_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class EstimatedLandedCostTemplateOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_default: bool | None = None
    is_active: bool | None = None
    currency_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class EstimatedLandedCostTemplateLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    template_id: UUID | None = None
    cost_category_id: UUID | None = None
    allocation_basis: str | None = None
    default_amount: Decimal | None = None
    default_percent: Decimal | None = None
    sort_order: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class LocationCostingGroupOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    currency_code: str | None = None
    include_in_transit_value: bool | None = None
    status: str | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class LocationCostingGroupMemberOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    location_costing_group_id: UUID | None = None
    location_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class GroupAverageBalanceOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    location_costing_group_id: UUID | None = None
    qty_on_hand: Decimal | None = None
    value_base: Decimal | None = None
    avg_cost: Decimal | None = None
    last_cost_ledger_seq: int | None = None
    status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostVersionOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    status: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    activated_at: datetime | None = None
    activated_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PlannedStandardCostOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cost_version_id: UUID | None = None
    item_id: UUID | None = None
    cost_category_id: UUID | None = None
    unit_cost: Decimal | None = None
    is_approved: bool | None = None
    approved_at: datetime | None = None
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class StandardCostOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    cost_category_id: UUID | None = None
    cost_version_id: UUID | None = None
    unit_cost: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostRollupRunOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cost_version_id: UUID | None = None
    scope: Any | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    max_level: int | None = None
    error: str | None = None
    material_cost_type: str | None = None
    include_conversion_costs: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostRollupLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cost_rollup_run_id: UUID | None = None
    item_id: UUID | None = None
    cost_category_id: UUID | None = None
    bom_level: int | None = None
    old_cost: Decimal | None = None
    new_cost: Decimal | None = None
    delta: Decimal | None = None
    breakdown: Any | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostRevaluationOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    revaluation_type: str | None = None
    cost_version_id: UUID | None = None
    scope: Any | None = None
    status: str | None = None
    posted_transaction_id: UUID | None = None
    reason: str | None = None
    document_number: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CostRevaluationLineOut(CostingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    cost_revaluation_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    work_order_id: UUID | None = None
    old_cost: Decimal | None = None
    new_cost: Decimal | None = None
    qty: Decimal | None = None
    value_delta: Decimal | None = None
    inventory_transaction_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PlannedStandardUpsertOut(CostingDTO):
    version_id: str | None = None
    count: int | None = None
    items: list[PlannedStandardCostOut] | None = None


class CostingActionOut(CostingDTO):
    id: str | None = None
    status: str | None = None
    document_number: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    job_run_id: str | None = None
    message: str | None = None


class CostingValuationOut(CostingDTO):
    items: list[ItemCostOut] | None = None
    totals: Any | None = None
    count: int | None = None


class CostingLayerValuationOut(CostingDTO):
    items: list[CostLayerOut] | None = None
    totals: Any | None = None
    count: int | None = None


class CostingPendingRecalculationOut(CostingDTO):
    items: list[Any] | None = None
    count: int | None = None


class EstimatedLandedCostOut(CostingDTO):
    items: list[LandedCostAllocationOut] | None = None
    count: int | None = None


class RollupResultOut(CostingDTO):
    run_id: str | None = None
    status: str | None = None
    line_count: int | None = None


class RecalculationResultOut(CostingDTO):
    job_run_id: str | None = None
    status: str | None = None
    deduplicated: bool | None = None


class ReconciliationOut(CostingDTO):
    items: list[Any] | None = None
    unmatched: list[Any] | None = None
    totals: Any | None = None
    as_of: str | None = None
    balanced: bool | None = None


class ItemCostPage(Page[ItemCostOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostLayerPage(Page[CostLayerOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostLayerConsumptionPage(Page[CostLayerConsumptionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostCategoryPage(Page[CostCategoryOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostTemplatePage(Page[CostTemplateOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostVersionPage(Page[CostVersionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PlannedStandardCostPage(Page[PlannedStandardCostOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class StandardCostPage(Page[StandardCostOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostRollupPage(Page[CostRollupRunOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostRollupLinePage(Page[CostRollupLineOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostRevaluationPage(Page[CostRevaluationOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostVariancePage(Page[CostVarianceOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class LandedCostPage(Page[LandedCostOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class LandedCostAllocationPage(Page[LandedCostAllocationOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class EstimatedLandedCostTemplatePage(Page[EstimatedLandedCostTemplateOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CogsAdjustmentPage(Page[CogsAdjustmentOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class LocationCostingGroupPage(Page[LocationCostingGroupOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class GroupAverageBalancePage(Page[GroupAverageBalanceOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CostingRequestDTO(CostingDTO):
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


class CostingAddGroupMemberIn(CostingRequestDTO):
    location_id: Any | None = None


class CostingApproveVersionIn(CostingRequestDTO):
    pass


class CostingCreateEstimatedLandedCostIn(CostingRequestDTO):
    pass


class CostingReconcileIn(CostingRequestDTO):
    pass


class CostingRequestRecalculationIn(CostingRequestDTO):
    pass


class CostingUpdateElcTemplateIn(CostingRequestDTO):
    pass


class CostingUpdateTemplateIn(CostingRequestDTO):
    pass
