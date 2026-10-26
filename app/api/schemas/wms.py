"""WMS boundary DTOs (native FastAPI request/response models for `wms.execution@1.0.0`).

Responses mirror the service documents (rows from ``wms.*`` tables plus the documented nested
collections/projections). Requests carry the fields each handler passes to the service; unknown extras are
ignored at the boundary, matching the pre-DTO behaviour where the service filtered its accepted keys.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field

from app.api.schemas.common import APIModel, Page

# ==================================================================== response records ========


class WmsRecord(APIModel):
    id: UUID
    tenant_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    version: int | None = None


class SystemRuleOut(APIModel):
    id: UUID | None = None
    tenant_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    description: str | None = None
    default_enabled: bool | None = None
    applies_to: str | None = None
    value_type: str | None = None
    is_active: bool | None = None
    effective_enabled: bool | None = None
    override: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int | None = None


class WarehouseOut(WmsRecord):
    location_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_wms_enabled: bool | None = None
    receiving_bin_id: UUID | None = None
    staging_bin_id: UUID | None = None
    shipping_bin_id: UUID | None = None
    default_order_type_id: UUID | None = None
    timezone: str | None = None
    is_active: bool | None = None
    location_code: str | None = None
    location_name: str | None = None
    rules: list[SystemRuleOut] = Field(default_factory=list)


class ItemProcessFamilyOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class ItemProcessGroupOut(WmsRecord):
    item_process_family_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class ItemProcessAssignmentOut(WmsRecord):
    item_id: UUID | None = None
    item_process_group_id: UUID | None = None
    location_id: UUID | None = None


class ItemStockLocationOut(WmsRecord):
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    is_primary: bool | None = None
    replenishment_source_bin_id: UUID | None = None


class OrderTypeOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    direction: str | None = None
    is_active: bool | None = None


class UccCodeFormatOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    format_pattern: str | None = None
    is_active: bool | None = None


class CarrierServiceLevelOut(WmsRecord):
    carrier_code: str | None = None
    service_code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class PickStrategyRuleOut(WmsRecord):
    pick_strategy_id: UUID | None = None
    sequence: int | None = None
    zone_id: UUID | None = None
    item_process_group_id: UUID | None = None
    sort_direction: str | None = None
    is_active: bool | None = None


class PickStrategyOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    priority: int | None = None
    applies_to_location_id: UUID | None = None
    is_active: bool | None = None
    rules: list[PickStrategyRuleOut] = Field(default_factory=list)


class PutawayStrategyRuleOut(WmsRecord):
    putaway_strategy_id: UUID | None = None
    sequence: int | None = None
    zone_id: UUID | None = None
    item_process_group_id: UUID | None = None
    bin_type: str | None = None
    max_qty_per_bin: Decimal | None = None
    is_active: bool | None = None


class PutawayStrategyOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    priority: int | None = None
    applies_to_location_id: UUID | None = None
    staging_bin_id: UUID | None = None
    is_active: bool | None = None
    rules: list[PutawayStrategyRuleOut] = Field(default_factory=list)


class BinBlockOut(WmsRecord):
    bin_id: UUID | None = None
    reason_code_id: UUID | None = None
    reason: str | None = None
    blocked_from: datetime | None = None
    blocked_to: datetime | None = None
    released_at: datetime | None = None


class InboundShipmentLineOut(WmsRecord):
    inbound_shipment_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    qty_expected: Decimal | None = None
    qty_received: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    inventory_status_id: UUID | None = None
    unit_cost: Decimal | None = None


class InboundShipmentOut(WmsRecord):
    shipment_number: str | None = None
    vendor_id: UUID | None = None
    purchase_order_id: UUID | None = None
    location_id: UUID | None = None
    expected_arrival_date: date | None = None
    status: str | None = None
    carrier_code: str | None = None
    tracking_number: str | None = None
    origin_country: str | None = None
    memo: str | None = None
    received_at: datetime | None = None
    lines: list[InboundShipmentLineOut] = Field(default_factory=list)


class ReceivingOrderLineOut(WmsRecord):
    receiving_order_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    qty_expected: Decimal | None = None
    qty_received: Decimal | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    inventory_status_id: UUID | None = None
    unit_cost: Decimal | None = None
    uom_id: UUID | None = None


class ReceivingOrderOut(WmsRecord):
    receiving_order_number: str | None = None
    inbound_shipment_id: UUID | None = None
    purchase_order_id: UUID | None = None
    location_id: UUID | None = None
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    item_receipt_id: UUID | None = None
    deferred_posting: bool | None = None
    memo: str | None = None
    lines: list[ReceivingOrderLineOut] = Field(default_factory=list)


class ReceivingCompleteOut(ReceivingOrderOut):
    item_receipt: dict[str, Any] | None = None
    putaway_task_ids: list[str] = Field(default_factory=list)


class ReceivingReverseOut(ReceivingOrderOut):
    receipt_reversal: dict[str, Any] | None = None


class PutawayTaskLineOut(WmsRecord):
    putaway_task_id: UUID | None = None
    line_number: int | None = None
    bin_id: UUID | None = None
    qty: Decimal | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None


class PutawayTaskOut(WmsRecord):
    task_number: str | None = None
    receiving_order_line_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    source_bin_id: UUID | None = None
    suggested_bin_id: UUID | None = None
    actual_bin_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    strategy_id: UUID | None = None
    strategy_rule_id: UUID | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    is_cart_putaway: bool | None = None
    cart_id: UUID | None = None
    exception_reason: str | None = None
    completed_at: datetime | None = None
    posted_transaction_id: UUID | None = None
    lines: list[PutawayTaskLineOut] = Field(default_factory=list)


class WaveLineOut(WmsRecord):
    wave_id: UUID | None = None
    line_number: int | None = None
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    priority: int | None = None
    status: str | None = None
    pick_task_id: UUID | None = None


class WaveTaskSummaryOut(APIModel):
    id: UUID
    task_number: str | None = None
    status: str | None = None
    task_type: str | None = None


class WaveOut(WmsRecord):
    wave_number: str | None = None
    location_id: UUID | None = None
    status: str | None = None
    released_at: datetime | None = None
    completed_at: datetime | None = None
    release_schedule_id: UUID | None = None
    order_count: int | None = None
    line_count: int | None = None
    memo: str | None = None
    lines: list[WaveLineOut] = Field(default_factory=list)
    tasks: list[WaveTaskSummaryOut] = Field(default_factory=list)


class ReleaseWaveOut(WaveOut):
    pick_task_ids: list[str] = Field(default_factory=list)


class WaveReleaseScheduleOut(WmsRecord):
    code: str | None = None
    name: str | None = None
    cron_expr: str | None = None
    filter: Any = None
    location_id: UUID | None = None
    is_active: bool | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


class PickTaskLineOut(WmsRecord):
    pick_task_id: UUID | None = None
    line_number: int | None = None
    item_id: UUID | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    qty_requested: Decimal | None = None
    qty_picked: Decimal | None = None
    qty_short: Decimal | None = None
    uom_id: UUID | None = None
    suggested_bin_id: UUID | None = None
    actual_bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None
    status: str | None = None


class PickTaskOut(WmsRecord):
    task_number: str | None = None
    task_type: str | None = None
    wave_id: UUID | None = None
    pick_strategy_id: UUID | None = None
    location_id: UUID | None = None
    zone_id: UUID | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    sequence: int | None = None
    priority: int | None = None
    cart_id: UUID | None = None
    completed_at: datetime | None = None
    posted_transaction_id: UUID | None = None
    lines: list[PickTaskLineOut] = Field(default_factory=list)


class PackTaskOut(WmsRecord):
    task_number: str | None = None
    pick_task_id: UUID | None = None
    location_id: UUID | None = None
    status: str | None = None
    packed_by: UUID | None = None
    packed_at: datetime | None = None
    package_count: int | None = None
    notes: str | None = None


class PackCartonLineOut(WmsRecord):
    pack_carton_id: UUID | None = None
    pick_task_line_id: UUID | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None


class PackCartonOut(WmsRecord):
    pack_task_id: UUID | None = None
    carton_number: str | None = None
    carton_type: str | None = None
    tracking_number: str | None = None
    weight: Decimal | None = None
    weight_uom: str | None = None
    status: str | None = None
    lines: list[PackCartonLineOut] = Field(default_factory=list)


class ShipmentLineOut(WmsRecord):
    shipment_id: UUID | None = None
    line_number: int | None = None
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    warehouse_shipment_status: str | None = None


class ShipManifestLineOut(WmsRecord):
    ship_manifest_id: UUID | None = None
    carton_number: str | None = None
    tracking_number: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    weight: Decimal | None = None


class ShipManifestOut(WmsRecord):
    manifest_number: str | None = None
    shipment_id: UUID | None = None
    carrier_code: str | None = None
    manifest_date: date | None = None
    status: str | None = None
    payload: Any = None
    response: Any = None
    error_text: str | None = None
    lines: list[ShipManifestLineOut] = Field(default_factory=list)


class ShipmentOut(WmsRecord):
    shipment_number: str | None = None
    location_id: UUID | None = None
    carrier_code: str | None = None
    carrier_service_level_id: UUID | None = None
    status: str | None = None
    shipped_at: datetime | None = None
    tracking_number: str | None = None
    is_manual_ship: bool | None = None
    item_fulfillment_ref: str | None = None
    posted_transaction_id: UUID | None = None
    lines: list[ShipmentLineOut] = Field(default_factory=list)
    manifests: list[ShipManifestOut] = Field(default_factory=list)


class ReplenishmentTaskOut(WmsRecord):
    task_number: str | None = None
    replenishment_plan_id: UUID | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    from_bin_id: UUID | None = None
    to_bin_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    completed_at: datetime | None = None
    posted_transaction_id: UUID | None = None


class ReplenishmentPlanOut(WmsRecord):
    plan_number: str | None = None
    location_id: UUID | None = None
    plan_date: date | None = None
    status: str | None = None
    trigger: str | None = None
    task_count: int | None = None
    memo: str | None = None
    tasks: list[ReplenishmentTaskOut] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)


class StatusChangeTaskOut(WmsRecord):
    task_number: str | None = None
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] | None = None
    from_status_id: UUID | None = None
    to_status_id: UUID | None = None
    qty: Decimal | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    completed_at: datetime | None = None
    posted_transaction_id: UUID | None = None


class CartOut(WmsRecord):
    cart_code: str | None = None
    location_id: UUID | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class DeviceSessionOut(WmsRecord):
    warehouse_id: UUID | None = None
    user_id: UUID | None = None
    session_token_hash: str | None = None
    session_token: str | None = None
    started_at: datetime | None = None
    last_seen_at: datetime | None = None
    ended_at: datetime | None = None
    client_info: Any = None


# ============================================================== action / projection DTOs ====


class SystemRuleSettingOut(APIModel):
    code: str
    warehouse_id: UUID
    is_enabled: bool


class ItemBinAssignmentOut(APIModel):
    warehouse_id: UUID
    bin_id: UUID
    assigned: int


class WarehouseInventoryRowOut(APIModel):
    item_id: UUID | None = None
    item_number: str | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    bin_code: str | None = None
    inventory_status_id: UUID | None = None
    status_code: str | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    qty_on_hand: Decimal | None = None
    qty_committed: Decimal | None = None


class WarehouseInventoryOut(APIModel):
    warehouse_id: UUID
    location_id: UUID
    items: list[WarehouseInventoryRowOut] = Field(default_factory=list)
    count: int | None = None


class WarehouseInventoryCreateOut(APIModel):
    warehouse_id: UUID
    inventory_transaction_id: str
    qty: Decimal


class CycleCountRequestOut(APIModel):
    warehouse_id: UUID
    count_id: UUID | None = None
    status: str | None = None


class GeneratePutawayOut(APIModel):
    receiving_order_id: UUID
    task_ids: list[str] = Field(default_factory=list)
    created: int | None = None


class ScheduleRunOut(APIModel):
    schedule_id: UUID
    released_wave_ids: list[str] = Field(default_factory=list)


class TaskSummaryOut(APIModel):
    id: UUID
    task_number: str | None = None
    status: str | None = None
    assigned_user_id: UUID | None = None
    created_at: datetime | None = None
    task_type: str | None = None


class FulfillmentGenerationOut(APIModel):
    pick_task_id: UUID
    task_number: str | None = None
    wave_id: UUID | None = None
    status: str | None = None
    posted_transaction_id: UUID | None = None
    completed_at: datetime | None = None


class OrderReleaseStatusOut(APIModel):
    wave_line_id: UUID
    wave_number: str | None = None
    wave_status: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    status: str | None = None
    pick_task_id: UUID | None = None


# ======================================================================= page wrappers ========


class WarehousePage(Page[WarehouseOut]):
    pass


class ItemProcessFamilyPage(Page[ItemProcessFamilyOut]):
    pass


class ItemProcessGroupPage(Page[ItemProcessGroupOut]):
    pass


class ItemProcessAssignmentPage(Page[ItemProcessAssignmentOut]):
    pass


class ItemStockLocationPage(Page[ItemStockLocationOut]):
    pass


class OrderTypePage(Page[OrderTypeOut]):
    pass


class UccCodeFormatPage(Page[UccCodeFormatOut]):
    pass


class CarrierServiceLevelPage(Page[CarrierServiceLevelOut]):
    pass


class PickStrategyPage(Page[PickStrategyOut]):
    pass


class PutawayStrategyPage(Page[PutawayStrategyOut]):
    pass


class SystemRulePage(Page[SystemRuleOut]):
    pass


class BinBlockPage(Page[BinBlockOut]):
    pass


class InboundShipmentPage(Page[InboundShipmentOut]):
    pass


class ReceivingOrderPage(Page[ReceivingOrderOut]):
    pass


class PutawayTaskPage(Page[PutawayTaskOut]):
    pass


class WavePage(Page[WaveOut]):
    pass


class WaveReleaseSchedulePage(Page[WaveReleaseScheduleOut]):
    pass


class PickTaskPage(Page[PickTaskOut]):
    pass


class PackTaskPage(Page[PackTaskOut]):
    pass


class ShipmentPage(Page[ShipmentOut]):
    pass


class ShipManifestPage(Page[ShipManifestOut]):
    pass


class ReplenishmentPlanPage(Page[ReplenishmentPlanOut]):
    pass


class ReplenishmentTaskPage(Page[ReplenishmentTaskOut]):
    pass


class CartPage(Page[CartOut]):
    pass


class TaskSummaryPage(Page[TaskSummaryOut]):
    pass


class FulfillmentGenerationPage(Page[FulfillmentGenerationOut]):
    pass


class OrderReleaseStatusPage(Page[OrderReleaseStatusOut]):
    pass


# ============================================================== request bodies ================
# All fields are optional so that missing values continue to be reported by the domain service
# (same problem+json VALIDATION_FAILED envelope as before the DTO boundary).


class StrategyRuleIn(APIModel):
    sequence: int | None = None
    zone_id: UUID | None = None
    item_process_group_id: UUID | None = None
    sort_direction: str | None = None
    bin_type: str | None = None
    max_qty_per_bin: Decimal | None = None
    is_active: bool | None = None


class WarehouseCreate(APIModel):
    location_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_wms_enabled: bool | None = None
    receiving_bin_id: UUID | None = None
    staging_bin_id: UUID | None = None
    shipping_bin_id: UUID | None = None
    default_order_type_id: UUID | None = None
    timezone: str | None = None


class WarehouseUpdate(APIModel):
    name: str | None = None
    is_wms_enabled: bool | None = None
    receiving_bin_id: UUID | None = None
    staging_bin_id: UUID | None = None
    shipping_bin_id: UUID | None = None
    default_order_type_id: UUID | None = None
    timezone: str | None = None
    is_active: bool | None = None
    version: int | None = None


class ItemProcessFamilyCreate(APIModel):
    code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class ItemProcessGroupCreate(APIModel):
    item_process_family_id: UUID | None = None
    code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class ItemProcessAssignmentCreate(APIModel):
    item_id: UUID | None = None
    item_process_group_id: UUID | None = None
    location_id: UUID | None = None


class ItemStockLocationCreate(APIModel):
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    is_primary: bool | None = None
    replenishment_source_bin_id: UUID | None = None


class ItemStockLocationUpdate(ItemStockLocationCreate):
    id: UUID | None = None
    version: int | None = None


class OrderTypeCreate(APIModel):
    code: str | None = None
    name: str | None = None
    direction: str | None = None
    is_active: bool | None = None


class UccCodeFormatCreate(APIModel):
    code: str | None = None
    name: str | None = None
    format_pattern: str | None = None
    is_active: bool | None = None


class CarrierServiceLevelCreate(APIModel):
    carrier_code: str | None = None
    service_code: str | None = None
    name: str | None = None
    is_active: bool | None = None


class PickStrategyCreate(APIModel):
    code: str | None = None
    name: str | None = None
    priority: int | None = None
    applies_to_location_id: UUID | None = None
    is_active: bool | None = None
    rules: list[StrategyRuleIn] = Field(default_factory=list)


class PickStrategyUpdate(PickStrategyCreate):
    id: UUID | None = None
    version: int | None = None


class PutawayStrategyCreate(APIModel):
    code: str | None = None
    name: str | None = None
    priority: int | None = None
    applies_to_location_id: UUID | None = None
    staging_bin_id: UUID | None = None
    is_active: bool | None = None
    rules: list[StrategyRuleIn] = Field(default_factory=list)


class PutawayStrategyUpdate(PutawayStrategyCreate):
    id: UUID | None = None
    version: int | None = None


class SystemRulePut(APIModel):
    is_enabled: bool | None = None
    value: Any = None


class BinBlockCreate(APIModel):
    bin_id: UUID | None = None
    reason_code_id: UUID | None = None
    reason: str | None = None
    blocked_from: datetime | None = None
    blocked_to: datetime | None = None


class ItemBinAssignmentPut(APIModel):
    bin_id: UUID | None = None
    item_ids: list[UUID] = Field(default_factory=list)


class WarehouseInventoryCreate(APIModel):
    item_id: UUID | None = None
    bin_id: UUID | None = None
    qty: Decimal | None = None
    inventory_status_id: UUID | None = None
    lot_id: UUID | None = None
    reason_code_id: UUID | None = None
    unit_cost: Decimal | None = None
    transaction_date: date | None = None


class CycleCountRequestCreate(APIModel):
    item_ids: list[UUID] = Field(default_factory=list)
    bin_ids: list[UUID] = Field(default_factory=list)
    plan_date: date | None = None
    assignee_id: UUID | None = None
    approval_required: bool | None = None


class InboundShipmentLineCreate(APIModel):
    line_number: int | None = None
    item_id: UUID | None = None
    purchase_order_line_id: UUID | None = None
    qty_expected: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] = Field(default_factory=list)
    inventory_status_id: UUID | None = None
    unit_cost: Decimal | None = None


class InboundShipmentCreate(APIModel):
    location_id: UUID | None = None
    vendor_id: UUID | None = None
    purchase_order_id: UUID | None = None
    expected_arrival_date: date | None = None
    carrier_code: str | None = None
    tracking_number: str | None = None
    origin_country: str | None = None
    memo: str | None = None
    shipment_number: str | None = None
    lines: list[InboundShipmentLineCreate] = Field(default_factory=list)


class ReceivingOrderLineCreate(InboundShipmentLineCreate):
    qty_received: Decimal | None = None
    bin_id: UUID | None = None


class ReceivingOrderCreate(APIModel):
    inbound_shipment_id: UUID | None = None
    purchase_order_id: UUID | None = None
    location_id: UUID | None = None
    deferred_posting: bool | None = None
    memo: str | None = None
    lines: list[ReceivingOrderLineCreate] = Field(default_factory=list)


class ReceivingCompleteLine(APIModel):
    line_number: int | None = None
    qty_received: Decimal | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] = Field(default_factory=list)
    inventory_status_id: UUID | None = None


class ReceivingCompleteRequest(APIModel):
    lines: list[ReceivingCompleteLine] = Field(default_factory=list)
    vendor_id: UUID | None = None
    deferred: bool | None = None
    defer_posting: bool | None = None
    received_at: datetime | None = None
    memo: str | None = None
    generate_putaway: bool | None = None


class PutawayGenerateRequest(APIModel):
    receiving_order_id: UUID | None = None


class TaskAssignIn(APIModel):
    assigned_user_id: UUID | None = None
    cart_id: UUID | None = None


class TaskCompleteLine(APIModel):
    line_number: int | None = None
    bin_id: UUID | None = None
    qty: Decimal | None = None
    lot_id: UUID | None = None
    actual_bin_id: UUID | None = None
    qty_picked: Decimal | None = None
    inventory_status_id: UUID | None = None


class TaskCompleteRequest(APIModel):
    lines: list[TaskCompleteLine] = Field(default_factory=list)
    transaction_date: date | None = None
    qty: Decimal | None = None


class TaskExceptionIn(APIModel):
    reason_code: str | None = None
    reason: str | None = None


class CancelTaskIn(APIModel):
    reason: str | None = None


class TransitionIn(APIModel):
    reason: str | None = None
    note: str | None = None
    transaction_date: date | None = None


class WaveLineCreate(APIModel):
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    priority: int | None = None


class WaveCreate(APIModel):
    location_id: UUID | None = None
    release_schedule_id: UUID | None = None
    lines: list[WaveLineCreate] = Field(default_factory=list)


class WaveReleaseRequest(APIModel):
    suggested_bin_id: UUID | None = None


class WaveReleaseScheduleCreate(APIModel):
    code: str | None = None
    name: str | None = None
    cron_expr: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    location_id: UUID | None = None
    is_active: bool | None = None
    next_run_at: datetime | None = None


class WaveReleaseScheduleUpdate(APIModel):
    id: UUID | None = None
    name: str | None = None
    cron_expr: str | None = None
    filter: dict[str, Any] | None = None
    is_active: bool | None = None
    next_run_at: datetime | None = None
    version: int | None = None


class PickCompleteRequest(APIModel):
    lines: list[TaskCompleteLine] = Field(default_factory=list)
    transaction_date: date | None = None


class PackTaskCreate(APIModel):
    location_id: UUID | None = None
    pick_task_id: UUID | None = None


class PackCartonLineCreate(APIModel):
    pick_task_line_id: UUID | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] = Field(default_factory=list)


class PackCartonCreate(APIModel):
    carton_number: str | None = None
    carton_type: str | None = None
    tracking_number: str | None = None
    weight: Decimal | None = None
    weight_uom: str | None = None
    lines: list[PackCartonLineCreate] = Field(default_factory=list)


class PackCompleteRequest(APIModel):
    package_count: int | None = None


class ShipmentLineCreate(APIModel):
    source_type: str | None = None
    source_ref: str | None = None
    source_line_ref: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    uom_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] = Field(default_factory=list)


class ShipmentCreate(APIModel):
    location_id: UUID | None = None
    carrier_code: str | None = None
    carrier_service_level_id: UUID | None = None
    tracking_number: str | None = None
    is_manual_ship: bool | None = None
    lines: list[ShipmentLineCreate] = Field(default_factory=list)


class ShipmentActionRequest(APIModel):
    transaction_date: date | None = None


class ShipManifestLineCreate(APIModel):
    carton_number: str | None = None
    tracking_number: str | None = None
    item_id: UUID | None = None
    qty: Decimal | None = None
    weight: Decimal | None = None


class ShipManifestCreate(APIModel):
    shipment_id: UUID | None = None
    carrier_code: str | None = None
    manifest_date: date | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    lines: list[ShipManifestLineCreate] = Field(default_factory=list)


class ReplenishmentPlanCreate(APIModel):
    location_id: UUID | None = None
    plan_date: date | None = None
    trigger: str | None = None
    memo: str | None = None


class ReplenishmentTaskComplete(APIModel):
    qty: Decimal | None = None


class StatusChangeTaskCreate(APIModel):
    item_id: UUID | None = None
    location_id: UUID | None = None
    bin_id: UUID | None = None
    lot_id: UUID | None = None
    serial_ids: list[UUID] = Field(default_factory=list)
    from_status_id: UUID | None = None
    to_status_id: UUID | None = None
    qty: Decimal | None = None


class CartCreate(APIModel):
    location_id: UUID | None = None
    cart_code: str | None = None
    assigned_user_id: UUID | None = None


class CartUpdate(APIModel):
    status: str | None = None
    assigned_user_id: UUID | None = None


class DeviceSessionCreate(APIModel):
    warehouse_id: UUID | None = None
    user_id: UUID | None = None
    client_info: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "BinBlockCreate",
    "BinBlockOut",
    "BinBlockPage",
    "CancelTaskIn",
    "CarrierServiceLevelCreate",
    "CarrierServiceLevelOut",
    "CarrierServiceLevelPage",
    "CartCreate",
    "CartOut",
    "CartPage",
    "CartUpdate",
    "CycleCountRequestCreate",
    "CycleCountRequestOut",
    "DeviceSessionCreate",
    "DeviceSessionOut",
    "FulfillmentGenerationOut",
    "FulfillmentGenerationPage",
    "GeneratePutawayOut",
    "InboundShipmentCreate",
    "InboundShipmentLineCreate",
    "InboundShipmentLineOut",
    "InboundShipmentOut",
    "InboundShipmentPage",
    "ItemBinAssignmentOut",
    "ItemBinAssignmentPut",
    "ItemProcessAssignmentCreate",
    "ItemProcessAssignmentOut",
    "ItemProcessAssignmentPage",
    "ItemProcessFamilyCreate",
    "ItemProcessFamilyOut",
    "ItemProcessFamilyPage",
    "ItemProcessGroupCreate",
    "ItemProcessGroupOut",
    "ItemProcessGroupPage",
    "ItemStockLocationCreate",
    "ItemStockLocationOut",
    "ItemStockLocationPage",
    "ItemStockLocationUpdate",
    "OrderReleaseStatusOut",
    "OrderReleaseStatusPage",
    "OrderTypeCreate",
    "OrderTypeOut",
    "OrderTypePage",
    "PackCartonCreate",
    "PackCartonLineCreate",
    "PackCartonLineOut",
    "PackCartonOut",
    "PackCompleteRequest",
    "PackTaskCreate",
    "PackTaskOut",
    "PackTaskPage",
    "PickCompleteRequest",
    "PickStrategyCreate",
    "PickStrategyOut",
    "PickStrategyPage",
    "PickStrategyRuleOut",
    "PickStrategyUpdate",
    "PickTaskLineOut",
    "PickTaskOut",
    "PickTaskPage",
    "PutawayGenerateRequest",
    "PutawayStrategyCreate",
    "PutawayStrategyOut",
    "PutawayStrategyPage",
    "PutawayStrategyRuleOut",
    "PutawayStrategyUpdate",
    "PutawayTaskLineOut",
    "PutawayTaskOut",
    "PutawayTaskPage",
    "ReceivingCompleteOut",
    "ReceivingCompleteLine",
    "ReceivingCompleteRequest",
    "ReceivingOrderCreate",
    "ReceivingOrderLineCreate",
    "ReceivingOrderLineOut",
    "ReceivingOrderOut",
    "ReceivingOrderPage",
    "ReceivingReverseOut",
    "ReleaseWaveOut",
    "ReplenishmentPlanCreate",
    "ReplenishmentPlanOut",
    "ReplenishmentPlanPage",
    "ReplenishmentTaskComplete",
    "ReplenishmentTaskOut",
    "ReplenishmentTaskPage",
    "ScheduleRunOut",
    "ShipManifestCreate",
    "ShipManifestLineCreate",
    "ShipManifestLineOut",
    "ShipManifestOut",
    "ShipManifestPage",
    "ShipmentActionRequest",
    "ShipmentCreate",
    "ShipmentLineCreate",
    "ShipmentLineOut",
    "ShipmentOut",
    "ShipmentPage",
    "StatusChangeTaskCreate",
    "StatusChangeTaskOut",
    "StrategyRuleIn",
    "SystemRuleOut",
    "SystemRulePage",
    "SystemRulePut",
    "SystemRuleSettingOut",
    "TaskAssignIn",
    "TaskCompleteLine",
    "TaskCompleteRequest",
    "TaskExceptionIn",
    "TaskSummaryOut",
    "TaskSummaryPage",
    "TransitionIn",
    "UccCodeFormatCreate",
    "UccCodeFormatOut",
    "UccCodeFormatPage",
    "WarehouseCreate",
    "WarehouseInventoryCreate",
    "WarehouseInventoryCreateOut",
    "WarehouseInventoryOut",
    "WarehouseInventoryRowOut",
    "WarehouseOut",
    "WarehousePage",
    "WarehouseUpdate",
    "WaveCreate",
    "WaveLineCreate",
    "WaveLineOut",
    "WaveOut",
    "WavePage",
    "WaveReleaseRequest",
    "WaveReleaseScheduleCreate",
    "WaveReleaseScheduleOut",
    "WaveReleaseSchedulePage",
    "WaveReleaseScheduleUpdate",
    "WaveTaskSummaryOut",
]
