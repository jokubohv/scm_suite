"""Item-master boundary DTOs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
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


class ItemUomCreate(APIModel):
    uom_id: UUID
    role: str
    is_default_for_role: bool | None = None
    conversion_override: Decimal | None = None


class ItemCreate(APIModel):
    item_number: str
    display_name: str | None = None
    item_type_code: str | None = None
    description: str | None = None
    units_type_id: UUID | None = None
    base_uom_id: UUID | None = None
    item_category_id: UUID | None = None
    upc_code: str | None = None
    external_ref: str | None = None
    created_from: str | None = None
    uoms: list[ItemUomCreate] = Field(default_factory=list)
    inventory: ItemInventoryOut | dict | None = None
    purchasing: dict | None = None
    sales: dict | None = None
    accounting: dict | None = None
    custom_fields: dict | None = None


class ItemUpdate(APIModel):
    display_name: str | None = None
    description: str | None = None
    item_category_id: UUID | None = None
    upc_code: str | None = None
    external_ref: str | None = None
    custom_fields: dict | None = None


class ItemPage(Page[ItemSummaryOut]):
    pass


# =============================================================================================
# Remaining FS-01 boundary DTOs (taxonomy, categories, UoM, extensions, locations, planning,
# codes/matrix/kit). Response models are the native FastAPI contract; unknown service fields are
# ignored at the boundary, action results allow the documented action extras.
# =============================================================================================
class AuditedOut(APIModel):
    tenant_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class PostingEligibilityOut(APIModel):
    eligible: bool
    code: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ItemTypeOut(APIModel):
    code: str
    name: str
    can_stock: bool
    requires_bom: bool
    requires_numbering: bool
    is_charge_only: bool
    sort_order: int | None = None
    posting_eligibility: dict[str, PostingEligibilityOut] = Field(default_factory=dict)


class ItemTypePage(Page[ItemTypeOut]):
    pass


class ItemCategoryOut(AuditedOut):
    id: UUID
    parent_id: UUID | None = None
    code: str
    name: str
    is_active: bool = True
    sort_order: int | None = None


class ItemCategoryPage(Page[ItemCategoryOut]):
    pass


class UomOut(AuditedOut):
    id: UUID
    uom_type_id: UUID | None = None
    code: str
    name: str | None = None
    conversion_factor: Decimal | None = None
    is_base: bool | None = None
    rounding_precision: int | None = None
    is_active: bool | None = None


class UomTypeOut(AuditedOut):
    id: UUID
    code: str
    name: str | None = None
    base_uom_id: UUID | None = None
    is_active: bool | None = None
    units: list[UomOut] = Field(default_factory=list)


class UomTypePage(Page[UomTypeOut]):
    pass


class UomPage(Page[UomOut]):
    pass


class UomConversionOut(AuditedOut):
    id: UUID
    uom_type_id: UUID | None = None
    from_uom_id: UUID | None = None
    to_uom_id: UUID | None = None
    factor: Decimal | None = None
    notes: str | None = None


class UomConversionPage(Page[UomConversionOut]):
    pass


class UomResolutionOut(APIModel):
    quantity: Decimal
    quantity_base: Decimal
    quantity_to: Decimal | None = None
    uom_code: str
    to_uom_code: str | None = None
    rounded_to: int | None = None
    factor: Decimal
    whole_base_required: bool | None = None


class ItemPurchasingOut(AuditedOut):
    item_id: UUID | None = None
    preferred_vendor_id: UUID | None = None
    purchase_uom_id: UUID | None = None
    purchase_price: Decimal | None = None
    purchase_price_currency: str | None = None
    lead_time_days: int | None = None
    is_drop_ship: bool | None = None
    is_special_order: bool | None = None
    country_of_origin: str | None = None
    harmonized_code: str | None = None
    purchase_tax_code: str | None = None


class ItemSalesOut(AuditedOut):
    item_id: UUID | None = None
    sale_uom_id: UUID | None = None
    price_level_id: UUID | None = None
    is_taxable: bool | None = None
    shipping_class: str | None = None
    weight: Decimal | None = None
    weight_uom: str | None = None
    is_sellable: bool | None = None
    is_shippable: bool | None = None
    quantity_pricing_schedule_id: UUID | None = None
    pricing_group_id: UUID | None = None
    matrix_parent_qty_behavior: str | None = None


class ItemAccountingOut(AuditedOut):
    item_id: UUID | None = None
    income_account_ref: str | None = None
    cogs_account_ref: str | None = None
    asset_account_ref: str | None = None
    expense_account_ref: str | None = None
    variance_account_ref: str | None = None
    deferred_revenue_account_ref: str | None = None
    tax_schedule_ref: str | None = None
    revenue_recognition_template: str | None = None


class ItemCustomFieldValueOut(APIModel):
    field_code: str
    data_type: str | None = None
    value: Any = None


class ItemCustomFieldsOut(APIModel):
    item_id: UUID
    items: list[ItemCustomFieldValueOut] = Field(default_factory=list)


class ItemLocationOut(AuditedOut):
    item_id: UUID | None = None
    location_id: UUID
    is_preferred: bool | None = None
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    auto_calculate_planning: bool | None = None
    default_bin_id: UUID | None = None
    default_status_id: UUID | None = None
    cost_group_override_id: UUID | None = None
    is_consigned_allowed: bool | None = None


class ItemLocationPage(Page[ItemLocationOut]):
    pass


class ItemUomAssignmentsOut(Page[ItemUOMOut]):
    pass


class PlanningSnapshotOut(APIModel):
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    auto_calculate_planning: bool | None = None


class AvailabilitySettingsOut(APIModel):
    allow_negative_inventory: bool | None = None
    allow_vendor_consignment: bool | None = None
    use_bins: bool | None = None
    bin_required_on_txn: bool | None = None
    use_statuses: bool | None = None
    default_status_id: UUID | None = None
    planning: PlanningSnapshotOut = Field(default_factory=PlanningSnapshotOut)


class PlanningValuesResultOut(APIModel):
    item_id: UUID
    location_id: UUID | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    source: str
    policy_version: str | None = None
    version: int


class ItemBarcodeOut(AuditedOut):
    id: UUID
    item_id: UUID | None = None
    barcode: str
    barcode_type: str | None = None
    uom_id: UUID | None = None
    is_primary: bool | None = None
    is_active: bool | None = None


class ItemBarcodePage(Page[ItemBarcodeOut]):
    pass


class BarcodeResolveOut(APIModel):
    item_id: UUID
    uom_id: UUID | None = None
    barcode_type: str | None = None
    item_number: str | None = None
    is_active: bool | None = None


class ItemAliasOut(AuditedOut):
    id: UUID
    item_id: UUID | None = None
    alias: str
    alias_type: str | None = None
    is_active: bool | None = None


class ItemAliasPage(Page[ItemAliasOut]):
    pass


class AliasResolveOut(APIModel):
    item_id: UUID
    alias_type: str | None = None
    item_number: str | None = None
    is_active: bool | None = None


class RelatedItemOut(AuditedOut):
    id: UUID
    item_id: UUID | None = None
    related_item_id: UUID
    related_item_number: str | None = None
    relation_type: str | None = None
    sort_order: int | None = None


class RelatedItemPage(Page[RelatedItemOut]):
    pass


class MatrixOptionValueOut(AuditedOut):
    id: UUID
    option_id: UUID | None = None
    value: str | None = None
    code: str
    sort_order: int | None = None
    is_active: bool | None = None


class MatrixOptionOut(AuditedOut):
    id: UUID
    parent_item_id: UUID | None = None
    name: str | None = None
    code: str
    sort_order: int | None = None
    values: list[MatrixOptionValueOut] = Field(default_factory=list)


class MatrixOptionsOut(APIModel):
    options: list[MatrixOptionOut] = Field(default_factory=list)


class MatrixOptionPage(Page[MatrixOptionOut]):
    pass


class MatrixChildOut(APIModel):
    child_item_id: UUID
    item_number: str | None = None
    option_values: dict[str, Any] = Field(default_factory=dict)
    is_active: bool | None = None


class MatrixChildPage(Page[MatrixChildOut]):
    pass


class MatrixGeneratedChildOut(APIModel):
    child_item_id: UUID
    item_number: str
    option_values: dict[str, Any] = Field(default_factory=dict)


class MatrixGenerateOut(APIModel):
    created: list[MatrixGeneratedChildOut] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)


class MatrixLinkOut(APIModel):
    parent_item_id: UUID
    child_item_id: UUID


class KitMemberOut(AuditedOut):
    id: UUID
    parent_item_id: UUID | None = None
    member_item_id: UUID | None = None
    member_item_number: str | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    is_optional: bool | None = None
    sort_order: int | None = None
    member_type: str | None = None


class KitMembersOut(APIModel):
    parent_type: str | None = None
    members: list[KitMemberOut] = Field(default_factory=list)


class TypeMigrationOut(APIModel):
    source_item_id: UUID
    new_item_id: UUID
    new_item_number: str
    status: str
    converted: bool
    rewired: list[Any] = Field(default_factory=list)


# --------------------------------------------------------------------------- request DTOs -----
class ItemCategoryCreate(APIModel):
    code: str
    name: str | None = None
    parent_id: UUID | None = None
    sort_order: int | None = None


class ItemCategoryUpdate(APIModel):
    name: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ItemCategoryMove(APIModel):
    new_parent_id: UUID | None = None


class UomCreate(APIModel):
    uom_type_id: UUID
    code: str
    name: str | None = None
    conversion_factor: Decimal | None = None
    is_base: bool | None = None
    rounding_precision: int | None = None


class UomTypeCreate(APIModel):
    code: str
    name: str | None = None
    base_uom: UomCreate | None = None
    units: list[UomCreate] = Field(default_factory=list)


class UomTypeUpdate(APIModel):
    name: str | None = None
    is_active: bool | None = None


class UomUpdate(APIModel):
    name: str | None = None
    conversion_factor: Decimal | None = None
    rounding_precision: int | None = None
    is_active: bool | None = None


class UomConversionCreate(APIModel):
    uom_type_id: UUID
    from_uom_id: UUID
    to_uom_id: UUID
    factor: Decimal
    notes: str | None = None


class UomResolveRequest(APIModel):
    quantity: Decimal
    from_uom_id: UUID
    to_uom_id: UUID | None = None
    uom_type_id: UUID | None = None
    conversion_override: Decimal | None = None
    whole_base_required: bool | None = None


class ItemBarcodeCreate(APIModel):
    barcode: str
    barcode_type: str = "CODE128"
    uom_id: UUID | None = None
    is_primary: bool | None = None


class ItemBarcodeUpdate(APIModel):
    is_primary: bool | None = None
    uom_id: UUID | None = None
    is_active: bool | None = None


class ItemAliasCreate(APIModel):
    alias: str
    alias_type: str = "WMS"


class ItemRelatedCreate(APIModel):
    related_item_id: UUID
    relation_type: str = "RELATED"
    sort_order: int | None = None


class MatrixOptionValueInput(APIModel):
    code: str
    value: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ItemMatrixOptionCreate(APIModel):
    code: str
    name: str | None = None
    sort_order: int | None = None
    values: list[MatrixOptionValueInput] = Field(default_factory=list)


class MatrixOptionValuesReplace(APIModel):
    values: list[MatrixOptionValueInput] = Field(default_factory=list)


class MatrixCombinationInput(APIModel):
    item_number: str
    display_name: str | None = None
    option_values: dict[str, Any] = Field(default_factory=dict)


class MatrixGenerateRequest(APIModel):
    combinations: list[MatrixCombinationInput] = Field(default_factory=list)


class MatrixChildLinkRequest(APIModel):
    child_item_id: UUID
    option_values: dict[str, Any] = Field(default_factory=dict)


class ItemKitMemberCreate(APIModel):
    member_item_id: UUID
    qty: Decimal | None = None
    uom_id: UUID | None = None
    is_optional: bool | None = None
    sort_order: int | None = None


class ItemKitMemberUpdate(APIModel):
    qty: Decimal | None = None
    uom_id: UUID | None = None
    is_optional: bool | None = None
    sort_order: int | None = None


class ItemUomAssignment(APIModel):
    uom_id: UUID
    role: str
    is_default_for_role: bool | None = None
    conversion_override: Decimal | None = None


class ItemUomAssignmentsReplace(APIModel):
    uoms: list[ItemUomAssignment] = Field(default_factory=list)


class ItemLocationEntry(APIModel):
    location_id: UUID
    is_preferred: bool | None = None
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    auto_calculate_planning: bool | None = None
    default_bin_id: UUID | None = None
    default_status_id: UUID | None = None
    cost_group_override_id: UUID | None = None
    is_consigned_allowed: bool | None = None


class ItemLocationsReplace(APIModel):
    locations: list[ItemLocationEntry] = Field(default_factory=list)


class ItemLocationUpdate(APIModel):
    is_preferred: bool | None = None
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    auto_calculate_planning: bool | None = None
    default_bin_id: UUID | None = None
    default_status_id: UUID | None = None
    cost_group_override_id: UUID | None = None
    is_consigned_allowed: bool | None = None


class PlanningValuesApply(APIModel):
    reorder_point: Decimal | None = None
    preferred_stock_level: Decimal | None = None
    safety_stock: Decimal | None = None
    lead_time_days: int | None = None
    seasonal_demand: Decimal | None = None
    source: str | None = None
    policy_version: str | None = None


class ItemInventorySettingsUpdate(APIModel):
    costing_method: str | None = None
    initial_cost: Decimal | None = None
    stock_uom_id: UUID | None = None
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
    must_be_whole_base_unit: bool | None = None


class ItemPurchasingSettingsUpdate(APIModel):
    preferred_vendor_id: UUID | None = None
    purchase_uom_id: UUID | None = None
    purchase_price: Decimal | None = None
    purchase_price_currency: str | None = None
    lead_time_days: int | None = None
    is_drop_ship: bool | None = None
    is_special_order: bool | None = None
    country_of_origin: str | None = None
    harmonized_code: str | None = None
    purchase_tax_code: str | None = None


class ItemSalesSettingsUpdate(APIModel):
    sale_uom_id: UUID | None = None
    price_level_id: UUID | None = None
    is_taxable: bool | None = None
    shipping_class: str | None = None
    weight: Decimal | None = None
    weight_uom: str | None = None
    is_sellable: bool | None = None
    is_shippable: bool | None = None
    quantity_pricing_schedule_id: UUID | None = None
    pricing_group_id: UUID | None = None
    matrix_parent_qty_behavior: str | None = None


class ItemAccountingSettingsUpdate(APIModel):
    income_account_ref: str | None = None
    cogs_account_ref: str | None = None
    asset_account_ref: str | None = None
    expense_account_ref: str | None = None
    variance_account_ref: str | None = None
    deferred_revenue_account_ref: str | None = None
    tax_schedule_ref: str | None = None
    revenue_recognition_template: str | None = None


class ItemCustomFieldsUpdate(APIModel):
    custom_fields: dict[str, Any] | None = None
    values: dict[str, Any] | None = None


class ItemInactivateRequest(APIModel):
    reason: str | None = None


class ItemConvertToInventoryRequest(APIModel):
    item_number: str | None = None
    target_item_type_code: str | None = None
    reason: str | None = None
    rewire: list[dict] = Field(default_factory=list)
