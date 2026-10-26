"""Pricing boundary DTOs (native FastAPI response/request models)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from app.api.schemas.common import APIModel, Page


class PricingDTO(APIModel):
    """Domain base: declared columns are typed; forward-compatible service additions pass through."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PriceLevelOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    currency_code: str | None = None
    is_default: bool | None = None
    is_inactive: bool | None = None
    sort_order: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemGroupOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemGroupMemberOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_group_id: UUID | None = None
    item_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CustomerGroupOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_inactive: bool | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CustomerGroupMemberOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    customer_group_id: UUID | None = None
    external_customer_ref: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PricingGroupOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_group_id: UUID | None = None
    customer_group_id: UUID | None = None
    price_level_id: UUID | None = None
    is_default: bool | None = None
    discount_percent: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemPriceOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    price_level_id: UUID | None = None
    currency_code: str | None = None
    qty_min: Decimal | None = None
    qty_max: Decimal | None = None
    unit_price: Decimal | None = None
    cost_plus_basis: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceScheduleOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    calculation_mode: str | None = None
    is_marginal: bool | None = None
    currency_code: str | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceScheduleBreakOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    price_schedule_id: UUID | None = None
    qty_from: Decimal | None = None
    qty_to: Decimal | None = None
    pricing_method: str | None = None
    value: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceRuleOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    rule_type: str | None = None
    priority: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None
    conflict_policy: str | None = None
    currency_code: str | None = None
    generated_from_update_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceRuleCriteriaOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    price_rule_id: UUID | None = None
    criterion: str | None = None
    operator: str | None = None
    value_text: str | None = None
    value_number: Decimal | None = None
    value_date: date | None = None
    group_no: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceRuleActionOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    price_rule_id: UUID | None = None
    action_type: str | None = None
    price_level_id: UUID | None = None
    value: Decimal | None = None
    cost_basis: str | None = None
    apply_to: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemCollectionOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    criteria: Any | None = None
    is_dynamic: bool | None = None
    is_inactive: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemCollectionMemberOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    item_collection_id: UUID | None = None
    item_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class CouponOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    coupon_type: str | None = None
    value: Decimal | None = None
    currency_code: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    max_uses: int | None = None
    uses_count: int | None = None
    item_id: UUID | None = None
    item_category_id: UUID | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceUpdateOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    update_type: str | None = None
    target_ref: str | None = None
    price_level_id: UUID | None = None
    currency_code: str | None = None
    effective_start: date | None = None
    effective_end: date | None = None
    status: str | None = None
    priority: int | None = None
    applied_at: datetime | None = None
    scheduled_by: UUID | None = None
    overlap_policy: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PriceUpdateDetailOut(PricingDTO):
    id: UUID | None = None
    tenant_id: UUID | None = None
    price_update_id: UUID | None = None
    item_id: UUID | None = None
    item_category_id: UUID | None = None
    external_customer_ref: str | None = None
    customer_group_id: UUID | None = None
    new_price: Decimal | None = None
    qty_min: Decimal | None = None
    qty_max: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PricingActionOut(PricingDTO):
    id: str | None = None
    status: str | None = None
    document_number: str | None = None
    posted_transaction_id: str | None = None
    reversal_transaction_id: str | None = None
    approval_request_id: str | None = None
    approval_mode: str | None = None
    message: str | None = None


class PriceLevelEnvelope(PricingDTO):
    price_level: PriceLevelOut | None = None


class ItemGroupEnvelope(PricingDTO):
    item_group: ItemGroupOut | None = None


class CustomerGroupEnvelope(PricingDTO):
    customer_group: CustomerGroupOut | None = None


class PricingGroupEnvelope(PricingDTO):
    pricing_group: PricingGroupOut | None = None


class ItemPriceEnvelope(PricingDTO):
    item_price: ItemPriceOut | None = None


class PriceScheduleEnvelope(PricingDTO):
    price_schedule: PriceScheduleOut | None = None


class PriceRuleEnvelope(PricingDTO):
    price_rule: PriceRuleOut | None = None


class ItemCollectionEnvelope(PricingDTO):
    item_collection: ItemCollectionOut | None = None


class CouponEnvelope(PricingDTO):
    coupon: CouponOut | None = None


class PriceUpdateEnvelope(PricingDTO):
    price_update: PriceUpdateOut | None = None


class PriceScheduleBreaksOut(PricingDTO):
    schedule: PriceScheduleOut | None = None
    breaks: list[PriceScheduleBreakOut] | None = None
    items: list[PriceScheduleBreakOut] | None = None


class PriceRuleDetailOut(PricingDTO):
    price_rule: PriceRuleOut | None = None
    criteria: list[PriceRuleCriteriaOut] | None = None
    actions: list[PriceRuleActionOut] | None = None
    items: list[Any] | None = None


class BulkResultOut(PricingDTO):
    updated: int | None = None
    applied: int | None = None
    rejected: list[Any] | None = None
    row_ids: list[Any] | None = None
    reverted: int | None = None
    recomputed: int | None = None
    errors: list[Any] | None = None
    items: list[Any] | None = None
    preview: list[Any] | None = None
    deduplicated: bool | None = None
    job_run_id: str | None = None


class PriceUpdateResultOut(PricingDTO):
    price_update: PriceUpdateOut | None = None
    details: list[PriceUpdateDetailOut] | None = None
    applied: bool | int | None = None
    rows_written: int | None = None
    reverted_rows: int | None = None
    rejected: list[Any] | None = None
    row_ids: list[Any] | None = None
    deduplicated: bool | None = None
    job_run_id: str | None = None


class CouponValidationOut(PricingDTO):
    valid: bool | None = None
    coupon_id: str | None = None
    code: str | None = None
    discount_type: str | None = None
    discount_value: Any | None = None
    currency_code: str | None = None
    reason: str | None = None
    message: str | None = None


class ResolvedPriceOut(PricingDTO):
    item_id: str | None = None
    price_level_id: str | None = None
    currency_code: str | None = None
    unit_price: Any | None = None
    qty: Any | None = None
    effective_date: str | None = None
    source: str | None = None


class PriceResolutionOut(PricingDTO):
    items: list[ResolvedPriceOut] | None = None
    prices: list[ResolvedPriceOut] | None = None
    resolved: list[Any] | None = None
    count: int | None = None


class EffectivePriceOut(PricingDTO):
    item_id: str | None = None
    price_level_id: str | None = None
    currency_code: str | None = None
    unit_price: Any | None = None
    effective_date: str | None = None
    source: str | None = None


class PriceListOut(PricingDTO):
    items: list[Any] | None = None
    columns: list[Any] | None = None
    count: int | None = None
    generated_at: str | None = None


class PriceLevelPage(Page[PriceLevelOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemGroupPage(Page[ItemGroupOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemGroupMemberPage(Page[ItemGroupMemberOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CustomerGroupPage(Page[CustomerGroupOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CustomerGroupMemberPage(Page[CustomerGroupMemberOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PricingGroupPage(Page[PricingGroupOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemPricePage(Page[ItemPriceOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PriceSchedulePage(Page[PriceScheduleOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PriceRulePage(Page[PriceRuleOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemCollectionPage(Page[ItemCollectionOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class ItemCollectionMemberPage(Page[ItemCollectionMemberOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class CouponPage(Page[CouponOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PriceUpdatePage(Page[PriceUpdateOut]):
    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class PricingRequestDTO(PricingDTO):
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


class PricingAddCollectionMembersIn(PricingRequestDTO):
    item_ids: Any | None = None


class PricingAddCustomerGroupMembersIn(PricingRequestDTO):
    external_customer_refs: Any | None = None


class PricingAddItemGroupMembersIn(PricingRequestDTO):
    item_ids: Any | None = None


class PricingApplyPriceUpdateIn(PricingRequestDTO):
    as_of: Any | None = None


class PricingCreateCouponIn(PricingRequestDTO):
    pass


class PricingCreateCustomerGroupIn(PricingRequestDTO):
    pass


class PricingCreateItemCollectionIn(PricingRequestDTO):
    pass


class PricingCreateItemGroupIn(PricingRequestDTO):
    pass


class PricingCreateItemPriceIn(PricingRequestDTO):
    pass


class PricingCreatePriceLevelIn(PricingRequestDTO):
    pass


class PricingCreatePriceRuleIn(PricingRequestDTO):
    pass


class PricingCreatePriceScheduleIn(PricingRequestDTO):
    pass


class PricingCreatePriceUpdateIn(PricingRequestDTO):
    pass


class PricingCreatePricingGroupIn(PricingRequestDTO):
    pass


class PricingGeneratePriceListIn(PricingRequestDTO):
    pass


class PricingPatchCouponIn(PricingRequestDTO):
    pass


class PricingPatchCustomerGroupIn(PricingRequestDTO):
    pass


class PricingPatchItemCollectionIn(PricingRequestDTO):
    is_active: Any | None = None


class PricingPatchItemGroupIn(PricingRequestDTO):
    pass


class PricingPatchItemPriceIn(PricingRequestDTO):
    pass


class PricingPatchPriceLevelIn(PricingRequestDTO):
    pass


class PricingPatchPriceRuleIn(PricingRequestDTO):
    pass


class PricingPatchPriceScheduleIn(PricingRequestDTO):
    pass


class PricingPatchPriceUpdateIn(PricingRequestDTO):
    pass


class PricingPatchPricingGroupIn(PricingRequestDTO):
    discount_percent: Any | None = None


class PricingPurchasePriceUpdatesIn(PricingRequestDTO):
    pass


class PricingRecomputeCollectionIn(PricingRequestDTO):
    pass


class PricingReplaceRuleActionIn(PricingRequestDTO):
    action: Any | None = None


class PricingReplaceRuleCriteriaIn(PricingRequestDTO):
    criteria: Any | None = None


class PricingReplaceScheduleBreaksIn(PricingRequestDTO):
    breaks: Any | None = None


class PricingSwapPriceLevelsIn(PricingRequestDTO):
    level_a_id: Any | None = None
