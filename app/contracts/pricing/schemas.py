"""FS-02 `pricing.engine@1.0.0` contract models (Pydantic v2 + JSON Schema helpers).

These are the authoritative request/response shapes for price resolution and the event payloads
published by the pricing engine (envelope is `platform.events@1.0.0`). The external order-management
system (D3/Δ-2) snapshots :class:`PricedQuote` on its own documents; Phase 1 adds no ledger table or
snapshot table for sell-side prices.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CostContextIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_cost: str | None = None
    cost_basis: Literal["AVERAGE", "LAST_PURCHASE", "STANDARD"] | None = None


class ResolutionLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_ref: str = Field(min_length=1, max_length=100)
    item_id: UUID
    quantity: str = Field(description="11+ digit decimal string; must be > 0")
    uom: str = Field(default="EA", min_length=1, max_length=20)
    parent_item_id: UUID | None = None


class PriceResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[ResolutionLineIn] = Field(min_length=1, max_length=500)
    external_customer_ref: str | None = Field(default=None, max_length=200)
    customer_group_id: UUID | None = None
    price_level_id: UUID | None = None
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    document_date: date | None = None
    coupon_code: str | None = Field(default=None, max_length=64)
    cost_context: CostContextIn | None = None
    include_trace: bool | None = None


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="allow")
    step: str
    source: str
    applied: bool
    reason: str = ""


class ResolutionLine(BaseModel):
    model_config = ConfigDict(extra="allow")
    line_ref: str
    item_id: str
    uom: str
    quantity: str
    unit_price: str
    extended_price: str
    currency_code: str
    price_source: Literal["LEVEL", "PRICING_GROUP", "RULE", "COUPON", "SCHEDULE", "NONE"]
    price_level_id: str | None = None
    price_rule_id: str | None = None
    coupon_code: str | None = None
    schedule_id: str | None = None
    quantity_break: str | None = None
    calculation_mode: str | None = None
    quantity_basis: str | None = None
    marginal_applied: bool = False
    fallback_used: bool = False
    discount_amount: str = "0"
    effective_at: str
    rules_applied: list[str] = Field(default_factory=list)
    policy_version: str
    quote_hash: str
    trace: list[dict[str, Any]] = Field(default_factory=list)


class PricedQuote(BaseModel):
    """Immutable authoritative quote returned to the external order boundary (D3/Δ-2)."""

    model_config = ConfigDict(extra="forbid")
    policy_version: str
    currency_code: str
    document_date: str
    external_customer_ref: str | None = None
    customer_group_id: str | None = None
    lines: list[ResolutionLine]
    quote_hash: str
    line_count: int


class PriceResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    lines: list[ResolutionLine]
    currency_code: str
    document_date: str
    policy_version: str
    priced_quote: PricedQuote


# --------------------------------------------------------------------------- events -----------
class PriceUpdatedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(default_factory=list)
    price_level_id: str | None = None
    currency_code: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    source: Literal["ROW", "BULK", "SWAP", "UPDATE", "RULE"] = "ROW"


class RuleChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_rule_id: str
    change: Literal["CREATED", "UPDATED", "DISABLED", "GENERATED"]
    priority: int


class PriceUpdateAppliedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_update_id: str
    update_type: str
    target_ref: str
    effective_start: str
    effective_end: str | None = None
    rows_written: int


class PriceUpdateSupersededEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_update_id: str
    superseded_by_id: str
    target_ref: str
    effective_start: str


class PriceUpdateRevertedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_update_id: str
    target_ref: str
    reverted_at: str


class CouponRedeemedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coupon_code: str
    document_type: str
    document_id: str
    uses_count: int
    max_uses: int | None = None
    discount_amount: str


EVENT_MODELS: dict[str, type[BaseModel]] = {
    "pricing.price.updated": PriceUpdatedEvent,
    "pricing.rule.changed": RuleChangedEvent,
    "pricing.price_update.applied": PriceUpdateAppliedEvent,
    "pricing.price_update.superseded": PriceUpdateSupersededEvent,
    "pricing.price_update.reverted": PriceUpdateRevertedEvent,
    "pricing.coupon.redeemed": CouponRedeemedEvent,
}


def priced_quote_schema() -> dict[str, Any]:
    return PricedQuote.model_json_schema()


def price_resolution_schema() -> dict[str, Any]:
    return PriceResolutionResponse.model_json_schema()


def event_schemas() -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in EVENT_MODELS.items()}


__all__ = [
    "CouponRedeemedEvent",
    "EVENT_MODELS",
    "PriceResolutionRequest",
    "PriceResolutionResponse",
    "PriceUpdateAppliedEvent",
    "PriceUpdateRevertedEvent",
    "PriceUpdateSupersededEvent",
    "PricedQuote",
    "PriceUpdatedEvent",
    "ResolutionLine",
    "ResolutionLineIn",
    "RuleChangedEvent",
    "event_schemas",
    "price_resolution_schema",
    "priced_quote_schema",
]
