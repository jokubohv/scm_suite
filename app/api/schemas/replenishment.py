"""Planning/replenishment boundary DTOs (native FastAPI models for `replenishment.planning@1.0.0`).

Responses mirror the service documents (rows from ``replenishment.*`` tables plus documented aggregate
projections). Request models carry only the fields the service reads; unknown extras stay ignored at the
boundary, which matches the legacy dict-body behaviour (the service always filtered its accepted keys).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page

# ==================================================================== response records ========


class PlanningRecord(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PlanningRuleScopeOut(PlanningRecord):
    planning_rule_id: UUID | None = None
    scope_type: str | None = None
    scope_id: UUID | None = None
    include_descendants: bool | None = None


class PlanningRuleOut(PlanningRecord):
    code: str | None = None
    name: str | None = None
    engine: str | None = None
    allowed_supply_types: list[str] | None = None
    lot_sizing: str | None = None
    fixed_order_qty: Decimal | None = None
    min_order_qty: Decimal | None = None
    max_order_qty: Decimal | None = None
    time_fence_days: int | None = None
    is_active: bool | None = None
    params: Any = None
    scopes: list[PlanningRuleScopeOut] = Field(default_factory=list)


class TimeFenceOut(PlanningRecord):
    scope_type: str | None = None
    scope_id: UUID | None = None
    fence_days: int | None = None
    behavior: str | None = None
    demand_change_threshold: Decimal | None = None
    is_active: bool | None = None


class DemandHistoryOut(PlanningRecord):
    item_id: UUID | None = None
    location_id: UUID | None = None
    bucket_date: date | None = None
    bucket_granularity: str | None = None
    qty: Decimal | None = None
    source: str | None = None
    external_ref: str | None = None


class RejectedDemandRowOut(APIModel):
    row: dict[str, Any] | None = None
    error: str | None = None
    message: str | None = None


class DemandHistoryIngestOut(APIModel):
    accepted: list[DemandHistoryOut] = Field(default_factory=list)
    rejected: list[RejectedDemandRowOut] = Field(default_factory=list)
    accepted_count: int | None = None
    rejected_count: int | None = None


class DemandPlanLineOut(PlanningRecord):
    demand_plan_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    bucket_date: date | None = None
    qty_forecast: Decimal | None = None
    qty_manual_override: Decimal | None = None
    qty_final: Decimal | None = None
    method_metadata: Any = None
    is_overridden: bool | None = None


class DemandPlanOut(PlanningRecord):
    plan_number: str | None = None
    method: str | None = None
    horizon_start: date | None = None
    horizon_end: date | None = None
    bucket_granularity: str | None = None
    status: str | None = None
    calculated_at: datetime | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    planning_rule_id: UUID | None = None
    demand_change_threshold: Decimal | None = None
    scope: Any = None
    method_metadata: Any = None
    error_text: str | None = None
    lines: list[DemandPlanLineOut] = Field(default_factory=list)


class SupplyPlanLineOut(PlanningRecord):
    supply_plan_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    bucket_date: date | None = None
    gross_requirement: Decimal | None = None
    scheduled_receipts: Decimal | None = None
    projected_on_hand: Decimal | None = None
    net_requirement: Decimal | None = None
    safety_stock: Decimal | None = None
    is_shortage: bool | None = None


class SupplyPlanSuggestionOut(PlanningRecord):
    supply_plan_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    action_type: str | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    due_date: date | None = None
    source_rule_id: UUID | None = None
    priority: int | None = None
    status: str | None = None
    created_doc_type: str | None = None
    created_doc_id: UUID | None = None
    explanation: Any = None


class SupplyPlanOut(PlanningRecord):
    plan_number: str | None = None
    planning_rule_id: UUID | None = None
    horizon_start: date | None = None
    horizon_end: date | None = None
    scope: Any = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_key: str | None = None
    bucket_granularity: str | None = None
    as_of_date: date | None = None
    input_hash: str | None = None
    error_text: str | None = None
    lines: list[SupplyPlanLineOut] = Field(default_factory=list)
    suggestions: list[SupplyPlanSuggestionOut] = Field(default_factory=list)


class ConvertSuggestionResultOut(APIModel):
    suggestion_id: str | None = None
    status: str | None = None
    created_doc_type: str | None = None
    created_doc_id: str | None = None


class ConvertOrdersOut(APIModel):
    items: list[ConvertSuggestionResultOut] = Field(default_factory=list)
    count: int | None = None
    dry_run: bool | None = None


class PlanningActionMessageOut(PlanningRecord):
    supply_plan_id: UUID | None = None
    supply_plan_suggestion_id: UUID | None = None
    message_type: str | None = None
    severity: str | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    message: str | None = None
    status: str | None = None
    context: Any = None


class DrpNetworkOut(PlanningRecord):
    code: str | None = None
    name: str | None = None
    status: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None


class DrpNetworkMemberOut(PlanningRecord):
    drp_network_id: UUID | None = None
    location_id: UUID | None = None
    level: int | None = None
    parent_member_id: UUID | None = None
    lead_time_days: int | None = None
    is_source: bool | None = None
    priority: int | None = None


class BillOfDistributionOut(PlanningRecord):
    drp_network_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    version: int | None = None
    status: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class BillOfDistributionLevelOut(PlanningRecord):
    bill_of_distribution_id: UUID | None = None
    level: int | None = None
    source_location_id: UUID | None = None
    destination_location_id: UUID | None = None
    transfer_lead_time_days: int | None = None
    is_default_source: bool | None = None


class DistributionCategoryOut(PlanningRecord):
    code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class DistributionCategoryMembersOut(APIModel):
    item_ids: list[UUID] = Field(default_factory=list)
    count: int | None = None


class AllocationStrategyRuleOut(PlanningRecord):
    allocation_strategy_id: UUID | None = None
    sequence: int | None = None
    criterion: str | None = None
    direction: str | None = None
    weight: Decimal | None = None
    is_active: bool | None = None


class AllocationStrategyOut(PlanningRecord):
    code: str | None = None
    name: str | None = None
    method: str | None = None
    priority: int | None = None
    location_scope: Any = None
    include_assembly: bool | None = None
    include_kits: bool | None = None
    fefo_enabled: bool | None = None
    is_active: bool | None = None
    rules: list[AllocationStrategyRuleOut] = Field(default_factory=list)


class AllocationRunOut(PlanningRecord):
    run_number: str | None = None
    allocation_strategy_id: UUID | None = None
    as_of_date: date | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    demand_count: int | None = None
    supply_count: int | None = None
    error_text: str | None = None


class AllocationResultOut(PlanningRecord):
    allocation_run_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    demand_ref: str | None = None
    demand_line_ref: str | None = None
    required_by_date: date | None = None
    supply_type: str | None = None
    supply_ref: str | None = None
    qty_allocated: Decimal | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    status: str | None = None


class AllocationExceptionOut(PlanningRecord):
    allocation_run_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    demand_ref: str | None = None
    demand_line_ref: str | None = None
    exception_type: str | None = None
    shortfall_qty: Decimal | None = None
    message: str | None = None
    status: str | None = None


class CalculationLineOut(PlanningRecord):
    replenishment_calculation_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    avg_daily_demand: Decimal | None = None
    lead_time_days_used: int | None = None
    safety_stock_used: Decimal | None = None
    seasonal_factor: Decimal | None = None
    reorder_point_calculated: Decimal | None = None
    preferred_stock_calculated: Decimal | None = None
    qty_to_order: Decimal | None = None
    action: str | None = None
    applied: bool | None = None
    formula_version: str | None = None
    detail: Any = None


class ReplenishmentCalculationOut(PlanningRecord):
    run_number: str | None = None
    calculation_day: date | None = None
    analysis_interval_days: int | None = None
    seasonal_interval_days: int | None = None
    formula_version: str | None = None
    status: str | None = None
    scope: Any = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lines: list[CalculationLineOut] = Field(default_factory=list)


class CalculationApplyOut(APIModel):
    calculation_id: UUID
    applied_count: int | None = None
    dry_run: bool | None = None


class OrderItemOut(APIModel):
    item_id: UUID | None = None
    location_id: UUID | None = None
    reorder_point: Decimal | None = None
    preferred_stock: Decimal | None = None
    available: Decimal | None = None
    qty_to_order: Decimal | None = None
    status: str | None = None


class AtpProjectionBucketOut(APIModel):
    bucket_date: date | None = None
    policy_version: str | None = None
    begin_qty: Decimal | None = None
    supply_qty: Decimal | None = None
    demand_qty: Decimal | None = None
    projected_available: Decimal | None = None
    earliest_full_date: date | None = None
    shortage_qty: Decimal | None = None
    shortage_date: date | None = None
    last_rebuilt_at: datetime | None = None


class AtpQueryOut(APIModel):
    earliest_date: date | None = None
    can_meet: bool | None = None
    shortage: bool | None = None
    buckets: list[AtpProjectionBucketOut] = Field(default_factory=list)
    method: str | None = None
    policy_version: str | None = None
    stale_seconds: int | None = None


class AtpProjectionOut(APIModel):
    items: list[AtpProjectionBucketOut] = Field(default_factory=list)
    count: int | None = None
    policy_version: str | None = None


class AtpRebuildOut(APIModel):
    rebuilt_count: int
    method: str
    policy_version: str


class PlanningPreferencesOut(APIModel):
    enabled: bool | None = None
    policy_versions: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)


class WorkbenchItemOut(APIModel):
    plans: list[SupplyPlanOut] = Field(default_factory=list)
    open_messages: list[PlanningActionMessageOut] = Field(default_factory=list)


class WorkbenchOut(APIModel):
    items: list[WorkbenchItemOut] = Field(default_factory=list)
    staleness_seconds: int | None = None


# ======================================================================= page wrappers ========


class PlanningRulePage(Page[PlanningRuleOut]):
    pass


class PlanningRuleScopePage(Page[PlanningRuleScopeOut]):
    pass


class TimeFencePage(Page[TimeFenceOut]):
    pass


class DemandHistoryPage(Page[DemandHistoryOut]):
    pass


class DemandPlanLinePage(Page[DemandPlanLineOut]):
    pass


class DemandPlanPage(Page[DemandPlanOut]):
    pass


class SupplyPlanLinePage(Page[SupplyPlanLineOut]):
    pass


class SupplyPlanSuggestionPage(Page[SupplyPlanSuggestionOut]):
    pass


class SupplyPlanPage(Page[SupplyPlanOut]):
    pass


class PlanningActionMessagePage(Page[PlanningActionMessageOut]):
    pass


class DrpNetworkPage(Page[DrpNetworkOut]):
    pass


class DrpNetworkMemberPage(Page[DrpNetworkMemberOut]):
    pass


class BillOfDistributionPage(Page[BillOfDistributionOut]):
    pass


class BillOfDistributionLevelPage(Page[BillOfDistributionLevelOut]):
    pass


class DistributionCategoryPage(Page[DistributionCategoryOut]):
    pass


class AllocationStrategyPage(Page[AllocationStrategyOut]):
    pass


class AllocationStrategyRulePage(Page[AllocationStrategyRuleOut]):
    pass


class AllocationRunPage(Page[AllocationRunOut]):
    pass


class AllocationResultPage(Page[AllocationResultOut]):
    pass


class AllocationExceptionPage(Page[AllocationExceptionOut]):
    pass


class CalculationLinePage(Page[CalculationLineOut]):
    pass


class ReplenishmentCalculationPage(Page[ReplenishmentCalculationOut]):
    pass


class OrderItemPage(Page[OrderItemOut]):
    pass


class ReorderReminderPage(Page[OrderItemOut]):
    pass


# ============================================================== request bodies ================
# Converted from the remaining untyped ``dict[str, Any]`` bodies; fields mirror what the service reads.


class PlanningRulePatch(APIModel):
    name: str | None = None
    allowed_supply_types: list[str] | None = None
    lot_sizing: str | None = None
    fixed_order_qty: Decimal | None = None
    min_order_qty: Decimal | None = None
    max_order_qty: Decimal | None = None
    time_fence_days: int | None = None
    is_active: bool | None = None
    params: dict[str, Any] | None = None


class RuleScopeIn(APIModel):
    scope_type: str | None = None
    scope_id: UUID | None = None
    include_descendants: bool | None = None


class RuleScopesIn(APIModel):
    scopes: list[RuleScopeIn] = Field(default_factory=list)


class TimeFencePatch(APIModel):
    scope_type: str | None = None
    fence_days: int | None = None
    behavior: str | None = None
    demand_change_threshold: Decimal | None = None
    is_active: bool | None = None


class DrpNetworkPatch(APIModel):
    name: str | None = None
    status: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None


class ApprovalIn(APIModel):
    approved_by: UUID | None = None
    reason: str | None = None
    note: str | None = None


class PlanningActionIn(APIModel):
    reason: str | None = None
    note: str | None = None
    memo: str | None = None
    dry_run: bool | None = None
    force: bool | None = None
    as_of: date | None = None
    scope: dict[str, Any] | None = None
    method: str | None = None


__all__ = [
    "AllocationExceptionOut",
    "AllocationExceptionPage",
    "AllocationResultOut",
    "AllocationResultPage",
    "AllocationRunOut",
    "AllocationRunPage",
    "AllocationStrategyOut",
    "AllocationStrategyPage",
    "AllocationStrategyRuleOut",
    "AllocationStrategyRulePage",
    "ApprovalIn",
    "AtpProjectionBucketOut",
    "AtpProjectionOut",
    "AtpQueryOut",
    "AtpRebuildOut",
    "BillOfDistributionLevelOut",
    "BillOfDistributionLevelPage",
    "BillOfDistributionOut",
    "BillOfDistributionPage",
    "CalculationApplyOut",
    "CalculationLineOut",
    "CalculationLinePage",
    "ConvertOrdersOut",
    "ConvertSuggestionResultOut",
    "DemandHistoryIngestOut",
    "DemandHistoryOut",
    "DemandHistoryPage",
    "DemandPlanLineOut",
    "DemandPlanLinePage",
    "DemandPlanOut",
    "DemandPlanPage",
    "DistributionCategoryMembersOut",
    "DistributionCategoryOut",
    "DistributionCategoryPage",
    "DrpNetworkMemberOut",
    "DrpNetworkMemberPage",
    "DrpNetworkOut",
    "DrpNetworkPage",
    "DrpNetworkPatch",
    "OrderItemOut",
    "OrderItemPage",
    "PlanningActionIn",
    "PlanningActionMessageOut",
    "PlanningActionMessagePage",
    "PlanningPreferencesOut",
    "PlanningRuleOut",
    "PlanningRulePage",
    "PlanningRulePatch",
    "PlanningRuleScopeOut",
    "PlanningRuleScopePage",
    "RejectedDemandRowOut",
    "ReorderReminderPage",
    "ReplenishmentCalculationOut",
    "ReplenishmentCalculationPage",
    "RuleScopeIn",
    "RuleScopesIn",
    "SupplyPlanLineOut",
    "SupplyPlanLinePage",
    "SupplyPlanOut",
    "SupplyPlanPage",
    "SupplyPlanSuggestionOut",
    "SupplyPlanSuggestionPage",
    "TimeFenceOut",
    "TimeFencePage",
    "TimeFencePatch",
    "WorkbenchItemOut",
    "WorkbenchOut",
]
