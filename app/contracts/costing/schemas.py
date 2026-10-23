"""Canonical Pydantic/JSON-Schema payloads for ``costing.engine@1.0.0`` (FS-05 §3/§7).

Decimal values are documented as decimal strings on the wire (``NUMERIC(38,12)``); the in-process port
uses :class:`~decimal.Decimal`. ``ValueResultPayload`` mirrors ``ValueResult.as_dict()`` exactly.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CostObjectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    costing_method: Literal["AVERAGE", "FIFO", "LIFO", "GROUP_AVERAGE", "STANDARD", "SPECIFIC", "LOT"]
    location_id: str | None = None
    location_costing_group_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    item_cost_id: str | None = None


class LayerStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer_id: str
    layer_seq: int = Field(ge=0)
    layer_date: date
    qty_original: str
    qty_remaining: str
    unit_cost: str
    value_remaining: str
    status: Literal["OPEN", "EXHAUSTED", "REVALUED"] = "OPEN"
    estimated: bool = False
    derived_from_layer_id: str | None = None


class ItemCostStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty_on_hand: str = "0.000000000000"
    value_base: str = "0.000000000000"
    avg_cost: str = "0.000000000000"
    last_purchase_price: str | None = None
    status: Literal["CURRENT", "PENDING_RECALC", "UNDERWATER"] = "CURRENT"
    cost_provisional: bool = False
    last_cost_ledger_seq: int | None = None
    item_cost_id: str | None = None


class CostingSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_when_recalc_pending: bool = True
    allow_provisional: bool = False
    allow_negative: bool = True
    rounding_mode: Literal["PER_MOVEMENT", "DOCUMENT"] = "PER_MOVEMENT"
    return_policy: Literal["ORIGINAL_WHEN_TRACEABLE", "CURRENT"] = "ORIGINAL_WHEN_TRACEABLE"
    numbered_inventory_available: bool = False
    period_locked: bool = False


class ValueMovementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    qty_delta: str
    movement_type: Literal[
        "IN", "OUT", "ADJUST", "TRANSFER", "REVALUE", "LANDED", "VARIANCE", "COGS_ADJUST"
    ]
    transaction_date: date
    location_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    source_line_id: str | None = None
    unit_cost: str | None = None
    value_delta: str | None = None
    actual_unit_cost: str | None = None
    standard_unit_cost: str | None = None
    posting_rule_id: str = ""
    asset_account_ref: str = "ASSET"
    offset_account_ref: str = ""
    variance_account_ref: str = "VARIANCE"
    cost_category_id: str | None = None
    layer_date: date | None = None
    close_layer_id: str | None = None
    derived_from_layer_id: str | None = None
    estimated_allowed: bool | None = None
    return_original_cost: str | None = None
    affects_inventory: bool = True
    reason: str | None = None


class CostEntryPayload(BaseModel):
    movement_type: str
    qty_delta: str
    unit_cost: str
    value_delta: str
    layer_id: str | None = None
    estimated: bool = False
    rounding_residual: bool = False
    cost_category_id: str | None = None
    variance_type: str | None = None


class LayerActionPayload(BaseModel):
    action: Literal["CREATE", "CONSUME", "REVALUE", "CLOSE"]
    layer_id: str
    layer_seq: int | None = None
    layer_date: date | None = None
    qty: str = "0"
    value: str = "0"
    unit_cost: str = "0"
    derived_from_layer_id: str | None = None
    estimated: bool = False


class GLIntentPayload(BaseModel):
    posting_rule_id: str
    account_ref: str
    debit: str = "0"
    credit: str = "0"
    qty: str = "0"
    cost_category_id: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)


class EventDraftPayload(BaseModel):
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)


class ValueResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_cost: str
    value_delta: str
    qty_delta: str
    estimated: bool = False
    provisional: bool = False
    variance_amount: str = "0.000000000000"
    cost_entries: list[CostEntryPayload] = Field(default_factory=list)
    layer_actions: list[LayerActionPayload] = Field(default_factory=list)
    gl_intents: list[GLIntentPayload] = Field(default_factory=list)
    events: list[EventDraftPayload] = Field(default_factory=list)
    new_state: ItemCostStatePayload | None = None
    new_layers: list[LayerStatePayload] = Field(default_factory=list)


# ---------------------------------------------------------------------------- API schemas ----
class CostCategoryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    category_type: Literal["MATERIAL", "LABOR", "MACHINE", "OVERHEAD", "FREIGHT", "DUTY", "OTHER"]
    is_rollup_input: bool = False
    sort_order: int = Field(default=0, ge=0)


class CostCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_rollup_input: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class CostTemplateLineIn(BaseModel):
    cost_category_id: str
    percentage: str | None = None
    fixed_amount: str | None = None
    sort_order: int = Field(default=0, ge=0)


class CostTemplateCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    item_type_id: str | None = None
    item_category_id: str | None = None
    lines: list[CostTemplateLineIn] = Field(default_factory=list)


class CostVersionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    effective_from: date
    effective_to: date | None = None


class PlannedStandardIn(BaseModel):
    item_id: str
    cost_category_id: str
    unit_cost: str


class PlannedStandardUpsert(BaseModel):
    lines: list[PlannedStandardIn] = Field(default_factory=list)


class StandardCostIn(BaseModel):
    item_id: str
    cost_category_id: str
    cost_version_id: str
    unit_cost: str
    effective_from: date
    effective_to: date | None = None
    source: Literal["MANUAL", "ROLLUP", "IMPORT"] = "MANUAL"


class RollupRequest(BaseModel):
    cost_version_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    material_cost_type: Literal["PER_COSTING_METHOD", "PER_PURCHASE_PRICE", "PER_ITEM_DEFAULT"] = "PER_ITEM_DEFAULT"
    include_conversion_costs: bool = True


class RevaluationLineIn(BaseModel):
    item_id: str
    location_id: str | None = None
    work_order_id: str | None = None
    old_cost: str
    new_cost: str
    qty: str


class RevaluationCreate(BaseModel):
    revaluation_type: Literal["STANDARD_CHANGE", "COST_CORRECTION"]
    cost_version_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    lines: list[RevaluationLineIn] = Field(default_factory=list)


class LandedCostLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    amount: str
    cost_category_id: str | None = None
    currency_code: str | None = None
    allocation_basis_override: Literal["WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE"] | None = None


class LandedCostCreate(BaseModel):
    transaction_date: date
    allocation_basis: Literal["WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE"]
    currency_code: str = Field(min_length=3, max_length=3)
    vendor_id: str | None = None
    vendor_bill_id: str | None = None
    receipt_refs: list[dict[str, Any]] = Field(default_factory=list)
    lines: list[LandedCostLineIn] = Field(default_factory=list)


class EstimatedLandedCostTemplateLineIn(BaseModel):
    cost_category_id: str
    allocation_basis: Literal["WEIGHT", "QUANTITY", "AMOUNT", "PER_LINE"]
    default_amount: str | None = None
    default_percent: str | None = None
    sort_order: int = Field(default=0, ge=0)


class EstimatedLandedCostTemplateCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    currency_code: str = Field(min_length=3, max_length=3)
    is_default: bool = False
    lines: list[EstimatedLandedCostTemplateLineIn] = Field(default_factory=list)


class CogsAdjustmentLineIn(BaseModel):
    item_id: str
    item_cost_id: str
    original_transaction_line_id: str | None = None
    value_delta: str
    estimated: bool = False


class CogsAdjustmentCreate(BaseModel):
    adjustment_type: Literal["UNDERWATER_TRUE_UP", "RECALC", "ROUNDING", "RETURN_COST"]
    reason: str = Field(min_length=1, max_length=500)
    source_cost_run_id: str | None = None
    lines: list[CogsAdjustmentLineIn] = Field(default_factory=list)


class GroupMemberIn(BaseModel):
    location_id: str


class CostingGroupCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    currency_code: str = Field(min_length=3, max_length=3)
    include_in_transit_value: bool = False
    members: list[GroupMemberIn] = Field(default_factory=list)


class CostingGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    include_in_transit_value: bool | None = None
    is_active: bool | None = None


# --------------------------------------------------------------------- JSON-Schema output ----
def value_movement_schema() -> dict[str, Any]:
    return ValueMovementPayload.model_json_schema()


def value_result_schema() -> dict[str, Any]:
    return ValueResultPayload.model_json_schema()


def cost_object_schema() -> dict[str, Any]:
    return CostObjectPayload.model_json_schema()


def item_cost_state_schema() -> dict[str, Any]:
    return ItemCostStatePayload.model_json_schema()


def layer_state_schema() -> dict[str, Any]:
    return LayerStatePayload.model_json_schema()


# Event payload models (contracts/events/costing.engine/<event>.schema.json `data`).
class _EventBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class CogsRecordedEvent(_EventBase):
    item_id: str
    value_delta: str
    cogs_adjustment_id: str | None = None
    fiscal_period_id: str | None = None


class CostAdjustmentPostedEvent(_EventBase):
    cogs_adjustment_id: str
    adjustment_type: Literal["UNDERWATER_TRUE_UP", "RECALC", "ROUNDING", "RETURN_COST"]
    total_value_base: str
    posted_transaction_id: str | None = None


class CostEngineStatusEvent(_EventBase):
    item_id: str
    costing_method: str
    qty_on_hand: str
    value_base: str
    avg_cost: str
    status: Literal["CURRENT", "PENDING_RECALC", "UNDERWATER"]
    estimated: bool = False


class CostLayerCreatedEvent(_EventBase):
    item_id: str
    layer_id: str
    qty: str
    unit_cost: str
    value: str
    estimated: bool = False


class CostLayerUpdatedEvent(_EventBase):
    item_id: str
    movement_type: str
    qty_delta: str
    unit_cost: str
    value_delta: str
    layer_id: str | None = None
    estimated: bool = False


class CostRecalculationCompletedEvent(_EventBase):
    item_id: str
    cost_run_id: str
    adjustments: int
    value_delta: str


class CostRecalculationQueuedEvent(_EventBase):
    item_id: str | None = None
    from_date: date
    reason: str
    cost_run_id: str | None = None


class CostReconciliationCompletedEvent(_EventBase):
    checked_positions: int
    drift_count: int
    as_of: datetime | None = None


class CostRevaluationPostedEvent(_EventBase):
    cost_revaluation_id: str
    revaluation_type: Literal["STANDARD_CHANGE", "COST_CORRECTION"]
    posted_transaction_id: str
    total_value_delta: str


class CostRollupCompletedEvent(_EventBase):
    cost_rollup_run_id: str
    cost_version_id: str
    lines: int
    status: Literal["SUCCEEDED", "FAILED"]


class CostUnderwaterTruedUpEvent(_EventBase):
    item_id: str
    qty: str
    true_unit_cost: str
    estimated_unit_cost: str
    value_delta: str


class CostVariancePostedEvent(_EventBase):
    item_id: str
    variance_type: str
    amount_base: str
    variance_id: str | None = None


class LandedCostAppliedEvent(_EventBase):
    landed_cost_id: str
    item_receipt_line_id: str
    allocated_amount: str
    cost_category_id: str | None = None


class LandedCostEstimatedEvent(_EventBase):
    item_receipt_line_id: str
    cost_category_id: str | None = None
    allocated_amount: str
    template_id: str | None = None


class StandardCostRevaluedEvent(_EventBase):
    item_id: str
    cost_version_id: str
    old_cost: str
    new_cost: str
    value_delta: str


EVENT_MODELS: dict[str, type[_EventBase]] = {
    "cogs.recorded": CogsRecordedEvent,
    "cost.adjustment.posted": CostAdjustmentPostedEvent,
    "cost.engine.status": CostEngineStatusEvent,
    "cost.layer.created": CostLayerCreatedEvent,
    "cost.layer.updated": CostLayerUpdatedEvent,
    "cost.recalculation.completed": CostRecalculationCompletedEvent,
    "cost.recalculation.queued": CostRecalculationQueuedEvent,
    "cost.reconciliation.completed": CostReconciliationCompletedEvent,
    "cost.revaluation.posted": CostRevaluationPostedEvent,
    "cost.rollup.completed": CostRollupCompletedEvent,
    "cost.underwater.trued_up": CostUnderwaterTruedUpEvent,
    "cost.variance.posted": CostVariancePostedEvent,
    "landed_cost.applied": LandedCostAppliedEvent,
    "landed_cost.estimated": LandedCostEstimatedEvent,
    "standard_cost.revalued": StandardCostRevaluedEvent,
}


def event_payload_schema(event_type: str) -> dict[str, Any]:
    model = EVENT_MODELS.get(event_type)
    if model is None:
        raise KeyError(f"unknown costing event {event_type!r}")
    return model.model_json_schema()


def all_event_payload_schemas() -> dict[str, dict[str, Any]]:
    return {event: event_payload_schema(event) for event in EVENT_MODELS}


def decimal_str(value: Decimal | int | str) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.000000000001")), "f")


__all__ = [
    "EVENT_MODELS",
    "CostCategoryCreate",
    "CostCategoryUpdate",
    "CostEntryPayload",
    "CostObjectPayload",
    "CostTemplateCreate",
    "CostTemplateLineIn",
    "CostVersionCreate",
    "CostingGroupCreate",
    "CostingGroupUpdate",
    "CostingSettingsPayload",
    "CogsAdjustmentCreate",
    "CogsAdjustmentLineIn",
    "EstimatedLandedCostTemplateCreate",
    "EstimatedLandedCostTemplateLineIn",
    "EventDraftPayload",
    "GLIntentPayload",
    "ItemCostStatePayload",
    "LandedCostCreate",
    "LandedCostLineIn",
    "LayerActionPayload",
    "LayerStatePayload",
    "PlannedStandardIn",
    "PlannedStandardUpsert",
    "RevaluationCreate",
    "RevaluationLineIn",
    "RollupRequest",
    "StandardCostIn",
    "ValueMovementPayload",
    "ValueResultPayload",
    "all_event_payload_schemas",
    "cost_object_schema",
    "decimal_str",
    "event_payload_schema",
    "item_cost_state_schema",
    "layer_state_schema",
    "value_movement_schema",
    "value_result_schema",
]
