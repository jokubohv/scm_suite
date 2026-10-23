"""FS-03 `traceability.registry@1.0.0` contract models (Pydantic v2) + JSON Schema helpers.

Authority: `feature-sets/FS-03.md` §3/§6. HTTP envelopes wrap rows in named keys (``location``, ``zone``,
``bin``, ``inventory_status``); provider/consumer tests validate both these models and the generated
OpenAPI fragment. Event payload schemas cover the five required v1 events plus additive bootstrap/zone
observability events.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

LOCATION_TYPES = ("WAREHOUSE", "STORE", "RECEIVING", "TRANSIT", "CONSUMPTION", "UNALLOCATED")
ZONE_TYPES = ("STORAGE", "RECEIVING", "STAGING", "PICKING", "SHIPPING")
BIN_TYPES = ("STORAGE", "PICKING", "STAGING", "RECEIVING", "DOCK")


class Location(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    code: str
    name: str
    parent_id: str | None = None
    location_type: Literal["WAREHOUSE", "STORE", "RECEIVING", "TRANSIT", "CONSUMPTION", "UNALLOCATED"]
    is_bins_enabled: bool
    is_statuses_enabled: bool
    is_consignment_allowed: bool
    allow_commit_from_non_available: bool
    is_unallocated: bool
    is_inactive: bool
    inactive_at: str | None = None
    inactive_reason: str | None = None
    timezone: str
    version: int


class Zone(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    location_id: str
    code: str
    name: str
    zone_type: Literal["STORAGE", "RECEIVING", "STAGING", "PICKING", "SHIPPING"]
    priority: int
    is_active: bool
    version: int


class Bin(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    location_id: str
    zone_id: str | None = None
    code: str
    name: str
    bin_type: Literal["STORAGE", "PICKING", "STAGING", "RECEIVING", "DOCK"]
    is_pickable: bool
    is_active: bool
    capacity_qty: str | None = None
    capacity_uom_id: str | None = None
    coordinates: dict[str, Any] | None = None
    version: int


class InventoryStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    code: str
    name: str
    is_available_for_allocation: bool
    is_committable: bool
    is_sellable: bool
    is_consigned: bool
    is_default: bool
    is_system: bool
    is_active: bool
    version: int


class StatusFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_id: str
    code: str
    available_for_allocation: bool
    committable: bool
    sellable: bool
    consigned: bool
    default: bool
    active: bool


class PostingEligibility(BaseModel):
    model_config = ConfigDict(extra="allow")
    item_id: str
    bin_id: str | None = None
    location_id: str
    eligible: bool
    normalized: dict[str, Any]
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bins_enabled: bool
    statuses_enabled: bool


class BootstrapState(BaseModel):
    model_config = ConfigDict(extra="allow")
    tenant_id: str
    sentinel_present: bool
    sentinel_location_id: str | None = None
    status_count: int
    default_status_code: str | None = None
    seeded: bool
    distribution_completed: bool | None = None
    unallocated_positions: int | None = None
    deferred_fk_closed: bool | None = None


# ---------------------------------------------------------------- resolver contract ----------
class ResolveLocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    location_id: UUID
    allow_unallocated: bool = False


class ResolveDefaultStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    item_id: UUID | None = None
    location_id: UUID | None = None
    ownership: Literal["OWNED", "CONSIGNED"] = "OWNED"
    supplied_status_id: UUID | None = None


class ValidatePostingPositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    item_id: UUID
    location_id: UUID
    bin_id: UUID | None = None
    inventory_status_id: UUID | None = None
    ownership: Literal["OWNED", "CONSIGNED"] = "OWNED"
    qty_class: Literal["ON_HAND", "IN_TRANSIT"] = "ON_HAND"
    allow_unallocated: bool = False


class ValidateDistributionTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    lines: list[dict[str, Any]] = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- events --------------------
class LocationCreatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: str
    code: str
    location_type: str


class LocationUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: str
    changed: list[str] = Field(default_factory=list)


class LocationInactivatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: str
    warnings: list[str] = Field(default_factory=list)


class BinChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bin_id: str
    change: Literal["CREATED", "UPDATED", "ACTIVATED", "INACTIVATED"]
    location_id: str | None = None
    changed: list[str] = Field(default_factory=list)


class StatusChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_id: str
    code: str | None = None
    change: Literal["CREATED", "UPDATED", "ACTIVATED", "INACTIVATED", "DEFAULT_SET"]


class BootstrapCompletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seeded: bool
    status_count: int


class DeferredFkCheckedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orphans: dict[str, int]
    ok: bool


class ZoneChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_id: str
    change: Literal["CREATED", "UPDATED", "INACTIVATED"]
    location_id: str | None = None


REQUIRED_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "traceability.location.created": LocationCreatedEvent,
    "traceability.location.updated": LocationUpdatedEvent,
    "traceability.location.inactivated": LocationInactivatedEvent,
    "traceability.bin.changed": BinChangedEvent,
    "traceability.status.changed": StatusChangedEvent,
}
ADDITIVE_EVENT_MODELS: dict[str, type[BaseModel]] = {
    "traceability.bootstrap.completed": BootstrapCompletedEvent,
    "traceability.deferred_fk.checked": DeferredFkCheckedEvent,
    "traceability.zone.changed": ZoneChangedEvent,
}
EVENT_MODELS: dict[str, type[BaseModel]] = {**REQUIRED_EVENT_MODELS, **ADDITIVE_EVENT_MODELS}

RESOLVER_METHODS = (
    "resolve",
    "resolve_default_status",
    "resolve_position_defaults",
    "expand_location_scope",
    "validate_posting_position",
    "validate_distribution_target",
    "get_status_flags",
    "bootstrap_state",
)


def event_schemas() -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in EVENT_MODELS.items()}


def location_schema() -> dict[str, Any]:
    return Location.model_json_schema()


def bootstrap_schema() -> dict[str, Any]:
    return BootstrapState.model_json_schema()


__all__ = [
    "ADDITIVE_EVENT_MODELS",
    "BIN_TYPES",
    "BootstrapState",
    "EVENT_MODELS",
    "LOCATION_TYPES",
    "Location",
    "PostingEligibility",
    "REQUIRED_EVENT_MODELS",
    "RESOLVER_METHODS",
    "StatusFlags",
    "ZONE_TYPES",
    "bootstrap_schema",
    "event_schemas",
    "location_schema",
]
