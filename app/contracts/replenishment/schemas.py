"""Canonical Pydantic/JSON-Schema payloads for ``replenishment.planning@1.0.0`` (FS-10)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanningRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    engine: Literal["MRP", "DRP_BOD"] = "MRP"
    allowed_supply_types: list[Literal["PURCHASE", "WORK_ORDER", "TRANSFER"]] = Field(default=["PURCHASE"])
    lot_sizing: Literal["LOT_FOR_LOT", "FIXED_QTY", "MIN_MAX"] = "LOT_FOR_LOT"
    fixed_order_qty: str | None = None
    min_order_qty: str | None = None
    max_order_qty: str | None = None
    time_fence_days: int = Field(default=0, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)
    scopes: list[dict[str, Any]] = Field(default_factory=list)


class TimeFenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_type: Literal["ITEM", "ITEM_CATEGORY", "LOCATION", "GLOBAL"]
    scope_id: str | None = None
    fence_days: int = Field(default=0, ge=0)
    behavior: Literal["FROZEN", "SLUSHED"] = "SLUSHED"
    demand_change_threshold: str | None = None


class DemandPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["LINEAR_REGRESSION", "MOVING_AVERAGE", "SEASONAL_AVERAGE", "SALES_FORECAST", "OPEN_DEMAND", "MANUAL"] = "MOVING_AVERAGE"
    horizon_start: date
    horizon_end: date
    bucket_granularity: Literal["DAY", "WEEK", "MONTH"] = "WEEK"
    planning_rule_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    demand_change_threshold: str | None = None


class SupplyPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planning_rule_id: str | None = None
    horizon_start: date
    horizon_end: date
    scope: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None
    bucket_granularity: Literal["DAY", "WEEK", "MONTH"] = "WEEK"
    run_key: str | None = None


class ConvertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestion_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    vendor_id: str | None = None


class AtpRebuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None
    dry_run: bool = False
    method: Literal["DISCRETE_ATP", "CUMULATIVE_ATP_WITH_LOOK_AHEAD"] | None = None


class StockItemSupplyPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    item_id: str
    location_id: str
    qty: str


class PlanningRulePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    code: str
    engine: str
    allowed_supply_types: list[str]
    lot_sizing: str


class DemandPlanPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    plan_number: str
    method: str
    status: str
    horizon_start: date
    horizon_end: date


class SupplyPlanPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    plan_number: str
    status: str
    run_key: str
    horizon_start: date
    horizon_end: date


class SuggestionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    supply_plan_id: str
    item_id: str
    location_id: str
    action_type: str
    qty: str
    due_date: date
    status: str


class AllocationRunPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    run_number: str
    allocation_strategy_id: str
    as_of_date: date
    status: str


class AtpBucketPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    bucket_date: date
    begin_qty: str
    supply_qty: str
    demand_qty: str
    projected_available: str
    shortage_qty: str
    shortage_date: date | None = None
    earliest_full_date: date | None = None


# --------------------------------------------------------------------- event payloads --------
class _EventBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class PlanGeneratedEvent(_EventBase):
    plan_type: Literal["DEMAND", "SUPPLY"]
    plan_id: str
    run_key: str
    status: str
    horizon: list[str]
    line_count: int
    policy_versions: dict[str, str] = Field(default_factory=dict)


class PlanDemandCalculatedEvent(_EventBase):
    demand_plan_id: str
    method: str
    line_count: int
    policy_version: str
    changed_lines: int = 0


class PlanSupplyCalculatedEvent(_EventBase):
    supply_plan_id: str
    rule_id: str | None = None
    suggestion_count: int
    exception_count: int
    run_key: str
    status: str


class PlanApprovedEvent(_EventBase):
    plan_type: Literal["DEMAND", "SUPPLY"]
    plan_id: str
    approver: str | None = None
    approved_at: str | None = None


class PlanSuggestionCreatedEvent(_EventBase):
    supply_plan_id: str
    suggestions: list[dict[str, Any]] = Field(default_factory=list)


class PlanSuggestionConvertedEvent(_EventBase):
    suggestion_id: str
    created_doc_type: str | None = None
    created_doc_id: str | None = None
    idempotency_ref: str | None = None


class PlanningExceptionRaisedEvent(_EventBase):
    message_type: str
    severity: str
    item_id: str | None = None
    location_id: str | None = None
    supply_plan_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class PlanningExceptionResolvedEvent(_EventBase):
    message_id: str
    resolution: str
    actor: str | None = None


class AllocationCompletedEvent(_EventBase):
    allocation_run_id: str
    strategy_id: str
    as_of_date: date
    demand_count: int
    supply_count: int
    allocated_count: int
    exception_count: int


class AllocationExceptionRaisedEvent(_EventBase):
    allocation_run_id: str
    exceptions: list[dict[str, Any]] = Field(default_factory=list)


class ReplenishmentCalculatedEvent(_EventBase):
    replenishment_calculation_id: str
    formula_version: str
    line_count: int
    applied_count: int = 0
    skipped_manual_count: int = 0


class ReplenishmentBelowReorderPointEvent(_EventBase):
    item_id: str
    location_id: str
    available_qty: str
    reorder_point: str
    preferred_stock: str
    qty_to_order: str


class ReplenishmentOrderItemsCalculatedEvent(_EventBase):
    calculation_id: str
    item_count: int


class AtpUpdatedEvent(_EventBase):
    scope: dict[str, Any] = Field(default_factory=dict)
    as_of: str
    method: str
    policy_version: str
    bucket_count: int


class ReplenishmentPreferencesChangedEvent(_EventBase):
    changed_keys: list[str] = Field(default_factory=list)
    policy_versions: dict[str, str] = Field(default_factory=dict)
    actor: str | None = None


EVENT_MODELS: dict[str, type[_EventBase]] = {
    "plan.generated": PlanGeneratedEvent,
    "plan.demand.calculated": PlanDemandCalculatedEvent,
    "plan.supply.calculated": PlanSupplyCalculatedEvent,
    "plan.approved": PlanApprovedEvent,
    "plan.suggestion.created": PlanSuggestionCreatedEvent,
    "plan.suggestion.converted": PlanSuggestionConvertedEvent,
    "planning.exception.raised": PlanningExceptionRaisedEvent,
    "planning.exception.resolved": PlanningExceptionResolvedEvent,
    "allocation.completed": AllocationCompletedEvent,
    "allocation.exception.raised": AllocationExceptionRaisedEvent,
    "replenishment.calculated": ReplenishmentCalculatedEvent,
    "replenishment.below_reorder_point": ReplenishmentBelowReorderPointEvent,
    "replenishment.order_items.calculated": ReplenishmentOrderItemsCalculatedEvent,
    "atp.updated": AtpUpdatedEvent,
    "replenishment.preferences.changed": ReplenishmentPreferencesChangedEvent,
}


def event_payload_schema(event_type: str) -> dict[str, Any]:
    model = EVENT_MODELS.get(event_type)
    if model is None:
        raise KeyError(f"unknown planning event {event_type!r}")
    return model.model_json_schema()


def all_event_payload_schemas() -> dict[str, dict[str, Any]]:
    return {event: event_payload_schema(event) for event in EVENT_MODELS}


def planning_plan_schemas() -> dict[str, dict[str, Any]]:
    return {
        "PlanningRule": PlanningRulePayload.model_json_schema(),
        "DemandPlan": DemandPlanPayload.model_json_schema(),
        "SupplyPlan": SupplyPlanPayload.model_json_schema(),
        "Suggestion": SuggestionPayload.model_json_schema(),
        "AllocationRun": AllocationRunPayload.model_json_schema(),
        "AtpBucket": AtpBucketPayload.model_json_schema(),
    }


__all__ = [
    "AtpBucketPayload",
    "AtpRebuildIn",
    "ConvertIn",
    "DemandPlanIn",
    "DemandPlanPayload",
    "EVENT_MODELS",
    "PlanningRuleIn",
    "PlanningRulePayload",
    "SupplyPlanIn",
    "SupplyPlanPayload",
    "SuggestionPayload",
    "TimeFenceIn",
    "all_event_payload_schemas",
    "event_payload_schema",
    "planning_plan_schemas",
]
