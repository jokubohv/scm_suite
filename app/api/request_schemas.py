"""Request-body schema registry for operations whose routers accept free-form dict payloads.

The service layer remains the runtime validator. This registry documents the actual fields, types,
required flags and enums in the runtime OpenAPI contract, which is also the source MCP uses to build
tool input schemas. Schemas are matched by HTTP method + path shape so aliases are covered.

Scope: Phase 1 covers every write operation exposed by the curated MCP catalogue plus the principal
resource CRUD operations. ``apply_request_schemas`` injects them into ``app.openapi()``.
"""
from __future__ import annotations

import re
from typing import Any

JSON_SCHEMA_KEY = "application/json"

UUID = {"type": "string", "format": "uuid"}
DATE = {"type": "string", "format": "date"}
DATETIME = {"type": "string", "format": "date-time"}
DECIMAL = {"type": "string", "description": "Decimal encoded as a JSON string"}
MONEY = {"type": "string", "description": "Monetary amount encoded as a JSON string"}
TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}


def _obj(properties: dict[str, Any], required: list[str] | tuple[str, ...] = (),
         *, additional: bool = False, description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _lines(item_schema: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return _arr(_obj(item_schema, required=required))


REASON = _obj({"reason": {**TEXT, "description": "Reason for the lifecycle action"}})
ACTION = _obj({
    "reason": TEXT,
    "memo": TEXT,
    "transaction_date": DATE,
    "override": BOOL,
}, description="Optional lifecycle/action body fields")

# ------------------------------------------------------------------- master data ---------------
ITEM_INVENTORY = _obj({
    "costing_method": {"type": "string", "enum": ["AVERAGE", "FIFO", "LIFO", "GROUP", "STANDARD", "SPECIFIC", "LOT"]},
    "initial_cost": MONEY,
    "stock_uom_id": UUID,
    "asset_account_ref": TEXT,
    "cogs_account_ref": TEXT,
    "adjustment_account_ref": TEXT,
    "variance_account_ref": TEXT,
})
ITEM = _obj({
    "item_number": TEXT,
    "display_name": TEXT,
    "item_type_code": {"type": "string", "description": "NetSuite item type code, e.g. INVENTORY, ASSEMBLY, KIT"},
    "description": TEXT,
    "base_uom_id": UUID,
    "item_category_id": UUID,
    "inventory": ITEM_INVENTORY,
    "purchasing": _obj({"purchase_uom_id": UUID, "default_vendor_id": UUID, "cost": MONEY}),
    "sales": _obj({"sales_uom_id": UUID, "price_level_id": UUID}),
    "accounting": _obj({"income_account_ref": TEXT, "expense_account_ref": TEXT}),
}, required=("item_number", "display_name", "item_type_code"), description="Create item")
ITEM_UPDATE = _obj({
    "display_name": TEXT,
    "description": TEXT,
    "base_uom_id": UUID,
    "item_category_id": UUID,
    "inventory": ITEM_INVENTORY,
    "purchasing": {"type": "object", "additionalProperties": True},
    "sales": {"type": "object", "additionalProperties": True},
    "accounting": {"type": "object", "additionalProperties": True},
}, description="Update item; all fields optional")
LOCATION = _obj({
    "code": TEXT, "name": TEXT,
    "location_type": {"type": "string", "enum": ["WAREHOUSE", "STORE", "RECEIVING", "TRANSIT", "CONSUMPTION"]},
    "parent_id": UUID, "address": {"type": "object", "additionalProperties": True},
    "subsidiary_ref": TEXT, "timezone": TEXT,
    "is_bins_enabled": BOOL, "is_statuses_enabled": BOOL,
    "is_consignment_allowed": BOOL, "allow_commit_from_non_available": BOOL,
}, required=("code", "name", "location_type"))
LOCATION_UPDATE = _obj({
    "name": TEXT, "address": {"type": "object", "additionalProperties": True}, "parent_id": UUID,
    "subsidiary_ref": TEXT, "timezone": TEXT, "is_bins_enabled": BOOL, "is_statuses_enabled": BOOL,
    "is_consignment_allowed": BOOL, "allow_commit_from_non_available": BOOL,
})
BIN = _obj({
    "location_id": UUID, "code": TEXT, "name": TEXT,
    "bin_type": {"type": "string", "enum": ["STORAGE", "PICKING", "STAGING", "RECEIVING", "DOCK"]},
    "zone_id": UUID, "is_pickable": BOOL, "capacity_qty": DECIMAL, "capacity_uom_id": UUID,
    "coordinates": {"type": "object", "additionalProperties": True},
}, required=("location_id", "code", "name"))
BIN_UPDATE = _obj({"name": TEXT, "bin_type": {"type": "string"}, "zone_id": UUID, "is_pickable": BOOL,
                   "coordinates": {"type": "object", "additionalProperties": True}})
STATUS = _obj({
    "code": TEXT, "name": TEXT,
    "is_available_for_allocation": BOOL, "is_committable": BOOL, "is_sellable": BOOL,
    "is_consigned": BOOL, "is_default": BOOL, "is_active": BOOL,
}, required=("code", "name"))
STATUS_UPDATE = _obj({
    "name": TEXT, "is_available_for_allocation": BOOL, "is_committable": BOOL, "is_sellable": BOOL,
    "is_consigned": BOOL, "is_default": BOOL, "is_active": BOOL,
})
CATEGORY = _obj({"code": TEXT, "name": TEXT, "parent_id": UUID}, required=("code", "name"))
CATEGORY_UPDATE = _obj({"name": TEXT, "parent_id": UUID})
UOM = _obj({"uom_type_id": UUID, "code": TEXT, "name": TEXT, "conversion_factor": DECIMAL,
            "is_base": BOOL, "rounding_precision": INT}, required=("uom_type_id", "code"))
UOM_UPDATE = _obj({"name": TEXT, "conversion_factor": DECIMAL, "is_base": BOOL, "rounding_precision": INT})
UOM_CONVERSION = _obj({"uom_type_id": UUID, "from_uom_id": UUID, "to_uom_id": UUID,
                       "factor": DECIMAL, "notes": TEXT}, required=("uom_type_id", "from_uom_id", "to_uom_id", "factor"))
PRICE_LEVEL = _obj({"code": TEXT, "name": TEXT, "currency_code": TEXT, "is_default": BOOL,
                    "is_inactive": BOOL, "sort_order": INT}, required=("code",))
PRICE_LEVEL_UPDATE = _obj({"name": TEXT, "currency_code": TEXT, "is_default": BOOL,
                           "is_inactive": BOOL, "sort_order": INT})
ITEM_PRICE = _obj({"item_id": UUID, "price_level_id": UUID, "currency_code": TEXT,
                   "qty_min": DECIMAL, "qty_max": DECIMAL, "unit_price": MONEY,
                   "cost_plus_basis": TEXT, "effective_from": DATE, "effective_to": DATE,
                   "is_active": BOOL}, required=("item_id", "price_level_id", "unit_price"))
ITEM_PRICE_UPDATE = _obj({"qty_min": DECIMAL, "qty_max": DECIMAL, "unit_price": MONEY,
                          "effective_from": DATE, "effective_to": DATE, "is_active": BOOL})

# ------------------------------------------------------------------- inventory ----------------
ADJUSTMENT_LINE = _obj({
    "item_id": UUID, "location_id": UUID, "inventory_status_id": UUID,
    "qty_delta": DECIMAL, "unit_cost": MONEY, "bin_id": UUID, "lot_id": UUID,
    "serial_ids": _arr(UUID), "ownership": {"type": "string", "enum": ["OWNED", "CONSIGNED"]},
}, required=("item_id", "location_id", "qty_delta"))
ADJUSTMENT = _obj({
    "transaction_date": DATE, "memo": TEXT, "reason_code_id": UUID,
    "document_number": TEXT, "lines": _lines(ADJUSTMENT_LINE, ("item_id", "location_id", "qty_delta")),
}, required=("transaction_date", "lines"), description="Create inventory adjustment")
ADJUSTMENT_UPDATE = _obj({"transaction_date": DATE, "memo": TEXT, "reason_code_id": UUID,
                          "lines": _lines(ADJUSTMENT_LINE)})
TRANSFER_LINE = _obj({
    "item_id": UUID, "qty": DECIMAL, "uom_id": UUID, "bin_from_id": UUID, "bin_to_id": UUID,
    "inventory_status_id": UUID, "lot_id": UUID, "serial_ids": _arr(UUID),
    "ownership": {"type": "string", "enum": ["OWNED", "CONSIGNED"]},
}, required=("item_id", "qty"))
TRANSFER = _obj({
    "transaction_date": DATE, "memo": TEXT, "from_location_id": UUID, "to_location_id": UUID,
    "document_number": TEXT, "lines": _lines(TRANSFER_LINE, ("item_id", "qty")),
}, required=("from_location_id", "to_location_id", "lines"))
TRANSFER_UPDATE = _obj({"transaction_date": DATE, "memo": TEXT,
                        "from_location_id": UUID, "to_location_id": UUID, "lines": _lines(TRANSFER_LINE)})
COUNT_SCOPE = _obj({"items": _arr(UUID), "locations": _arr(UUID), "bins": _arr(UUID),
                    "statuses": _arr(UUID), "abc": _arr(TEXT)})
COUNT = _obj({"count_type": {"type": "string", "enum": ["MANUAL", "CYCLE", "CALCULATED"]},
              "count_date": DATE, "blind_count": BOOL, "approval_required": BOOL,
              "scope": COUNT_SCOPE, "variance_account_ref": TEXT})
COUNT_PLAN_UPDATE = _obj({"name": TEXT, "frequency": TEXT, "next_run_date": DATE,
                          "filters": _arr({"type": "object", "additionalProperties": True})})

# ------------------------------------------------------------------- procurement --------------
VENDOR = _obj({
    "vendor_number": TEXT, "name": TEXT, "vendor_category_id": UUID, "currency_code": TEXT,
    "payment_terms": TEXT, "tax_id": TEXT, "is_1099_eligible": BOOL, "credit_limit": MONEY,
    "default_expense_account_ref": TEXT, "default_payable_account_ref": TEXT,
    "addresses": _arr({"type": "object", "additionalProperties": True}),
    "primary_contact": {"type": "object", "additionalProperties": True},
}, required=("vendor_number", "name"), description="Create vendor")
VENDOR_UPDATE = _obj({
    "name": TEXT, "vendor_category_id": UUID, "currency_code": TEXT, "payment_terms": TEXT,
    "tax_id": TEXT, "is_1099_eligible": BOOL, "credit_limit": MONEY,
    "default_expense_account_ref": TEXT, "default_payable_account_ref": TEXT,
    "addresses": _arr({"type": "object", "additionalProperties": True}),
    "primary_contact": {"type": "object", "additionalProperties": True},
})
PO_LINE = _obj({
    "item_id": UUID, "qty_ordered": DECIMAL, "unit_price": MONEY, "uom_id": UUID,
    "expected_receipt_date": DATE, "description": TEXT, "expense_account_ref": TEXT,
    "ship_to_location_id": UUID, "external_sales_ref": TEXT,
    "is_drop_ship": BOOL, "is_special_order": BOOL, "vendor_contract_item_id": UUID,
}, required=("item_id", "qty_ordered"))
PO = _obj({
    "vendor_id": UUID, "po_type": {"type": "string", "enum": ["STANDARD", "BLANKET", "DROP_SHIP", "SPECIAL_ORDER"]},
    "currency_code": TEXT, "transaction_date": DATE, "ship_to_location_id": UUID,
    "bill_to": {"type": "object", "additionalProperties": True}, "vendor_contract_id": UUID,
    "exchange_rate": DECIMAL, "memo": TEXT, "lines": _lines(PO_LINE, ("item_id", "qty_ordered")),
}, required=("vendor_id", "lines"), description="Create purchase order")
PO_UPDATE = _obj({"currency_code": TEXT, "transaction_date": DATE, "ship_to_location_id": UUID,
                  "bill_to": {"type": "object", "additionalProperties": True}, "memo": TEXT,
                  "lines": _lines(PO_LINE)})
REQUEST_LINE = _obj({"item_id": UUID, "qty": DECIMAL, "uom_id": UUID, "target_price": MONEY,
                     "needed_by": DATE, "location_id": UUID, "description": TEXT},
                    required=("item_id", "qty"))
REQUEST = _obj({"transaction_date": DATE, "requested_by": UUID, "currency_code": TEXT,
                "memo": TEXT, "lines": _lines(REQUEST_LINE, ("item_id", "qty"))},
               required=("lines",))
REQUISITION = _obj({"transaction_date": DATE, "requested_by": UUID, "currency_code": TEXT,
                    "memo": TEXT, "lines": _lines(REQUEST_LINE, ("item_id", "qty"))}, required=("lines",))
REQUISITION_UPDATE = _obj({"transaction_date": DATE, "memo": TEXT, "lines": _lines(REQUEST_LINE)})
RFQ = _obj({"vendor_ids": _arr(UUID), "bid_open_date": DATE, "bid_close_date": DATE,
            "currency_code": TEXT, "memo": TEXT,
            "lines": _lines(_obj({"item_id": UUID, "qty": DECIMAL, "uom_id": UUID, "target_price": MONEY},
                                 required=("item_id", "qty")))},
           required=("vendor_ids", "lines"))
CONTRACT = _obj({
    "vendor_id": UUID,
    "contract_type": {"type": "string", "enum": ["CONTRACT", "BLANKET_ORDER"]},
    "contract_number": TEXT, "start_date": DATE, "end_date": DATE, "currency_code": TEXT,
    "auto_renew": BOOL, "memo": TEXT,
    "lines": _lines(_obj({"item_id": UUID, "qty": DECIMAL, "unit_price": MONEY, "uom_id": UUID})),
}, required=("vendor_id",))
CONTRACT_UPDATE = _obj({"end_date": DATE, "auto_renew": BOOL, "memo": TEXT,
                        "lines": _lines({"type": "object", "additionalProperties": True})})
RECEIPT_LINE = _obj({
    "purchase_order_line_id": UUID, "item_id": UUID, "qty_received": DECIMAL, "unit_cost": MONEY,
    "location_id": UUID, "inventory_status_id": UUID, "bin_id": UUID, "lot_id": UUID,
    "serial_ids": _arr(UUID), "ownership": {"type": "string", "enum": ["OWNED", "CONSIGNED"]},
    "details": _arr(_obj({"qty_received": DECIMAL, "inventory_status_id": UUID, "lot_id": UUID,
                          "serial_id": UUID, "bin_id": UUID,
                          "ownership": {"type": "string", "enum": ["OWNED", "CONSIGNED"]}})),
}, required=("item_id", "qty_received"))
RECEIPT = _obj({
    "purchase_order_id": UUID, "vendor_id": UUID,
    "receipt_type": {"type": "string", "enum": ["PO", "ASN", "MANUAL"]},
    "received_at": DATETIME, "location_id": UUID, "memo": TEXT,
    "lines": _lines(RECEIPT_LINE, ("item_id", "qty_received")),
}, required=("vendor_id", "lines"), description="Create item receipt")
RECEIPT_UPDATE = _obj({"memo": TEXT, "lines": _lines(RECEIPT_LINE)})
BILL_LINE = _obj({"item_id": UUID, "qty": DECIMAL, "unit_price": MONEY, "description": TEXT,
                  "purchase_order_line_id": UUID, "item_receipt_line_id": UUID,
                  "expense_account_ref": TEXT}, required=("item_id", "qty"))
BILL = _obj({
    "vendor_id": UUID, "vendor_bill_number": TEXT, "purchase_order_id": UUID,
    "item_receipt_id": UUID, "currency_code": TEXT, "transaction_date": DATE, "due_date": DATE,
    "memo": TEXT, "lines": _lines(BILL_LINE, ("item_id", "qty")),
}, required=("vendor_id", "vendor_bill_number", "lines"), description="Create vendor bill")
BILL_UPDATE = _obj({"transaction_date": DATE, "due_date": DATE, "memo": TEXT, "lines": _lines(BILL_LINE)})
CREDIT_LINE = _obj({"item_id": UUID, "qty": DECIMAL, "unit_price": MONEY,
                    "purchase_order_line_id": UUID, "item_receipt_line_id": UUID},
                   required=("item_id", "qty"))
CREDIT = _obj({"vendor_id": UUID, "vendor_bill_id": UUID, "currency_code": TEXT,
               "transaction_date": DATE, "memo": TEXT, "lines": _lines(CREDIT_LINE, ("item_id", "qty"))},
              required=("vendor_id", "lines"))
RETURN_LINE = _obj({"item_id": UUID, "qty": DECIMAL, "unit_cost": MONEY,
                    "purchase_order_line_id": UUID, "item_receipt_line_id": UUID,
                    "inventory_status_id": UUID, "location_id": UUID}, required=("item_id", "qty"))
RETURN = _obj({"vendor_id": UUID, "purchase_order_id": UUID, "reason_code": TEXT,
               "transaction_date": DATE, "currency_code": TEXT, "memo": TEXT,
               "lines": _lines(RETURN_LINE, ("item_id", "qty"))}, required=("vendor_id", "lines"))

# ------------------------------------------------------------------- WMS ----------------------
WAREHOUSE = _obj({"location_id": UUID, "code": TEXT, "name": TEXT, "is_wms_enabled": BOOL,
                  "receiving_bin_id": UUID, "staging_bin_id": UUID, "shipping_bin_id": UUID,
                  "default_order_type_id": UUID, "timezone": TEXT}, required=("location_id", "code"))
WAREHOUSE_UPDATE = _obj({"name": TEXT, "is_wms_enabled": BOOL, "receiving_bin_id": UUID,
                         "staging_bin_id": UUID, "shipping_bin_id": UUID, "timezone": TEXT})
INBOUND = _obj({"location_id": UUID, "vendor_id": UUID, "purchase_order_id": UUID,
                "carrier_code": TEXT, "tracking_number": TEXT, "expected_arrival_date": DATE,
                "origin_country": TEXT, "memo": TEXT,
                "lines": _lines(_obj({"item_id": UUID, "qty_expected": DECIMAL, "unit_cost": MONEY,
                                      "purchase_order_line_id": UUID, "lot_id": UUID},
                                     required=("item_id", "qty_expected")))},
               required=("location_id", "vendor_id", "lines"))
RECEIVING_ORDER = _obj({"inbound_shipment_id": UUID, "purchase_order_id": UUID,
                        "deferred_posting": BOOL, "memo": TEXT}, required=("inbound_shipment_id",))
RECEIVING_COMPLETE_LINE = _obj({"line_number": INT, "qty_received": DECIMAL,
                                "location_id": UUID, "bin_id": UUID, "inventory_status_id": UUID,
                                "lot_id": UUID, "serial_ids": _arr(UUID)},
                               required=("line_number", "qty_received"))
PUTAWAY_ASSIGN = _obj({"assigned_user_id": UUID}, required=("assigned_user_id",))
PICK_ASSIGN = _obj({"assigned_user_id": UUID}, required=("assigned_user_id",))
PICK_COMPLETE = _obj({"lines": _lines(_obj({"line_number": INT, "qty_picked": DECIMAL,
                                            "actual_bin_id": UUID}, required=("line_number", "qty_picked")))})
PACK_TASK = _obj({"warehouse_id": UUID, "shipment_id": UUID,
                  "lines": _lines(_obj({"item_id": UUID, "qty": DECIMAL, "carton_number": TEXT}))})
PACK_COMPLETE = _obj({"cartons": _arr(_obj({"carton_number": TEXT, "weight": DECIMAL,
                                            "length": DECIMAL, "width": DECIMAL, "height": DECIMAL}))})
SHIPMENT = _obj({"warehouse_id": UUID, "ship_from_location_id": UUID, "carrier_code": TEXT,
                 "service_level": TEXT, "tracking_number": TEXT,
                 "lines": _lines(_obj({"item_id": UUID, "qty": DECIMAL}))})
WAVE_LINE = _obj({"source_type": TEXT, "source_ref": TEXT, "source_line_ref": TEXT,
                  "item_id": UUID, "qty": DECIMAL, "location_id": UUID}, required=("item_id", "qty"))
WAVE = _obj({"location_id": UUID, "memo": TEXT, "lines": _lines(WAVE_LINE, ("item_id", "qty"))},
            required=("location_id", "lines"))

# ------------------------------------------------------------------- manufacturing ------------
ASSEMBLY_UPDATE = _obj({"assembly_type": TEXT, "default_bom_id": UUID, "default_routing_id": UUID,
                        "unbuild_variance_account_ref": TEXT, "wip_account_ref": TEXT,
                        "revision_control": TEXT, "backflush_default": BOOL})
BOM_UPDATE = _obj({"name": TEXT, "is_default": BOOL, "effective_from": DATE, "effective_to": DATE})
ROUTING = _obj({"assembly_item_id": UUID, "code": TEXT, "name": TEXT, "is_default": BOOL,
                "steps": _arr(_obj({"sequence": INT, "work_center_id": UUID, "setup_time": DECIMAL,
                                    "run_time": DECIMAL}))}, required=("code",))
ROUTING_UPDATE = _obj({"name": TEXT, "is_default": BOOL,
                       "steps": _arr({"type": "object", "additionalProperties": True})})
WORK_CENTER = _obj({"code": TEXT, "name": TEXT, "location_id": UUID, "capacity_per_day": DECIMAL,
                    "cost_per_hour": MONEY, "is_active": BOOL}, required=("code", "name"))
WORK_CENTER_UPDATE = _obj({"name": TEXT, "capacity_per_day": DECIMAL, "cost_per_hour": MONEY,
                           "is_active": BOOL})
WORK_ORDER_UPDATE = _obj({"qty_ordered": DECIMAL, "start_date": DATE, "end_date": DATE,
                          "uom_id": UUID, "memo": TEXT})
WORK_ORDER_ISSUE = _obj({"qty": DECIMAL, "transaction_date": DATE,
                         "lines": _arr(_obj({"item_id": UUID, "qty": DECIMAL, "lot_id": UUID,
                                             "serial_ids": _arr(UUID)}))})
WORK_ORDER_COMPLETE = _obj({"qty": DECIMAL, "transaction_date": DATE, "location_id": UUID,
                            "bin_id": UUID, "lot_id": UUID, "serial_ids": _arr(UUID)})

# ------------------------------------------------------------------- quality ------------------
QUALITY_SPEC = _obj({"code": TEXT, "name": TEXT, "item_id": UUID, "item_category_id": UUID,
                     "effective_from": DATE, "effective_to": DATE, "notes": TEXT,
                     "lines": _lines(_obj({"characteristic": TEXT, "result_type": TEXT,
                                           "min_value": DECIMAL, "max_value": DECIMAL,
                                           "is_internal": BOOL, "sequence": INT}),
                                    ("characteristic", "result_type"))},
                    required=("code", "item_id", "lines"))
QUALITY_PLAN = _obj({"code": TEXT, "name": TEXT, "item_id": UUID, "quality_spec_id": UUID,
                     "inspection_type": {"type": "string", "enum": ["INCOMING", "IN_PROCESS", "FINAL"]},
                     "sampling_method": {"type": "string", "enum": ["FULL", "SAMPLE"]},
                     "sample_size": INT, "is_active": BOOL},
                    required=("code", "item_id", "quality_spec_id"))
QUALITY_PLAN_UPDATE = _obj({"name": TEXT, "is_active": BOOL, "sample_size": INT})
QUALITY_INSPECTION = _obj({"inspection_plan_id": UUID, "item_id": UUID, "location_id": UUID,
                           "source_entity_type": TEXT, "source_entity_id": UUID,
                           "inspection_queue_id": UUID, "trigger_id": UUID},
                          required=("inspection_plan_id", "item_id"))
QUALITY_RESULTS = _obj({"results": _arr(_obj({"sequence": INT, "characteristic": TEXT,
                                              "result_type": TEXT, "measured_value_number": DECIMAL,
                                              "measured_value_text": TEXT, "is_pass": BOOL}))})
QUALITY_COMPLETE = _obj({"notes": TEXT,
                         "disposition": {"type": "string", "enum": ["ACCEPT", "REJECT", "REWORK", "SCRAP", "HOLD"]},
                         "results": _arr({"type": "object", "additionalProperties": True})})

# ------------------------------------------------------------------- consignment --------------
CONSIGNMENT_AGREEMENT = _obj({"vendor_id": UUID, "item_id": UUID, "location_id": UUID,
                              "start_date": DATE, "end_date": DATE, "currency_code": TEXT,
                              "unit_cost": MONEY, "memo": TEXT},
                             required=("vendor_id", "item_id", "location_id", "start_date"))
CONSIGNMENT_AGREEMENT_UPDATE = _obj({"end_date": DATE, "unit_cost": MONEY, "memo": TEXT})
CONSIGNMENT_TRANSFER = _obj({"from_location_id": UUID, "to_location_id": UUID, "transaction_date": DATE,
                             "memo": TEXT,
                             "lines": _lines(_obj({"item_id": UUID, "qty": DECIMAL, "lot_id": UUID,
                                                   "serial_ids": _arr(UUID)}), ("item_id", "qty"))},
                            required=("from_location_id", "to_location_id", "lines"))

# ------------------------------------------------------------------- extensions -------------
WARRANTY_REGISTRATION = _obj({"item_id": UUID, "serial_id": UUID, "warranty_term_id": UUID,
                              "customer_ref": TEXT, "start_date": DATE, "end_date": DATE,
                              "sold_at": DATETIME, "notes": TEXT},
                             required=("item_id", "serial_id", "start_date", "end_date"))
UPC_REGISTRY = _obj({"item_id": UUID, "upc_code": TEXT, "notes": TEXT}, required=("item_id",))

# ------------------------------------------------------------------- planning ----------------
ALLOCATION_RUN = _obj({"horizon_date": DATE, "strategy_id": UUID, "item_ids": _arr(UUID),
                       "location_ids": _arr(UUID), "memo": TEXT})


def _objempty() -> dict[str, Any]:
    return _obj({})


# ------------------------------------------------------------------- rules -------------------
class Rule:
    __slots__ = ("base", "create", "update", "actions", "default_action", "create_required")

    def __init__(self, base: str, *, create: dict[str, Any] | None = None, update: dict[str, Any] | None = None,
                 actions: dict[str, dict[str, Any]] | None = None, default_action: dict[str, Any] | None = None,
                 create_required: bool = True):
        self.base = base
        self.create = create
        self.update = update
        self.actions = actions or {}
        self.default_action = default_action if default_action is not None else ACTION
        self.create_required = create_required


RULES: tuple[Rule, ...] = (
    Rule("/api/v1/items", create=ITEM, update=ITEM_UPDATE, actions={"inactivate": REASON}),
    Rule("/api/v1/locations", create=LOCATION, update=LOCATION_UPDATE,
         actions={"activate": ACTION, "inactivate": REASON}),
    Rule("/api/v1/bins", create=BIN, update=BIN_UPDATE,
         actions={"activate": ACTION, "inactivate": REASON}),
    Rule("/api/v1/inventory-statuses", create=STATUS, update=STATUS_UPDATE,
         actions={"activate": ACTION, "inactivate": REASON}),
    Rule("/api/v1/item-categories", create=CATEGORY, update=CATEGORY_UPDATE),
    Rule("/api/v1/uoms", create=UOM, update=UOM_UPDATE),
    Rule("/api/v1/uom-conversions", create=UOM_CONVERSION),
    Rule("/api/v1/price-levels", create=PRICE_LEVEL, update=PRICE_LEVEL_UPDATE),
    Rule("/api/v1/item-prices", create=ITEM_PRICE, update=ITEM_PRICE_UPDATE),
    Rule("/api/v1/inventory-adjustments", create=ADJUSTMENT, update=ADJUSTMENT_UPDATE,
         actions={"reverse": REASON, "void": REASON}),
    Rule("/api/v1/inventory-transfers", create=TRANSFER, update=TRANSFER_UPDATE,
         actions={"reverse": REASON, "void": REASON}),
    Rule("/api/v1/inventory-counts", create=COUNT, actions={"recount": ACTION, "reject": REASON},
         default_action=ACTION),
    Rule("/api/v1/count-plans", update=COUNT_PLAN_UPDATE, default_action=ACTION),
    Rule("/api/v1/vendors", create=VENDOR, update=VENDOR_UPDATE, actions={"inactivate": REASON}),
    Rule("/api/v1/purchase-orders", create=PO, update=PO_UPDATE),
    Rule("/api/v1/purchase-requests", create=REQUEST),
    Rule("/api/v1/purchase-requisitions", create=REQUISITION, update=REQUISITION_UPDATE),
    Rule("/api/v1/rfqs", create=RFQ),
    Rule("/api/v1/vendor-contracts", create=CONTRACT, update=CONTRACT_UPDATE),
    Rule("/api/v1/item-receipts", create=RECEIPT, update=RECEIPT_UPDATE),
    Rule("/api/v1/vendor-bills", create=BILL, update=BILL_UPDATE,
         actions={"match": _obj({"override": BOOL})}),
    Rule("/api/v1/vendor-credits", create=CREDIT),
    Rule("/api/v1/vendor-returns", create=RETURN),
    Rule("/api/v1/warehouses", create=WAREHOUSE, update=WAREHOUSE_UPDATE),
    Rule("/api/v1/inbound-shipments", create=INBOUND),
    Rule("/api/v1/receiving-orders", create=RECEIVING_ORDER,
         actions={"complete": _obj({"lines": _lines(RECEIVING_COMPLETE_LINE)}), "start": ACTION,
                  "cancel": REASON}),
    Rule("/api/v1/putaway-tasks", actions={"assign": PUTAWAY_ASSIGN, "start": ACTION,
                                           "complete": ACTION, "cancel": REASON}),
    Rule("/api/v1/waves", create=WAVE),
    Rule("/api/v1/pick-tasks", actions={"assign": PICK_ASSIGN, "start": ACTION,
                                        "complete": PICK_COMPLETE, "reverse": REASON}),
    Rule("/api/v1/pack-tasks", create=PACK_TASK, actions={"complete": PACK_COMPLETE}),
    Rule("/api/v1/shipments", create=SHIPMENT, actions={"confirm": ACTION, "ship": ACTION, "cancel": REASON}),
    Rule("/api/v1/assemblies", update=ASSEMBLY_UPDATE),
    Rule("/api/v1/boms", update=BOM_UPDATE),
    Rule("/api/v1/routings", create=ROUTING, update=ROUTING_UPDATE),
    Rule("/api/v1/work-centers", create=WORK_CENTER, update=WORK_CENTER_UPDATE),
    Rule("/api/v1/work-orders", update=WORK_ORDER_UPDATE,
         actions={"issue": WORK_ORDER_ISSUE, "complete": WORK_ORDER_COMPLETE}),
    Rule("/api/v1/quality/specs", create=QUALITY_SPEC),
    Rule("/api/v1/quality/inspection-plans", create=QUALITY_PLAN, update=QUALITY_PLAN_UPDATE),
    Rule("/api/v1/quality/inspections", create=QUALITY_INSPECTION,
         actions={"start": ACTION, "complete": QUALITY_COMPLETE, "cancel": REASON}),
    Rule("/api/v1/consignment-agreements", create=CONSIGNMENT_AGREEMENT, update=CONSIGNMENT_AGREEMENT_UPDATE,
         actions={"activate": ACTION}),
    Rule("/api/v1/consignment-transfers", create=CONSIGNMENT_TRANSFER),
    Rule("/api/v1/warranty/registrations", create=WARRANTY_REGISTRATION,
         actions={"void": REASON}),
    Rule("/api/v1/upc-registry", create=UPC_REGISTRY),
    Rule("/api/v1/allocation-runs", create=ALLOCATION_RUN),
)


def _normalise_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def schema_for_operation(method: str, path: str) -> dict[str, Any] | None:
    method = method.upper()
    normalized = _normalise_path(path)
    for rule in RULES:
        base = rule.base
        if normalized == base:
            if method == "POST":
                return rule.create
            return None
        if re.fullmatch(re.escape(base) + r"/\{\}", normalized):
            if method in {"PATCH", "PUT"}:
                return rule.update
            return None
        match = re.fullmatch(re.escape(base) + r"/\{\}/([A-Za-z0-9_.-]+)", normalized)
        if match and method == "POST":
            action = match.group(1)
            return rule.actions.get(action, rule.default_action)
    return None


def apply_request_schemas(spec: dict[str, Any]) -> int:
    """Inject registered request-body schemas into an OpenAPI document in place."""
    injected = 0
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method not in {"post", "put", "patch"}:
                continue
            schema = schema_for_operation(method, path)
            if schema is None:
                continue
            request_body = operation.setdefault("requestBody", {})
            request_body["required"] = bool(schema.get("required"))
            content = request_body.setdefault("content", {})
            content[JSON_SCHEMA_KEY] = {"schema": schema}
            operation["x-schema-source"] = "request_schemas"
            injected += 1
    return injected


__all__ = ["RULES", "apply_request_schemas", "schema_for_operation"]
