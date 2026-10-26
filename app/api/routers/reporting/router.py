"""FS-15 Reporting API (read-only, `/api/v1`, catalogued scopes, tenant + data-scope filtered).

Thin transport over :class:`app.modules.reporting.service.ReportService`. Every endpoint is GET-only,
returns decimal strings/UTC timestamps, a stable ``next_cursor`` and freshness metadata. Cost/margin
endpoints require ``reporting.cost.view``; exports require ``reporting.export``. Router registration is
integrator-owned; this module exports ``router`` and ``ROUTE_SCOPES`` only.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas import reporting as reporting_dto
from app.modules.reporting.catalogue import (
    ENDPOINTS,
    REPORT_ALIASES,
    REPORT_COST,
    REPORT_EXPORT,
    REPORT_READ,
)
from app.modules.reporting.service import ReportingError, ReportService

router = APIRouter(prefix="/api/v1", tags=["reporting"])


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, ReportingError):
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "details": exc.details}
        ) from exc
    raise exc


def _service(db: DbDep, principal: PrincipalDep) -> ReportService:
    return ReportService(db, tenant_id=principal.tenant_id, app_user_id=principal.app_user_id)


ServiceDep = Annotated[ReportService, Depends(_service)]
Read = Annotated[object, Depends(require_scope(REPORT_READ))]
Cost = Annotated[object, Depends(require_scope(REPORT_COST))]
Exporter = Annotated[object, Depends(require_scope(REPORT_EXPORT))]

ROUTE_SCOPES: dict[tuple[str, str], str] = {("GET", "/api/v1/reports"): REPORT_READ}
for _spec in ENDPOINTS:
    if _spec.export:
        ROUTE_SCOPES[("GET", "/api/v1" + "/reports/{report}/export")] = REPORT_EXPORT
    else:
        ROUTE_SCOPES[("GET", "/api/v1" + _spec.path)] = _spec.scope


async def _page(
    service: ReportService, view: str, filters: dict[str, Any], cursor: str | None, limit: int, fields: str | None
) -> dict[str, Any]:
    from app.modules.reporting.catalogue import FILTER_TYPES

    clean = {
        key: value
        for key, value in filters.items()
        if key in FILTER_TYPES and key not in {"cursor", "limit", "fields"} and value is not None and value != ""
    }
    try:
        page = await service.query(view, filters=clean, cursor=cursor, limit=limit, fields=fields)
    except Exception as exc:  # noqa: BLE001 - normalised to problem+json
        _raise(exc)
    return {"items": page.items, "next_cursor": page.next_cursor, "freshness": page.freshness}


@router.get("/reports", response_model=reporting_dto.ReportCatalogue)
async def report_catalogue(_: Read, service: ServiceDep) -> dict[str, Any]:
    return await service.catalogue()


@router.get("/reports/stock-ledger", response_model=reporting_dto.ReportPage)
async def stock_ledger(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    status_id: str | None = None,
    ownership: str | None = None,
    lot_id: str | None = None,
    serial_id: str | None = None,
    transaction_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source_doc_type: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_stock_ledger", locals(), cursor, limit, fields)


@router.get("/reports/inventory-valuation", response_model=reporting_dto.ReportPage)
async def inventory_valuation(
    service: ServiceDep,
    _: Cost,
    item_id: str | None = None,
    location_id: str | None = None,
    costing_method: str | None = None,
    as_of: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_valuation", locals(), cursor, limit, fields)


@router.get("/reports/inventory-valuation-detail", response_model=reporting_dto.ReportPage)
async def inventory_valuation_detail(
    service: ServiceDep,
    _: Cost,
    item_id: str | None = None,
    location_id: str | None = None,
    layer_date_from: str | None = None,
    layer_date_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_valuation_detail", locals(), cursor, limit, fields)


@router.get("/reports/inventory-snapshot", response_model=reporting_dto.ReportPage)
async def inventory_snapshot(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    status_id: str | None = None,
    category_id: str | None = None,
    as_of: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_snapshot", locals(), cursor, limit, fields)


@router.get("/reports/negative-inventory", response_model=reporting_dto.ReportPage)
async def negative_inventory(
    service: ServiceDep,
    _: Cost,
    item_id: str | None = None,
    location_id: str | None = None,
    include_estimated: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_negative_inventory", locals(), cursor, limit, fields)


@router.get("/reports/backorders", response_model=reporting_dto.ReportPage)
async def backorders(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    external_ref: str | None = None,
    required_by_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_backorders", locals(), cursor, limit, fields)


@router.get("/reports/pending-fulfillment", response_model=reporting_dto.ReportPage)
async def pending_fulfillment(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    external_ref: str | None = None,
    required_by_from: str | None = None,
    required_by_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_pending_fulfillment", locals(), cursor, limit, fields)


@router.get("/reports/inventory-activity", response_model=reporting_dto.ReportPage)
async def inventory_activity(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    transaction_id: str | None = None,
    bin_id: str | None = None,
    lot_id: str | None = None,
    serial_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_activity_detail", locals(), cursor, limit, fields)


@router.get("/reports/turnover", response_model=reporting_dto.ReportPage)
async def turnover(
    service: ServiceDep,
    _: Cost,
    item_id: str | None = None,
    location_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_turnover_inputs", locals(), cursor, limit, fields)


@router.get("/reports/profitability", response_model=reporting_dto.ReportPage)
async def profitability(
    service: ServiceDep,
    _: Cost,
    item_id: str | None = None,
    category_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_profitability_inputs", locals(), cursor, limit, fields)


@router.get("/reports/lot-expiry", response_model=reporting_dto.ReportPage)
async def lot_expiry(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    expiry_before: str | None = None,
    include_expired: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_lot_expiry", locals(), cursor, limit, fields)


@router.get("/reports/serial-history", response_model=reporting_dto.ReportPage)
async def serial_history(
    service: ServiceDep,
    _: Read,
    serial_id: str | None = None,
    item_id: str | None = None,
    serial_number: str | None = None,
    event: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_serial_history", locals(), cursor, limit, fields)


@router.get("/reports/consigned-balance", response_model=reporting_dto.ReportPage)
async def consigned_balance(
    service: ServiceDep,
    _: Read,
    vendor_id: str | None = None,
    agreement_id: str | None = None,
    item_id: str | None = None,
    location_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_consigned_balance", locals(), cursor, limit, fields)


@router.get("/reports/quality-inspections", response_model=reporting_dto.ReportPage)
async def quality_inspections(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    status: str | None = None,
    disposition: str | None = None,
    source_entity_type: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_quality_inspection_status", locals(), cursor, limit, fields)


@router.get("/reports/purchase-history", response_model=reporting_dto.ReportPage)
async def purchase_history(
    service: ServiceDep,
    _: Read,
    vendor_id: str | None = None,
    item_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    purchase_order_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_purchase_history", locals(), cursor, limit, fields)


@router.get("/reports/manufacturing-workbench", response_model=reporting_dto.ReportPage)
async def manufacturing_workbench(
    service: ServiceDep,
    _: Read,
    location_id: str | None = None,
    assembly_item_id: str | None = None,
    status: str | None = None,
    shortage_only: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_manufacturing_workbench", locals(), cursor, limit, fields)


@router.get("/reports/planning-workbench", response_model=reporting_dto.ReportPage)
async def planning_workbench(
    service: ServiceDep,
    _: Read,
    plan_id: str | None = None,
    item_id: str | None = None,
    location_id: str | None = None,
    action_type: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_planning_workbench", locals(), cursor, limit, fields)


@router.get("/reports/atp-workbench", response_model=reporting_dto.ReportPage)
async def atp_workbench(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    bucket_from: str | None = None,
    bucket_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_atp_current", locals(), cursor, limit, fields)


@router.get("/inventory/balances/search", response_model=reporting_dto.ReportPage)
async def balance_search(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    status_id: str | None = None,
    bin_id: str | None = None,
    as_of: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_balance", locals(), cursor, limit, fields)


@router.get("/inventory/details/search", response_model=reporting_dto.ReportPage)
async def detail_search(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    bin_id: str | None = None,
    lot_id: str | None = None,
    serial_id: str | None = None,
    status_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_inventory_activity_detail", locals(), cursor, limit, fields)


@router.get("/reports/open-commitments", response_model=reporting_dto.ReportPage)
async def open_commitments(
    service: ServiceDep,
    _: Read,
    item_id: str | None = None,
    location_id: str | None = None,
    external_ref: str | None = None,
    required_by_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_open_commitments", locals(), cursor, limit, fields)


@router.get("/reports/available-to-build", response_model=reporting_dto.ReportPage)
async def available_to_build(
    service: ServiceDep,
    _: Read,
    assembly_item_id: str | None = None,
    location_id: str | None = None,
    level_mode: str | None = None,
    basis: str | None = None,
    as_of: str | None = None,
    policy_version: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    fields: str | None = None,
) -> dict[str, Any]:
    return await _page(service, "v_available_to_build", locals(), cursor, limit, fields)


@router.get("/reports/{report}/export", dependencies=[Depends(require_scope(REPORT_EXPORT))])
async def export_report(
    report: str,
    service: ServiceDep,
    _: Exporter,
    format: str = Query("json", pattern="^(json|csv)$"),
    cursor: str | None = None,
    limit: int = Query(500, ge=1, le=500),
    fields: str | None = None,
) -> Response:
    if report not in REPORT_ALIASES:
        raise HTTPException(
            status_code=404, detail={"code": "REPORT_NOT_FOUND", "message": f"unknown report {report!r}"}
        )
    try:
        body, media_type = await service.export_view(report, cursor=cursor, limit=limit, format=format, fields=fields)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return Response(content=body, media_type=media_type)


__all__ = ["ROUTE_SCOPES", "router"]
