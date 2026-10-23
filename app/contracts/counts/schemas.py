"""Canonical Pydantic/JSON-Schema payloads for ``counts.engine@1.0.0`` (FS-07 §6/§7)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CountPlanFilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter_type: Literal["LOCATION", "ZONE", "BIN", "ITEM", "ITEM_CATEGORY", "ABC_CLASS", "STATUS"]
    filter_operator: Literal["INCLUDE", "EXCLUDE"] = "INCLUDE"
    value_text: str | None = None
    value_uuid: str | None = None
    sort_order: int = Field(default=0, ge=0)


class CountPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "ADHOC"]
    next_run_date: date
    blind_count: bool = False
    approval_required: bool = True
    include_zero_qty: bool = False
    notes: str | None = None
    filters: list[CountPlanFilterIn] = Field(default_factory=list)


class CountPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "ADHOC"] | None = None
    next_run_date: date | None = None
    blind_count: bool | None = None
    approval_required: bool | None = None
    include_zero_qty: bool | None = None
    notes: str | None = None
    filters: list[CountPlanFilterIn] | None = None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class GenerateCountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count_date: date | None = None
    assignee_id: str | None = None
    item_ids: list[str] = Field(default_factory=list)
    max_lines: int | None = Field(default=None, ge=1, le=5000)


class CountScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    bins: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    abc: list[str] = Field(default_factory=list)


class CountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count_type: Literal["MANUAL", "CALCULATED"] = "MANUAL"
    count_date: date | None = None
    scope: CountScopeIn = Field(default_factory=CountScopeIn)
    blind_count: bool = False
    approval_required: bool = True
    variance_account_ref: str | None = None


class CountLinePositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    location_id: str
    bin_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    inventory_status_id: str
    ownership: Literal["OWNED", "CONSIGNED"] = "OWNED"


class CountedLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: str | None = None
    position: CountLinePositionIn | None = None
    counted_qty: str
    notes: str | None = None
    counted_by: str | None = None


class CaptureLinesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_batch_id: str | None = None
    lines: list[CountedLineIn] = Field(min_length=1, max_length=1000)


class RecountIn(CaptureLinesIn):
    round: int | None = Field(default=None, ge=1)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = None
    approved_by: str | None = None


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    recalc_snapshot: bool = True


class SnapshotRecalcIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------- event payloads --------
class _EventBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class CountGeneratedEvent(_EventBase):
    count_id: str
    count_number: str
    count_type: Literal["CALCULATED", "MANUAL", "CYCLE"]
    count_plan_id: str | None = None
    count_date: date
    line_count: int


class CountApprovedEvent(_EventBase):
    count_id: str
    approved_by: str | None = None
    approved_at: str | None = None
    line_count: int
    variance_qty_total: str


class CountRejectedEvent(_EventBase):
    count_id: str
    reason: str
    recalc_snapshot: bool


class CountVariancePostedEvent(_EventBase):
    count_id: str
    inventory_transaction_id: str | None = None
    ledger_sequence: int | None = None
    line_count: int
    variance_value_base: str


class CountRecountedEvent(_EventBase):
    count_id: str
    line_id: str
    attempt: int
    counted_qty: str


class CountSnapshotRecalculatedEvent(_EventBase):
    count_id: str
    snapshot_revision: int
    recalculated_from_id: str | None = None


EVENT_MODELS: dict[str, type[_EventBase]] = {
    "count.generated": CountGeneratedEvent,
    "count.approved": CountApprovedEvent,
    "count.rejected": CountRejectedEvent,
    "count.variance_posted": CountVariancePostedEvent,
    "count.recounted": CountRecountedEvent,
    "count.snapshot.recalculated": CountSnapshotRecalculatedEvent,
}


def event_payload_schema(event_type: str) -> dict[str, Any]:
    model = EVENT_MODELS.get(event_type)
    if model is None:
        raise KeyError(f"unknown counts event {event_type!r}")
    return model.model_json_schema()


def all_event_payload_schemas() -> dict[str, dict[str, Any]]:
    return {event: event_payload_schema(event) for event in EVENT_MODELS}


def count_ref_schema() -> dict[str, Any]:
    class _CountRef(BaseModel):
        count_id: str
        count_number: str
        status: str
        count_type: str
        count_date: date
        count_round: int

    return _CountRef.model_json_schema()


__all__ = [
    "ApproveIn",
    "CaptureLinesIn",
    "CountCreate",
    "CountGeneratedEvent",
    "CountLinePositionIn",
    "CountPlanCreate",
    "CountPlanFilterIn",
    "CountPlanUpdate",
    "CountRecountedEvent",
    "CountRejectIn",
    "CountedLineIn",
    "EVENT_MODELS",
    "GenerateCountIn",
    "ReasonIn",
    "RecountIn",
    "RejectIn",
    "SnapshotRecalcIn",
    "all_event_payload_schemas",
    "count_ref_schema",
    "event_payload_schema",
]
