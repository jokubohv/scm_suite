"""FS-15 reporting catalogue: 21 views + the minimal reporting API surface (reporting.readmodels@1.0.0)."""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.reporting.views import VIEW_NAMES, VIEWS, ViewSpec

REPORT_READ = "reporting.read"
REPORT_COST = "reporting.cost.view"
REPORT_EXPORT = "reporting.export"
REPORT_ADMIN = "reporting.admin"

#: allow-listed filter columns per view (all values arrive as strings from HTTP and are cast by type)
FILTER_TYPES: dict[str, str] = {
    "item_id": "uuid", "location_id": "uuid", "inventory_status_id": "uuid", "status_id": "uuid",
    "lot_id": "uuid", "serial_id": "uuid", "bin_id": "uuid", "transaction_id": "uuid",
    "source_doc_id": "uuid", "commitment_id": "uuid", "work_order_id": "uuid",
    "inspection_id": "uuid", "serial_number": "text", "lot_number": "text",
    "external_ref": "text", "ownership": "text", "costing_method": "text", "method": "text",
    "status": "text", "action_type": "text", "disposition": "text", "source_entity_type": "text",
    "event": "text", "category_id": "uuid", "vendor_id": "uuid", "agreement_id": "uuid",
    "purchase_order_id": "uuid", "assembly_item_id": "uuid", "plan_id": "uuid", "item_number": "text",
    "date_from": "date", "date_to": "date", "from": "date", "to": "date",
    "required_by_to": "date", "required_by_from": "date", "expiry_before": "date",
    "occurred_from": "timestamptz", "occurred_to": "timestamptz",
    "layer_date_from": "date", "layer_date_to": "date", "period_from": "date", "period_to": "date",
    "bucket_from": "date", "bucket_to": "date", "as_of": "date", "level_mode": "text",
    "basis": "text", "policy_version": "text", "fields": "text", "cursor": "text",
    "include_estimated": "bool", "include_expired": "bool", "shortage_only": "bool",
}

# physical column name on the view for each public filter name
FILTER_COLUMNS: dict[str, str] = {
    "item_id": "item_id", "location_id": "location_id", "inventory_status_id": "inventory_status_id",
    "status_id": "inventory_status_id", "lot_id": "lot_id", "serial_id": "serial_id", "bin_id": "bin_id",
    "transaction_id": "transaction_id", "source_doc_id": "source_doc_id",
    "commitment_id": "commitment_id", "work_order_id": "work_order_id", "inspection_id": "inspection_id",
    "serial_number": "serial_number", "lot_number": "lot_number", "external_ref": "external_ref",
    "ownership": "ownership", "costing_method": "costing_method", "method": "method", "status": "status",
    "action_type": "action_type", "disposition": "disposition", "source_entity_type": "source_entity_type",
    "event": "event", "category_id": "category_id", "vendor_id": "vendor_id", "agreement_id": "agreement_id",
    "purchase_order_id": "purchase_order_id", "assembly_item_id": "assembly_item_id", "plan_id": "plan_id",
    "item_number": "item_number",
    # date filters map to the most useful view column (bound semantics are applied by the service)
    "date_from": "transaction_date", "date_to": "transaction_date", "from": "transaction_date",
    "to": "transaction_date", "required_by_to": "required_by_date", "required_by_from": "required_by_date",
    "expiry_before": "expiry_date", "occurred_from": "occurred_at", "occurred_to": "occurred_at",
    "layer_date_from": "layer_date", "layer_date_to": "layer_date",
    "period_from": "period_start", "period_to": "period_start",
    "bucket_from": "bucket_date", "bucket_to": "bucket_date", "as_of": "as_of",
    "level_mode": "level_mode", "basis": "basis", "policy_version": "policy_version",
    "transaction_type": "transaction_type", "source_doc_type": "source_doc_type",
    "include_estimated": "estimated_negative", "include_expired": "expiry_date",
    "shortage_only": "shortage_qty",
}


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    path: str
    view_name: str
    scope: str
    filters: tuple[str, ...]
    cost: bool = False
    export: bool = False
    title: str = ""


def _e(path: str, view_name: str, scope: str, filters: tuple[str, ...], *, cost: bool = False,
       export: bool = False, title: str = "") -> EndpointSpec:
    return EndpointSpec(path, view_name, scope, filters, cost, export, title)


ENDPOINTS: tuple[EndpointSpec, ...] = (
    _e("/reports/stock-ledger", "v_stock_ledger", REPORT_READ,
       ("item_id", "location_id", "status_id", "ownership", "lot_id", "serial_id", "transaction_type",
        "date_from", "date_to", "source_doc_type"), cost=True, title="Stock ledger"),
    _e("/reports/inventory-valuation", "v_inventory_valuation", REPORT_COST,
       ("item_id", "location_id", "costing_method", "as_of"), cost=True, title="Inventory valuation summary"),
    _e("/reports/inventory-valuation-detail", "v_inventory_valuation_detail", REPORT_COST,
       ("item_id", "location_id", "layer_date_from", "layer_date_to"), cost=True, title="Valuation detail by layer"),
    _e("/reports/inventory-snapshot", "v_inventory_snapshot", REPORT_READ,
       ("item_id", "location_id", "status_id", "category_id", "as_of"), title="Current inventory snapshot"),
    _e("/reports/negative-inventory", "v_negative_inventory", REPORT_COST,
       ("item_id", "location_id", "include_estimated"), cost=True, title="Negative inventory"),
    _e("/reports/backorders", "v_backorders", REPORT_READ,
       ("item_id", "location_id", "external_ref", "required_by_to"), title="Back orders"),
    _e("/reports/pending-fulfillment", "v_pending_fulfillment", REPORT_READ,
       ("item_id", "location_id", "external_ref", "required_by_from", "required_by_to"),
       title="Pending fulfilment"),
    _e("/reports/inventory-activity", "v_inventory_activity_detail", REPORT_READ,
       ("item_id", "location_id", "transaction_id", "bin_id", "lot_id", "serial_id", "date_from", "date_to"),
       cost=True, title="Inventory activity detail"),
    _e("/reports/turnover", "v_inventory_turnover_inputs", REPORT_COST,
       ("item_id", "location_id", "period_from", "period_to"), cost=True, title="Turnover inputs"),
    _e("/reports/profitability", "v_inventory_profitability_inputs", REPORT_COST,
       ("item_id", "category_id", "period_from", "period_to"), cost=True, title="Profitability inputs"),
    _e("/reports/lot-expiry", "v_lot_expiry", REPORT_READ,
       ("item_id", "location_id", "expiry_before", "include_expired"), title="Lot expiry"),
    _e("/reports/serial-history", "v_serial_history", REPORT_READ,
       ("serial_id", "item_id", "serial_number", "event", "occurred_from", "occurred_to"),
       title="Serial history"),
    _e("/reports/consigned-balance", "v_consigned_balance", REPORT_READ,
       ("vendor_id", "agreement_id", "item_id", "location_id"), cost=True, title="Consigned balance"),
    _e("/reports/quality-inspections", "v_quality_inspection_status", REPORT_READ,
       ("item_id", "location_id", "status", "disposition", "source_entity_type"), title="Quality inspection status"),
    _e("/reports/purchase-history", "v_purchase_history", REPORT_READ,
       ("vendor_id", "item_id", "date_from", "date_to", "purchase_order_id"), title="Purchase history"),
    _e("/reports/manufacturing-workbench", "v_manufacturing_workbench", REPORT_READ,
       ("location_id", "assembly_item_id", "status", "shortage_only"), title="Manufacturing workbench"),
    _e("/reports/planning-workbench", "v_planning_workbench", REPORT_READ,
       ("plan_id", "item_id", "location_id", "action_type", "status"), title="Planning workbench"),
    _e("/reports/atp-workbench", "v_atp_current", REPORT_READ,
       ("item_id", "location_id", "bucket_from", "bucket_to"), title="ATP workbench"),
    _e("/inventory/balances/search", "v_inventory_balance", REPORT_READ,
       ("item_id", "location_id", "status_id", "bin_id", "as_of"), title="Balance search"),
    _e("/inventory/details/search", "v_inventory_activity_detail", REPORT_READ,
       ("item_id", "location_id", "bin_id", "lot_id", "serial_id", "status_id", "date_from", "date_to"),
       cost=True, title="Inventory detail search"),
    _e("/reports/open-commitments", "v_open_commitments", REPORT_READ,
       ("item_id", "location_id", "external_ref", "required_by_to"), title="Open commitments"),
    _e("/reports/available-to-build", "v_available_to_build", REPORT_READ,
       ("assembly_item_id", "location_id", "level_mode", "basis", "as_of", "policy_version"),
       title="Available to build"),
    _e("/reports/{report_name}/export", "", REPORT_EXPORT, (), export=True, title="Export report"),
)

VIEWS_BY_NAME: dict[str, ViewSpec] = {view.name: view for view in VIEWS}
ENDPOINTS_BY_PATH: dict[str, EndpointSpec] = {spec.path: spec for spec in ENDPOINTS if not spec.export}

#: report catalogue aliases accepted by the export endpoint
REPORT_ALIASES: dict[str, str] = {
    "stock-ledger": "v_stock_ledger", "inventory-valuation": "v_inventory_valuation",
    "inventory-valuation-detail": "v_inventory_valuation_detail", "inventory-snapshot": "v_inventory_snapshot",
    "negative-inventory": "v_negative_inventory", "backorders": "v_backorders",
    "pending-fulfillment": "v_pending_fulfillment", "inventory-activity": "v_inventory_activity_detail",
    "turnover": "v_inventory_turnover_inputs", "profitability": "v_inventory_profitability_inputs",
    "lot-expiry": "v_lot_expiry", "serial-history": "v_serial_history",
    "consigned-balance": "v_consigned_balance", "quality-inspections": "v_quality_inspection_status",
    "purchase-history": "v_purchase_history", "manufacturing-workbench": "v_manufacturing_workbench",
    "planning-workbench": "v_planning_workbench", "atp-workbench": "v_atp_current",
    "open-commitments": "v_open_commitments", "available-to-build": "v_available_to_build",
    "balances-search": "v_inventory_balance", "details-search": "v_inventory_activity_detail",
}

__all__ = ["ENDPOINTS", "ENDPOINTS_BY_PATH", "FILTER_COLUMNS", "FILTER_TYPES", "REPORT_ALIASES",
           "REPORT_COST", "REPORT_EXPORT", "REPORT_READ", "REPORT_ADMIN", "VIEWS", "VIEW_NAMES",
           "VIEWS_BY_NAME", "EndpointSpec"]
