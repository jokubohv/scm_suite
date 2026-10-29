"""FS-01 Item Master + UoM API surface (FS-01 §6), tenant-scoped and deny-by-default scoped.

The integrator registers ``router`` in ``app/api/main.py``; this module never edits main/registry/deps.
Endpoints delegate to :class:`app.modules.item_master.service.ItemMasterService`; mutations that must be
idempotent require an ``Idempotency-Key`` header (item creation uses the durable FS-00 store).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES, ActionResult
from app.api.schemas.items import (
    AliasResolveOut,
    AvailabilitySettingsOut,
    BarcodeResolveOut,
    ItemAccountingOut,
    ItemAccountingSettingsUpdate,
    ItemAliasCreate,
    ItemAliasOut,
    ItemAliasPage,
    ItemBarcodeCreate,
    ItemBarcodeOut,
    ItemBarcodePage,
    ItemBarcodeUpdate,
    ItemCategoryCreate,
    ItemCategoryMove,
    ItemCategoryOut,
    ItemCategoryPage,
    ItemCategoryUpdate,
    ItemConvertToInventoryRequest,
    ItemCreate,
    ItemCustomFieldsOut,
    ItemCustomFieldsUpdate,
    ItemInactivateRequest,
    ItemInventoryOut,
    ItemInventorySettingsUpdate,
    ItemKitMemberCreate,
    ItemKitMemberUpdate,
    ItemLocationOut,
    ItemLocationPage,
    ItemLocationsReplace,
    ItemLocationUpdate,
    ItemMatrixOptionCreate,
    ItemOut,
    ItemPage,
    ItemPurchasingOut,
    ItemPurchasingSettingsUpdate,
    ItemRelatedCreate,
    ItemSalesOut,
    ItemSalesSettingsUpdate,
    ItemTypeOut,
    ItemTypePage,
    ItemUomAssignmentsOut,
    ItemUomAssignmentsReplace,
    ItemUpdate,
    KitMembersOut,
    MatrixChildLinkRequest,
    MatrixChildPage,
    MatrixGenerateOut,
    MatrixGenerateRequest,
    MatrixLinkOut,
    MatrixOptionOut,
    MatrixOptionsOut,
    MatrixOptionValuesReplace,
    PlanningValuesApply,
    PlanningValuesResultOut,
    RelatedItemOut,
    RelatedItemPage,
    TypeMigrationOut,
    UomConversionCreate,
    UomConversionOut,
    UomConversionPage,
    UomCreate,
    UomOut,
    UomPage,
    UomResolutionOut,
    UomResolveRequest,
    UomTypeCreate,
    UomTypeOut,
    UomTypePage,
    UomTypeUpdate,
    UomUpdate,
)
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.security.idempotency import IdempotencyOutcome
from app.modules.item_master.errors import IdempotencyKeyRequired, item_error
from app.modules.item_master.service import ItemMasterService

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["item-master"], route_class=SCMAPIRoute)

READ = "items.read"
CREATE = "items.create"
UPDATE = "items.update"
INACTIVATE = "items.inactivate"
UOM = "items.uom.manage"
LOCATION = "items.location.manage"
MATRIX = "items.matrix.manage"
BARCODE = "items.barcode.manage"
CATEGORY = "items.category.manage"
PLANNING = "items.planning.manage"


def _svc(db: AsyncSession, principal: PrincipalDep) -> ItemMasterService:
    return ItemMasterService(db, principal.tenant_id, app_user_id=principal.app_user_id)


ReadDep = Annotated[PrincipalDep, Depends(require_scope(READ))]
CreateDep = Annotated[PrincipalDep, Depends(require_scope(CREATE))]
UpdateDep = Annotated[PrincipalDep, Depends(require_scope(UPDATE))]
InactivateDep = Annotated[PrincipalDep, Depends(require_scope(INACTIVATE))]
UomDep = Annotated[PrincipalDep, Depends(require_scope(UOM))]
LocationDep = Annotated[PrincipalDep, Depends(require_scope(LOCATION))]
MatrixDep = Annotated[PrincipalDep, Depends(require_scope(MATRIX))]
BarcodeDep = Annotated[PrincipalDep, Depends(require_scope(BARCODE))]
CategoryDep = Annotated[PrincipalDep, Depends(require_scope(CATEGORY))]
PlanningDep = Annotated[PrincipalDep, Depends(require_scope(PLANNING))]


async def idempotency_key(
    x_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if not x_idempotency_key:
        raise IdempotencyKeyRequired("Idempotency-Key header is required")
    return x_idempotency_key


IdempotencyDep = Annotated[str | None, Depends(idempotency_key)]


def _version(if_match: str | None) -> int | None:
    if not if_match:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise item_error("If-Match must be an integer version", code="PRECONDITION_FAILED", http_status=412) from exc


VersionHeader = Annotated[str | None, Header(alias="If-Match")]


# --------------------------------------------------------------------------- taxonomy ----------
@router.get("/item-types", response_model=ItemTypePage)
async def list_item_types(
    principal: ReadDep,
    db: DbDep,
    can_stock: bool | None = None,
    is_charge_only: bool | None = None,
) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_item_types(can_stock=can_stock, charge_only=is_charge_only)}


@router.get("/item-types/{code}", response_model=ItemTypeOut)
async def get_item_type(code: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_item_type(code)


# --------------------------------------------------------------------------- categories --------
@router.get("/item-categories", response_model=ItemCategoryPage)
async def list_categories(
    principal: ReadDep,
    db: DbDep,
    parent_id: str | None = None,
    include_inactive: bool = False,
) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_categories(parent_id=parent_id, include_inactive=include_inactive)}


@router.post("/item-categories", status_code=201, response_model=ItemCategoryOut)
async def create_category(
    body: ItemCategoryCreate, principal: CategoryDep, db: DbDep, _: IdempotencyDep = None
) -> dict[str, Any]:
    return await _svc(db, principal).create_category(body.model_dump(exclude_unset=True))


@router.get("/item-categories/{category_id}", response_model=ItemCategoryOut)
async def get_category(category_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_category(category_id)


@router.patch("/item-categories/{category_id}", response_model=ItemCategoryOut)
async def patch_category(
    category_id: str, body: ItemCategoryUpdate, principal: CategoryDep, db: DbDep, if_match: VersionHeader = None
) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).update_category(category_id, body.model_dump(exclude_unset=True))


@router.post("/item-categories/{category_id}/move", response_model=ActionResult)
async def move_category(
    category_id: str, body: ItemCategoryMove, principal: CategoryDep, db: DbDep, _: IdempotencyDep = None
) -> dict[str, Any]:
    return await _svc(db, principal).move_category(category_id, str(body.new_parent_id) if body.new_parent_id else None)


# --------------------------------------------------------------------------- UoM types/units --
@router.get("/uom-types", response_model=UomTypePage)
async def list_uom_types(principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_uom_types()}


@router.post("/uom-types", status_code=201, response_model=UomTypeOut)
async def create_uom_type(body: UomTypeCreate, principal: UomDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).create_uom_type(body.model_dump(exclude_unset=True))


@router.patch("/uom-types/{uom_type_id}", response_model=UomTypeOut)
async def patch_uom_type(
    uom_type_id: str, body: UomTypeUpdate, principal: UomDep, db: DbDep, if_match: VersionHeader = None
) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).update_uom_type(uom_type_id, body.model_dump(exclude_unset=True))


@router.get("/uoms", response_model=UomPage)
async def list_uoms(
    principal: ReadDep, db: DbDep, uom_type_id: str | None = None, is_active: bool | None = None
) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_uoms(uom_type_id=uom_type_id, is_active=is_active)}


@router.post("/uoms", status_code=201, response_model=UomOut)
async def create_uom(body: UomCreate, principal: UomDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).create_uom(body.model_dump(exclude_unset=True))


@router.patch("/uoms/{uom_id}", response_model=UomOut)
async def patch_uom(uom_id: str, body: UomUpdate, principal: UomDep, db: DbDep, if_match: VersionHeader = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).update_uom(uom_id, body.model_dump(exclude_unset=True))


@router.get("/uom-conversions", response_model=UomConversionPage)
async def list_conversions(principal: ReadDep, db: DbDep, uom_type_id: str | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_conversions(uom_type_id=uom_type_id)}


@router.post("/uom-conversions", status_code=201, response_model=UomConversionOut)
async def create_conversion(body: UomConversionCreate, principal: UomDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).create_conversion(body.model_dump(exclude_unset=True))


@router.delete("/uom-conversions/{conversion_id}", status_code=204)
async def delete_conversion(conversion_id: str, principal: UomDep, db: DbDep) -> None:
    await _svc(db, principal).delete_conversion(conversion_id)


@router.post("/uom-conversions/resolve", response_model=UomResolutionOut)
async def resolve_conversion(body: UomResolveRequest, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).resolve_conversion(body.model_dump(exclude_unset=True))


# --------------------------------------------------------------------------- idempotency ------
async def _idempotent_mutation(
    *,
    db: AsyncSession,
    principal: PrincipalDep,
    endpoint_id: str,
    key: str,
    method: str,
    path: str,
    body: Any,
    status: int,
    runner: Any,
) -> Any:
    store = IdempotencyStore(db)
    fingerprint = build_fingerprint(
        method=method, path=path, tenant_id=principal.tenant_id, endpoint_id=endpoint_id, body=body
    )
    decision = await store.guard(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        request_fingerprint_hash=fingerprint,
        actor_id=principal.identity_id,
    )
    if decision.outcome is IdempotencyOutcome.REPLAY and decision.record is not None:
        return JSONResponse(status_code=decision.record.response_status or status, content=dict(decision.record.response_body or {}))
    if decision.outcome is IdempotencyOutcome.CONFLICT or decision.outcome is IdempotencyOutcome.IN_PROGRESS:
        decision.raise_for_conflict()
    result = await runner()
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        response_status=status,
        response_body=result,
        resource_id=result.get("id") or result.get("new_item_id") or result.get("item_id"),
    )
    return JSONResponse(status_code=status, content=result)


# --------------------------------------------------------------------------- items -------------
@router.post("/items", status_code=201, response_model=ItemOut)
async def create_item(
    body: ItemCreate,
    request: Request,
    principal: CreateDep,
    db: DbDep,
    key: IdempotencyDep = None,
) -> Any:
    assert key is not None  # dependency raises when the header is missing
    payload = body.model_dump(exclude_unset=True)
    service = _svc(db, principal)
    store = IdempotencyStore(db)
    fingerprint = build_fingerprint(
        method="POST", path="/api/v1/items", tenant_id=principal.tenant_id, endpoint_id="items.create", body=payload
    )
    decision = await store.guard(
        tenant_id=principal.tenant_id,
        endpoint_id="items.create",
        key=key,
        request_fingerprint_hash=fingerprint,
        actor_id=principal.identity_id,
    )
    if decision.outcome is IdempotencyOutcome.REPLAY and decision.record is not None:
        return dict(decision.record.response_body or {})
    if decision.outcome is IdempotencyOutcome.CONFLICT or decision.outcome is IdempotencyOutcome.IN_PROGRESS:
        decision.raise_for_conflict()
    result = await service.create_item(payload | {"actor_id": principal.identity_id})
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id="items.create",
        key=key,
        response_status=201,
        response_body=result,
        resource_id=result["id"],
    )
    return result


@router.get("/items", response_model=ItemPage)
async def list_items(
    principal: ReadDep,
    db: DbDep,
    q: str | None = None,
    item_type_code: str | None = None,
    is_active: bool | None = None,
    category_id: str | None = None,
    include_inactive: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await _svc(db, principal).list_items(
        q=q, item_type_code=item_type_code, is_active=is_active, category_id=category_id,
        include_inactive=include_inactive, cursor=cursor, limit=limit,
    )


@router.get("/items/resolve/barcode", response_model=BarcodeResolveOut)
async def resolve_barcode(barcode: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).resolve_barcode(barcode)


@router.get("/items/resolve/alias", response_model=AliasResolveOut)
async def resolve_alias(alias: str, principal: ReadDep, db: DbDep, alias_type: str = "WMS") -> dict[str, Any]:
    return await _svc(db, principal).resolve_alias(alias, alias_type)


@router.get("/items/{item_id}", response_model=ItemOut)
async def get_item(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_item(item_id)


@router.post("/items/{item_id}/convert-to-inventory", status_code=201, response_model=TypeMigrationOut)
async def convert_to_inventory(
    item_id: str,
    body: ItemConvertToInventoryRequest,
    principal: CreateDep,
    db: DbDep,
    key: IdempotencyDep = None,
) -> Any:
    assert key is not None
    payload = body.model_dump(exclude_unset=True)
    service = _svc(db, principal)
    return await _idempotent_mutation(
        db=db, principal=principal, endpoint_id="items.convert_to_inventory", key=key,
        method="POST", path=f"/api/v1/items/{item_id}/convert-to-inventory", body=payload, status=201,
        runner=lambda: service.convert_to_inventory(item_id, payload, actor_id=principal.identity_id),
    )


@router.get("/items/{item_id}/custom-fields", response_model=ItemCustomFieldsOut)
async def get_item_custom_fields(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_item_custom_fields(item_id)


@router.put("/items/{item_id}/custom-fields", response_model=ItemCustomFieldsOut)
async def put_item_custom_fields(
    item_id: str,
    body: ItemCustomFieldsUpdate,
    principal: UpdateDep,
    db: DbDep,
    key: IdempotencyDep = None,
) -> Any:
    assert key is not None
    payload = body.model_dump(exclude_unset=True)
    service = _svc(db, principal)
    values = payload.get("custom_fields") if isinstance(payload.get("custom_fields"), dict) else payload.get("values")
    if not isinstance(values, dict):
        raise item_error("custom_fields must be an object", code="CUSTOM_FIELD_INVALID")
    return await _idempotent_mutation(
        db=db, principal=principal, endpoint_id="items.custom_fields.put", key=key,
        method="PUT", path=f"/api/v1/items/{item_id}/custom-fields", body=payload, status=200,
        runner=lambda: service.set_item_custom_fields(item_id, values, actor_id=principal.identity_id),
    )


@router.patch("/items/{item_id}", response_model=ItemOut)
async def patch_item(item_id: str, body: ItemUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    return await _svc(db, principal).patch_item(item_id, payload | {"actor_id": principal.identity_id}, expected_version=_version(if_match))


@router.post("/items/{item_id}/inactivate", response_model=ItemOut)
async def inactivate_item(item_id: str, body: ItemInactivateRequest, principal: InactivateDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).inactivate(item_id, str(body.reason or "manual"), actor_id=principal.identity_id)


@router.post("/items/{item_id}/reactivate", response_model=ItemOut)
async def reactivate_item(item_id: str, principal: InactivateDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).reactivate(item_id, actor_id=principal.identity_id)


# -- extensions -------------------------------------------------------------------------------
@router.get("/items/{item_id}/inventory", response_model=ItemInventoryOut)
async def get_inventory(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_extension(item_id, "inventory")


@router.put("/items/{item_id}/inventory", response_model=ItemInventoryOut)
async def put_inventory(item_id: str, body: ItemInventorySettingsUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).upsert_extension(item_id, "inventory", body.model_dump(exclude_unset=True) | {"actor_id": principal.identity_id, "expected_version": _version(if_match)})


@router.get("/items/{item_id}/purchasing", response_model=ItemPurchasingOut)
async def get_purchasing(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_extension(item_id, "purchasing")


@router.put("/items/{item_id}/purchasing", response_model=ItemPurchasingOut)
async def put_purchasing(item_id: str, body: ItemPurchasingSettingsUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None, _: IdempotencyDep = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).upsert_extension(item_id, "purchasing", body.model_dump(exclude_unset=True) | {"actor_id": principal.identity_id})


@router.get("/items/{item_id}/sales", response_model=ItemSalesOut)
async def get_sales(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_extension(item_id, "sales")


@router.put("/items/{item_id}/sales", response_model=ItemSalesOut)
async def put_sales(item_id: str, body: ItemSalesSettingsUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None, _: IdempotencyDep = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).upsert_extension(item_id, "sales", body.model_dump(exclude_unset=True) | {"actor_id": principal.identity_id})


@router.get("/items/{item_id}/accounting", response_model=ItemAccountingOut)
async def get_accounting(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).get_extension(item_id, "accounting")


@router.put("/items/{item_id}/accounting", response_model=ItemAccountingOut)
async def put_accounting(item_id: str, body: ItemAccountingSettingsUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None, _: IdempotencyDep = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).upsert_extension(item_id, "accounting", body.model_dump(exclude_unset=True) | {"actor_id": principal.identity_id})


# -- UoM assignments / locations / planning ----------------------------------------------------
@router.get("/items/{item_id}/uoms", response_model=ItemUomAssignmentsOut)
async def list_item_uoms(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_item_uoms(item_id)}


@router.put("/items/{item_id}/uoms", response_model=ItemUomAssignmentsOut)
async def replace_item_uoms(item_id: str, body: ItemUomAssignmentsReplace, principal: UomDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).replace_item_uoms(item_id, [entry.model_dump(exclude_unset=True) for entry in body.uoms])}


@router.get("/items/{item_id}/locations", response_model=ItemLocationPage)
async def list_locations(item_id: str, principal: ReadDep, db: DbDep, location_id: str | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_locations(item_id, location_id=location_id)}


@router.put("/items/{item_id}/locations", response_model=ItemLocationPage)
async def replace_locations(item_id: str, body: ItemLocationsReplace, principal: LocationDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).replace_locations(item_id, [entry.model_dump(exclude_unset=True) for entry in body.locations])}


@router.patch("/items/{item_id}/locations/{location_id}", response_model=ItemLocationOut)
async def patch_location(item_id: str, location_id: str, body: ItemLocationUpdate, principal: LocationDep, db: DbDep, if_match: VersionHeader = None) -> dict[str, Any]:
    return await _svc(db, principal).patch_location(item_id, location_id, body.model_dump(exclude_unset=True), expected_version=_version(if_match))


@router.get("/items/{item_id}/availability-settings", response_model=AvailabilitySettingsOut)
async def availability_settings(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).availability_settings(item_id)


@router.post("/items/{item_id}/planning-values", response_model=PlanningValuesResultOut)
async def apply_planning_values(
    item_id: str,
    body: PlanningValuesApply,
    principal: PlanningDep,
    db: DbDep,
    if_match: VersionHeader = None,
    key: IdempotencyDep = None,
) -> dict[str, Any]:
    assert key is not None
    payload = body.model_dump(exclude_unset=True)
    values: dict[str, Any] = {name: payload[name] for name in ("reorder_point", "preferred_stock_level", "safety_stock", "lead_time_days", "seasonal_demand") if name in payload}
    service = _svc(db, principal)
    return await _idempotent_mutation(
        db=db, principal=principal, endpoint_id="items.planning_values", key=key,
        method="POST", path=f"/api/v1/items/{item_id}/planning-values", body=payload, status=200,
        runner=lambda: service.apply_planning_values(
            item_id, location_id=None, values=values, source=str(payload.get("source") or "MANUAL"),
            policy_version=payload.get("policy_version"), expected_version=_version(if_match),
            actor_id=principal.identity_id, idempotency_key=key,
        ),
    )


@router.post("/items/{item_id}/locations/{location_id}/planning-values", response_model=PlanningValuesResultOut)
async def apply_location_planning_values(
    item_id: str,
    location_id: str,
    body: PlanningValuesApply,
    principal: PlanningDep,
    db: DbDep,
    if_match: VersionHeader = None,
    key: IdempotencyDep = None,
) -> dict[str, Any]:
    assert key is not None
    payload = body.model_dump(exclude_unset=True)
    values: dict[str, Any] = {name: payload[name] for name in ("reorder_point", "preferred_stock_level", "safety_stock", "lead_time_days", "seasonal_demand") if name in payload}
    service = _svc(db, principal)
    return await _idempotent_mutation(
        db=db, principal=principal, endpoint_id="items.location_planning_values", key=key,
        method="POST", path=f"/api/v1/items/{item_id}/locations/{location_id}/planning-values", body=payload, status=200,
        runner=lambda: service.apply_planning_values(
            item_id, location_id=location_id, values=values, source=str(payload.get("source") or "MANUAL"),
            policy_version=payload.get("policy_version"), expected_version=_version(if_match),
            actor_id=principal.identity_id, idempotency_key=key,
        ),
    )


# -- barcodes / aliases / related --------------------------------------------------------------
@router.get("/items/{item_id}/barcodes", response_model=ItemBarcodePage)
async def list_barcodes(item_id: str, principal: ReadDep, db: DbDep, barcode_type: str | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_barcodes(item_id, barcode_type=barcode_type)}


@router.post("/items/{item_id}/barcodes", status_code=201, response_model=ItemBarcodeOut)
async def add_barcode(item_id: str, body: ItemBarcodeCreate, principal: BarcodeDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).add_barcode(item_id, body.model_dump(exclude_unset=True))


@router.patch("/items/{item_id}/barcodes/{barcode_id}", response_model=ItemBarcodeOut)
async def patch_barcode(item_id: str, barcode_id: str, body: ItemBarcodeUpdate, principal: BarcodeDep, db: DbDep, if_match: VersionHeader = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).update_barcode(item_id, barcode_id, body.model_dump(exclude_unset=True))


@router.get("/items/{item_id}/aliases", response_model=ItemAliasPage)
async def list_aliases(item_id: str, principal: ReadDep, db: DbDep, alias_type: str | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_aliases(item_id, alias_type=alias_type)}


@router.post("/items/{item_id}/aliases", status_code=201, response_model=ItemAliasOut)
async def add_alias(item_id: str, body: ItemAliasCreate, principal: UpdateDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).add_alias(item_id, body.model_dump(exclude_unset=True))


@router.get("/items/{item_id}/related", response_model=RelatedItemPage)
async def list_related(item_id: str, principal: ReadDep, db: DbDep, relation_type: str | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_related(item_id, relation_type=relation_type)}


@router.post("/items/{item_id}/related", status_code=201, response_model=RelatedItemOut)
async def add_related(item_id: str, body: ItemRelatedCreate, principal: UpdateDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).add_related(item_id, body.model_dump(exclude_unset=True))


@router.delete("/items/{item_id}/related/{related_id}", status_code=204)
async def delete_related(item_id: str, related_id: str, principal: UpdateDep, db: DbDep) -> None:
    await _svc(db, principal).delete_related(item_id, related_id)


# -- matrix ------------------------------------------------------------------------------------
@router.get("/items/{item_id}/matrix-options", response_model=MatrixOptionsOut)
async def list_matrix_options(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return {"options": await _svc(db, principal).list_matrix_options(item_id)}


@router.post("/items/{item_id}/matrix-options", status_code=201, response_model=MatrixOptionOut)
async def create_matrix_option(item_id: str, body: ItemMatrixOptionCreate, principal: MatrixDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).create_matrix_option(item_id, body.model_dump(exclude_unset=True))


@router.put("/items/{item_id}/matrix-options/{option_id}/values", response_model=MatrixOptionOut)
async def replace_matrix_values(item_id: str, option_id: str, body: MatrixOptionValuesReplace, principal: MatrixDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).replace_matrix_option_values(item_id, option_id, [entry.model_dump(exclude_unset=True) for entry in body.values])


@router.get("/items/{item_id}/matrix-children", response_model=MatrixChildPage)
async def list_matrix_children(item_id: str, principal: ReadDep, db: DbDep, is_active: bool | None = None) -> dict[str, Any]:
    return {"items": await _svc(db, principal).list_matrix_children(item_id, is_active=is_active)}


@router.post("/items/{item_id}/matrix-children/generate", status_code=201, response_model=MatrixGenerateOut)
async def generate_matrix_children(item_id: str, body: MatrixGenerateRequest, principal: MatrixDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).generate_matrix_children(item_id, [entry.model_dump(exclude_unset=True) for entry in body.combinations])


@router.post("/items/{item_id}/matrix-children/link", status_code=201, response_model=MatrixLinkOut)
async def link_matrix_child(item_id: str, body: MatrixChildLinkRequest, principal: MatrixDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).link_matrix_child(item_id, body.model_dump(exclude_unset=True))


# -- kit members -------------------------------------------------------------------------------
@router.get("/items/{item_id}/kit-members", response_model=KitMembersOut)
async def list_kit_members(item_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _svc(db, principal).list_kit_members(item_id)


@router.post("/items/{item_id}/kit-members", status_code=201, response_model=KitMembersOut)
async def add_kit_member(item_id: str, body: ItemKitMemberCreate, principal: UpdateDep, db: DbDep, _: IdempotencyDep = None) -> dict[str, Any]:
    return await _svc(db, principal).add_kit_member(item_id, body.model_dump(exclude_unset=True))


@router.patch("/items/{item_id}/kit-members/{member_id}", response_model=KitMembersOut)
async def update_kit_member(item_id: str, member_id: str, body: ItemKitMemberUpdate, principal: UpdateDep, db: DbDep, if_match: VersionHeader = None) -> dict[str, Any]:
    del if_match
    return await _svc(db, principal).update_kit_member(item_id, member_id, body.model_dump(exclude_unset=True))


@router.delete("/items/{item_id}/kit-members/{member_id}", status_code=204)
async def delete_kit_member(item_id: str, member_id: str, principal: UpdateDep, db: DbDep) -> None:
    await _svc(db, principal).delete_kit_member(item_id, member_id)


__all__ = ["router"]
