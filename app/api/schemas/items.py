"""Item-master boundary DTOs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page


class ItemInventoryOut(APIModel):
    tenant_id: UUID | None = None
    item_id: UUID | None = None
    costing_method: str
    costing_method_locked: bool | None = None
    initial_cost: Decimal | None = None
    asset_account_ref: str | None = None
    cogs_account_ref: str | None = None
    adjustment_account_ref: str | None = None
    variance_account_ref: str | None = None
    unbuild_variance_account_ref: str | None = None
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    auto_calculate_planning: bool | None = None
    use_bins: bool | None = None
    bin_required_on_txn: bool | None = None
    use_statuses: bool | None = None
    default_status_id: UUID | None = None
    allow_negative_inventory: bool | None = None
    allow_vendor_consignment: bool | None = None
    is_wip: bool | None = None
    backflush_default: bool | None = None
    yield_percent: Decimal | None = None
    round_up_component_qty: bool | None = None
    stock_uom_id: UUID | None = None
    must_be_whole_base_unit: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class ItemUOMOut(APIModel):
    uom_id: UUID
    role: str
    conversion_override: Decimal | None = None
    is_default_for_role: bool | None = None
    uom_code: str | None = None
    conversion_factor: Decimal | None = None
    is_base: bool | None = None
    uom_type_id: UUID | None = None


class ItemSummaryOut(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    item_number: str
    display_name: str
    description: str | None = None
    item_type_code: str | None = None
    item_category_id: UUID | None = None
    base_uom_id: UUID | None = None
    units_type_id: UUID | None = None
    is_active: bool = True
    created_from: str | None = None
    version: int | None = None


class ItemOut(ItemSummaryOut):
    item_type_id: UUID | None = None
    inactive_at: datetime | None = None
    matrix_parent_id: UUID | None = None
    upc_code: str | None = None
    external_ref: str | None = None
    category_code: str | None = None
    category_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    inventory: ItemInventoryOut | None = None
    purchasing: dict | None = None
    sales: dict | None = None
    accounting: dict | None = None
    uoms: list[ItemUOMOut] = Field(default_factory=list)
    custom_fields: list[dict] = Field(default_factory=list)


class ItemCreate(APIModel):
    item_number: str
    display_name: str
    item_type_code: str
    description: str | None = None
    base_uom_id: UUID | None = None
    item_category_id: UUID | None = None
    inventory: ItemInventoryOut | dict | None = None
    purchasing: dict | None = None
    sales: dict | None = None
    accounting: dict | None = None
    custom_fields: dict | None = None


class ItemUpdate(APIModel):
    display_name: str | None = None
    description: str | None = None
    base_uom_id: UUID | None = None
    item_category_id: UUID | None = None
    inventory: ItemInventoryOut | dict | None = None
    purchasing: dict | None = None
    sales: dict | None = None
    accounting: dict | None = None
    custom_fields: dict | None = None


class ItemPage(Page[ItemSummaryOut]):
    pass
