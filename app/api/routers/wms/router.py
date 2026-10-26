# ruff: noqa: B008 - FastAPI Body(...)/Query(...) defaults are the conventional concise form for this router.
"""FS-11 WMS API (`/api/v1`, catalogued scopes, tenant isolated, idempotent writes).

Thin transport over :class:`app.modules.wms.service.WmsService`; execution rules live in the service and
every inventory effect posts through the frozen lower-layer contracts (never direct ledger writes).
Each route declares exactly one catalogued scope (``ROUTE_SCOPES``); state-changing routes accept an
``Idempotency-Key``. Router registration is integrator-owned — this module exports ``router`` and
``ROUTE_SCOPES`` only.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.wms import (
    BinBlockCreate,
    BinBlockOut,
    BinBlockPage,
    CancelTaskIn,
    CarrierServiceLevelCreate,
    CarrierServiceLevelOut,
    CarrierServiceLevelPage,
    CartCreate,
    CartOut,
    CartPage,
    CartUpdate,
    CycleCountRequestCreate,
    CycleCountRequestOut,
    DeviceSessionCreate,
    DeviceSessionOut,
    FulfillmentGenerationPage,
    GeneratePutawayOut,
    InboundShipmentCreate,
    InboundShipmentOut,
    InboundShipmentPage,
    ItemBinAssignmentOut,
    ItemBinAssignmentPut,
    ItemProcessAssignmentCreate,
    ItemProcessAssignmentOut,
    ItemProcessAssignmentPage,
    ItemProcessFamilyCreate,
    ItemProcessFamilyOut,
    ItemProcessFamilyPage,
    ItemProcessGroupCreate,
    ItemProcessGroupOut,
    ItemProcessGroupPage,
    ItemStockLocationCreate,
    ItemStockLocationOut,
    ItemStockLocationPage,
    ItemStockLocationUpdate,
    OrderReleaseStatusPage,
    OrderTypeCreate,
    OrderTypeOut,
    OrderTypePage,
    PackCartonCreate,
    PackCartonOut,
    PackCompleteRequest,
    PackTaskCreate,
    PackTaskOut,
    PackTaskPage,
    PickCompleteRequest,
    PickStrategyCreate,
    PickStrategyOut,
    PickStrategyPage,
    PickStrategyUpdate,
    PickTaskOut,
    PickTaskPage,
    PutawayGenerateRequest,
    PutawayStrategyCreate,
    PutawayStrategyOut,
    PutawayStrategyPage,
    PutawayStrategyUpdate,
    PutawayTaskOut,
    PutawayTaskPage,
    ReceivingCompleteOut,
    ReceivingCompleteRequest,
    ReceivingOrderCreate,
    ReceivingOrderOut,
    ReceivingOrderPage,
    ReceivingReverseOut,
    ReleaseWaveOut,
    ReplenishmentPlanCreate,
    ReplenishmentPlanOut,
    ReplenishmentPlanPage,
    ReplenishmentTaskComplete,
    ReplenishmentTaskOut,
    ReplenishmentTaskPage,
    ScheduleRunOut,
    ShipManifestCreate,
    ShipManifestOut,
    ShipManifestPage,
    ShipmentActionRequest,
    ShipmentCreate,
    ShipmentOut,
    ShipmentPage,
    StatusChangeTaskCreate,
    StatusChangeTaskOut,
    SystemRulePage,
    SystemRulePut,
    SystemRuleSettingOut,
    TaskAssignIn,
    TaskCompleteRequest,
    TaskExceptionIn,
    TaskSummaryPage,
    TransitionIn,
    UccCodeFormatCreate,
    UccCodeFormatOut,
    UccCodeFormatPage,
    WarehouseCreate,
    WarehouseInventoryCreate,
    WarehouseInventoryCreateOut,
    WarehouseInventoryOut,
    WarehouseOut,
    WarehousePage,
    WarehouseUpdate,
    WaveCreate,
    WaveOut,
    WavePage,
    WaveReleaseRequest,
    WaveReleaseScheduleCreate,
    WaveReleaseScheduleOut,
    WaveReleaseSchedulePage,
    WaveReleaseScheduleUpdate,
)
from app.modules.wms.errors import WmsError
from app.modules.wms.service import WmsService

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["wms"], route_class=SCMAPIRoute)

WMS_READ = "wms.read"
WMS_SETUP = "wms.setup.write"
WMS_RECEIVE = "wms.receive"
WMS_PUTAWAY = "wms.putaway"
WMS_PICK = "wms.pick"
WMS_PACK = "wms.pack"
WMS_SHIP = "wms.ship"
WMS_WAVE = "wms.wave"
WMS_REPLENISH = "wms.replenish"
WMS_INVENTORY = "wms.inventory"
WMS_ADMIN = "wms.admin"

ROUTE_SCOPES: dict[tuple[str, str], str] = {}


def _route(method: str, path: str, scope: str) -> None:
    ROUTE_SCOPES[(method, path)] = scope


IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, WmsError):
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


def _service(db: AsyncSession, principal: PrincipalDep) -> WmsService:
    return WmsService(db, principal.tenant_id)


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 100), 500))


# ================================================================== setup/configuration ========
_route("POST", "/api/v1/warehouses", WMS_SETUP)
_route("GET", "/api/v1/warehouses", WMS_READ)
_route("GET", "/api/v1/warehouses/{id}", WMS_READ)
_route("PATCH", "/api/v1/warehouses/{id}", WMS_SETUP)


@router.post("/warehouses", response_model=WarehouseOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_warehouse(
    db: DbDep, principal: PrincipalDep, body: WarehouseCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_warehouse(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/warehouses", response_model=WarehousePage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_warehouses(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None,
    is_active: bool | None = None, limit: int = 100,
) -> dict[str, Any]:
    items = await _service(db, principal).list_warehouses(
        location_id=location_id, is_active=is_active, limit=_limit(limit)
    )
    return {"items": items, "count": len(items)}


@router.get("/warehouses/{id}", response_model=WarehouseOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_warehouse(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_warehouse(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/warehouses/{id}", response_model=WarehouseOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def update_warehouse(
    id: str, db: DbDep, principal: PrincipalDep, body: WarehouseUpdate = Body(...)
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).update_warehouse(
            id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


_SETUP_LISTS = {
    "item-process-families": "families",
    "item-process-groups": "groups",
    "item-process-assignments": "assignments",
    "item-stock-locations": "stock-locations",
    "order-types": "order-types",
    "ucc-code-formats": "ucc-code-formats",
    "carrier-service-levels": "carrier-service-levels",
    "pick-strategies": "pick-strategies",
    "putaway-strategies": "putaway-strategies",
}
for _path, _kind in _SETUP_LISTS.items():
    ROUTE_SCOPES[("GET", f"/api/v1/{_path}")] = WMS_READ
    ROUTE_SCOPES[("POST", f"/api/v1/{_path}")] = WMS_SETUP
    if _kind in ("pick-strategies", "putaway-strategies", "stock-locations"):
        ROUTE_SCOPES[("PATCH", f"/api/v1/{_path}")] = WMS_SETUP


@router.get("/item-process-families", response_model=ItemProcessFamilyPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_process_families(db: DbDep, principal: PrincipalDep, limit: int = 100) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup("families", limit=_limit(limit))}


@router.post("/item-process-families", response_model=ItemProcessFamilyOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_process_family(
    db: DbDep, principal: PrincipalDep, body: ItemProcessFamilyCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "families", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/item-process-groups", response_model=ItemProcessGroupPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_process_groups(
    db: DbDep, principal: PrincipalDep, item_process_family_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup(
        "groups", filters={"item_process_family_id": item_process_family_id}, limit=_limit(limit)
    )}


@router.post("/item-process-groups", response_model=ItemProcessGroupOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_process_group(
    db: DbDep, principal: PrincipalDep, body: ItemProcessGroupCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "groups", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/item-process-assignments", response_model=ItemProcessAssignmentPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_process_assignments(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup(
        "assignments", filters={"item_id": item_id}, limit=_limit(limit)
    )}


@router.post("/item-process-assignments", response_model=ItemProcessAssignmentOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_process_assignment(
    db: DbDep, principal: PrincipalDep, body: ItemProcessAssignmentCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "assignments", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/item-stock-locations", response_model=ItemStockLocationPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_stock_locations(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None, location_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup(
        "stock-locations", filters={"item_id": item_id, "location_id": location_id}, limit=_limit(limit)
    )}


@router.post("/item-stock-locations", response_model=ItemStockLocationOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_stock_location(
    db: DbDep, principal: PrincipalDep, body: ItemStockLocationCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "stock-locations", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/item-stock-locations", response_model=ItemStockLocationOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def update_stock_location(
    db: DbDep, principal: PrincipalDep, body: ItemStockLocationUpdate = Body(...)
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    stock_id = payload.get("id")
    if not stock_id:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": "id is required"})
    try:
        return await _service(db, principal).update_setup(
            "stock-locations", stock_id, payload, actor_id=principal.app_user_id,
            expected_version=payload.get("version"),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/order-types", response_model=OrderTypePage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_order_types(db: DbDep, principal: PrincipalDep, limit: int = 100) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup("order-types", limit=_limit(limit))}


@router.post("/order-types", response_model=OrderTypeOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_order_type(
    db: DbDep, principal: PrincipalDep, body: OrderTypeCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "order-types", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/ucc-code-formats", response_model=UccCodeFormatPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_ucc_formats(db: DbDep, principal: PrincipalDep, limit: int = 100) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup("ucc-code-formats", limit=_limit(limit))}


@router.post("/ucc-code-formats", response_model=UccCodeFormatOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_ucc_format(
    db: DbDep, principal: PrincipalDep, body: UccCodeFormatCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "ucc-code-formats", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/carrier-service-levels", response_model=CarrierServiceLevelPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_carrier_service_levels(db: DbDep, principal: PrincipalDep, limit: int = 100) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_setup("carrier-service-levels", limit=_limit(limit))}


@router.post("/carrier-service-levels", response_model=CarrierServiceLevelOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_carrier_service_level(
    db: DbDep, principal: PrincipalDep, body: CarrierServiceLevelCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_setup(
            "carrier-service-levels", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/pick-strategies", response_model=PickStrategyPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_pick_strategies(
    db: DbDep, principal: PrincipalDep, is_active: bool | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_strategies(
        "pick-strategies", is_active=is_active, limit=_limit(limit)
    )}


@router.post("/pick-strategies", response_model=PickStrategyOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_pick_strategy(
    db: DbDep, principal: PrincipalDep, body: PickStrategyCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_strategy(
            "pick-strategies", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/pick-strategies", response_model=PickStrategyOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def update_pick_strategy(db: DbDep, principal: PrincipalDep, body: PickStrategyUpdate = Body(...)) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    strategy_id = payload.get("id")
    if not strategy_id:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": "id is required"})
    try:
        return await _service(db, principal).update_strategy(
            "pick-strategies", strategy_id, payload, actor_id=principal.app_user_id,
            expected_version=payload.get("version"),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/putaway-strategies", response_model=PutawayStrategyPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_putaway_strategies(
    db: DbDep, principal: PrincipalDep, is_active: bool | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_strategies(
        "putaway-strategies", is_active=is_active, limit=_limit(limit)
    )}


@router.post("/putaway-strategies", response_model=PutawayStrategyOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_putaway_strategy(
    db: DbDep, principal: PrincipalDep, body: PutawayStrategyCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_strategy(
            "putaway-strategies", payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/putaway-strategies", response_model=PutawayStrategyOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def update_putaway_strategy(
    db: DbDep, principal: PrincipalDep, body: PutawayStrategyUpdate = Body(...)
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    strategy_id = payload.get("id")
    if not strategy_id:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": "id is required"})
    try:
        return await _service(db, principal).update_strategy(
            "putaway-strategies", strategy_id, payload, actor_id=principal.app_user_id,
            expected_version=payload.get("version"),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# -- system rules / bin blocks / assignments -------------------------------------------------
_route("GET", "/api/v1/system-rules", WMS_READ)
_route("PUT", "/api/v1/warehouses/{id}/system-rules/{code}", WMS_SETUP)
_route("PUT", "/api/v1/warehouses/{id}/item-bin-assignments", WMS_SETUP)
_route("GET", "/api/v1/bin-blocks", WMS_READ)
_route("POST", "/api/v1/bin-blocks", WMS_SETUP)
_route("POST", "/api/v1/bin-blocks/{id}/release", WMS_SETUP)


@router.get("/system-rules", response_model=SystemRulePage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_system_rules(db: DbDep, principal: PrincipalDep, warehouse_id: str | None = None) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_system_rules(warehouse_id)}


@router.put(
    "/warehouses/{id}/system-rules/{code}", response_model=SystemRuleSettingOut, dependencies=[Depends(require_scope(WMS_SETUP))]
)
async def set_system_rule(
    id: str, code: str, db: DbDep, principal: PrincipalDep, body: SystemRulePut | None = Body(default=None)
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).set_system_rule(
            id, code, payload, actor_id=principal.app_user_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put(
    "/warehouses/{id}/item-bin-assignments", response_model=ItemBinAssignmentOut, dependencies=[Depends(require_scope(WMS_SETUP))]
)
async def assign_item_bins(
    id: str, db: DbDep, principal: PrincipalDep, body: ItemBinAssignmentPut = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).assign_item_bins(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/bin-blocks", response_model=BinBlockPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_bin_blocks(
    db: DbDep, principal: PrincipalDep, bin_id: str | None = None, released: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_bin_blocks(
        bin_id=bin_id, released=released, limit=_limit(limit)
    )}


@router.post("/bin-blocks", response_model=BinBlockOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_bin_block(
    db: DbDep, principal: PrincipalDep, body: BinBlockCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_bin_block(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/bin-blocks/{id}/release", response_model=BinBlockOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def release_bin_block(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).release_bin_block(
            id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# -- warehouse inventory / cycle count ------------------------------------------------------
_route("GET", "/api/v1/warehouses/{id}/inventory", WMS_READ)
_route("POST", "/api/v1/warehouses/{id}/inventory", WMS_INVENTORY)
_route("POST", "/api/v1/warehouses/{id}/cycle-count-requests", WMS_INVENTORY)


@router.get("/warehouses/{id}/inventory", response_model=WarehouseInventoryOut, dependencies=[Depends(require_scope(WMS_READ))])
async def list_warehouse_inventory(
    id: str, db: DbDep, principal: PrincipalDep, item_id: str | None = None, bin_id: str | None = None,
    inventory_status_id: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_warehouse_inventory(
            id, item_id=item_id, bin_id=bin_id, inventory_status_id=inventory_status_id, limit=_limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/warehouses/{id}/inventory", response_model=WarehouseInventoryCreateOut, status_code=201, dependencies=[Depends(require_scope(WMS_INVENTORY))])
async def create_warehouse_inventory(
    id: str, db: DbDep, principal: PrincipalDep, body: WarehouseInventoryCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_warehouse_inventory(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/warehouses/{id}/cycle-count-requests", response_model=CycleCountRequestOut, status_code=201,
    dependencies=[Depends(require_scope(WMS_INVENTORY))],
)
async def request_cycle_count(
    id: str, db: DbDep, principal: PrincipalDep, body: CycleCountRequestCreate | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).request_cycle_count(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ==================================================================== inbound processing ======
for _method, _path, _scope in (
    ("GET", "/api/v1/inbound-shipments", WMS_READ),
    ("POST", "/api/v1/inbound-shipments", WMS_RECEIVE),
    ("GET", "/api/v1/inbound-shipments/{id}", WMS_READ),
    ("POST", "/api/v1/inbound-shipments/{id}/arrive", WMS_RECEIVE),
    ("POST", "/api/v1/inbound-shipments/{id}/cancel", WMS_RECEIVE),
    ("GET", "/api/v1/receiving-orders", WMS_READ),
    ("POST", "/api/v1/receiving-orders", WMS_RECEIVE),
    ("GET", "/api/v1/receiving-orders/{id}", WMS_READ),
    ("POST", "/api/v1/receiving-orders/{id}/start", WMS_RECEIVE),
    ("POST", "/api/v1/receiving-orders/{id}/complete", WMS_RECEIVE),
    ("POST", "/api/v1/receiving-orders/{id}/reverse-receipt", WMS_RECEIVE),
    ("POST", "/api/v1/receiving-orders/{id}/cancel", WMS_RECEIVE),
    ("GET", "/api/v1/putaway-tasks", WMS_READ),
    ("POST", "/api/v1/putaway-tasks/generate", WMS_PUTAWAY),
    ("GET", "/api/v1/putaway-tasks/{id}", WMS_READ),
    ("POST", "/api/v1/putaway-tasks/{id}/assign", WMS_PUTAWAY),
    ("POST", "/api/v1/putaway-tasks/{id}/start", WMS_PUTAWAY),
    ("POST", "/api/v1/putaway-tasks/{id}/complete", WMS_PUTAWAY),
    ("POST", "/api/v1/putaway-tasks/{id}/exception", WMS_PUTAWAY),
    ("POST", "/api/v1/putaway-tasks/{id}/cancel", WMS_PUTAWAY),
):
    ROUTE_SCOPES[(_method, _path)] = _scope


@router.get("/inbound-shipments", response_model=InboundShipmentPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_inbound_shipments(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_inbound_shipments(
        location_id=location_id, status=status, limit=_limit(limit)
    )}


@router.post("/inbound-shipments", response_model=InboundShipmentOut, status_code=201, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def create_inbound_shipment(
    db: DbDep, principal: PrincipalDep, body: InboundShipmentCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_inbound_shipment(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/inbound-shipments/{id}", response_model=InboundShipmentOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_inbound_shipment(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_inbound_shipment(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/inbound-shipments/{id}/arrive", response_model=InboundShipmentOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def arrive_inbound_shipment(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).transition_inbound_shipment(
            id, "ARRIVED", actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/inbound-shipments/{id}/cancel", response_model=InboundShipmentOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def cancel_inbound_shipment(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).transition_inbound_shipment(
            id, "CANCELLED", actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/receiving-orders", response_model=ReceivingOrderPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_receiving_orders(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_receiving_orders(
        location_id=location_id, status=status, limit=_limit(limit)
    )}


@router.post("/receiving-orders", response_model=ReceivingOrderOut, status_code=201, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def create_receiving_order(
    db: DbDep, principal: PrincipalDep, body: ReceivingOrderCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_receiving_order(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/receiving-orders/{id}", response_model=ReceivingOrderOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_receiving_order(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_receiving_order(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/receiving-orders/{id}/start", response_model=ReceivingOrderOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def start_receiving_order(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).transition_receiving_order(
            id, "IN_PROGRESS", actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/receiving-orders/{id}/complete", response_model=ReceivingCompleteOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def complete_receiving_order(
    id: str, db: DbDep, principal: PrincipalDep, body: ReceivingCompleteRequest = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).complete_receiving_order(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/receiving-orders/{id}/reverse-receipt", response_model=ReceivingReverseOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def reverse_receiving_order(
    id: str, db: DbDep, principal: PrincipalDep, body: CancelTaskIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).reverse_receiving_order(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/receiving-orders/{id}/cancel", response_model=ReceivingOrderOut, dependencies=[Depends(require_scope(WMS_RECEIVE))])
async def cancel_receiving_order(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).transition_receiving_order(
            id, "CANCELLED", actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/putaway-tasks", response_model=PutawayTaskPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_putaway_tasks(
    db: DbDep, principal: PrincipalDep, status: str | None = None, warehouse_id: str | None = None,
    assigned_user_id: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_putaway_tasks(
        status=status, warehouse_id=warehouse_id, assigned_user_id=assigned_user_id, limit=_limit(limit)
    )}


@router.post("/putaway-tasks/generate", response_model=GeneratePutawayOut, status_code=201, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def generate_putaway_tasks(
    db: DbDep, principal: PrincipalDep, body: PutawayGenerateRequest = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).generate_putaway_tasks(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/putaway-tasks/{id}", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_putaway_task(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_putaway_task(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/putaway-tasks/{id}/assign", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def assign_putaway_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TaskAssignIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).assign_putaway_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/putaway-tasks/{id}/start", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def start_putaway_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).start_putaway_task(
            id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/putaway-tasks/{id}/complete", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def complete_putaway_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TaskCompleteRequest | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).complete_putaway_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/putaway-tasks/{id}/exception", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def exception_putaway_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TaskExceptionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).exception_putaway_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/putaway-tasks/{id}/cancel", response_model=PutawayTaskOut, dependencies=[Depends(require_scope(WMS_PUTAWAY))])
async def cancel_putaway_task(
    id: str, db: DbDep, principal: PrincipalDep, body: CancelTaskIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).cancel_putaway_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ==================================================================== outbound processing =====
for _method, _path, _scope in (
    ("GET", "/api/v1/waves", WMS_READ),
    ("POST", "/api/v1/waves", WMS_WAVE),
    ("GET", "/api/v1/waves/{id}", WMS_READ),
    ("POST", "/api/v1/waves/{id}/release", WMS_WAVE),
    ("POST", "/api/v1/waves/{id}/cancel", WMS_WAVE),
    ("GET", "/api/v1/wave-release-schedules", WMS_READ),
    ("POST", "/api/v1/wave-release-schedules", WMS_SETUP),
    ("PATCH", "/api/v1/wave-release-schedules", WMS_SETUP),
    ("POST", "/api/v1/wave-release-schedules/{id}/run-now", WMS_WAVE),
    ("GET", "/api/v1/pick-tasks", WMS_READ),
    ("GET", "/api/v1/pick-tasks/{id}", WMS_READ),
    ("POST", "/api/v1/pick-tasks/{id}/assign", WMS_PICK),
    ("POST", "/api/v1/pick-tasks/{id}/start", WMS_PICK),
    ("POST", "/api/v1/pick-tasks/{id}/complete", WMS_PICK),
    ("POST", "/api/v1/pick-tasks/{id}/reverse", WMS_PICK),
    ("GET", "/api/v1/pack-tasks", WMS_READ),
    ("POST", "/api/v1/pack-tasks", WMS_PACK),
    ("POST", "/api/v1/pack-tasks/{id}/cartons", WMS_PACK),
    ("POST", "/api/v1/pack-tasks/{id}/complete", WMS_PACK),
    ("GET", "/api/v1/shipments", WMS_READ),
    ("POST", "/api/v1/shipments", WMS_SHIP),
    ("POST", "/api/v1/shipments/{id}/ready", WMS_SHIP),
    ("POST", "/api/v1/shipments/{id}/ship", WMS_SHIP),
    ("POST", "/api/v1/shipments/{id}/confirm", WMS_SHIP),
    ("POST", "/api/v1/shipments/{id}/cancel", WMS_SHIP),
    ("GET", "/api/v1/ship-manifests", WMS_READ),
    ("POST", "/api/v1/ship-manifests", WMS_SHIP),
    ("POST", "/api/v1/ship-manifests/{id}/send", WMS_SHIP),
):
    ROUTE_SCOPES[(_method, _path)] = _scope


@router.get("/waves", response_model=WavePage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_waves(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_waves(
        location_id=location_id, status=status, limit=_limit(limit)
    )}


@router.post("/waves", response_model=WaveOut, status_code=201, dependencies=[Depends(require_scope(WMS_WAVE))])
async def create_wave(
    db: DbDep, principal: PrincipalDep, body: WaveCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_wave(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/waves/{id}", response_model=WaveOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_wave(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_wave(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/waves/{id}/release", response_model=ReleaseWaveOut, dependencies=[Depends(require_scope(WMS_WAVE))])
async def release_wave(
    id: str, db: DbDep, principal: PrincipalDep, body: WaveReleaseRequest | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).release_wave(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/waves/{id}/cancel", response_model=WaveOut, dependencies=[Depends(require_scope(WMS_WAVE))])
async def cancel_wave(
    id: str, db: DbDep, principal: PrincipalDep, body: CancelTaskIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).cancel_wave(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/wave-release-schedules", response_model=WaveReleaseSchedulePage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_wave_release_schedules(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, is_active: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_wave_release_schedules(
        location_id=location_id, is_active=is_active, limit=_limit(limit)
    )}


@router.post("/wave-release-schedules", response_model=WaveReleaseScheduleOut, status_code=201, dependencies=[Depends(require_scope(WMS_SETUP))])
async def create_wave_release_schedule(
    db: DbDep, principal: PrincipalDep, body: WaveReleaseScheduleCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_wave_release_schedule(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/wave-release-schedules", response_model=WaveReleaseScheduleOut, dependencies=[Depends(require_scope(WMS_SETUP))])
async def update_wave_release_schedule(
    db: DbDep, principal: PrincipalDep, body: WaveReleaseScheduleUpdate = Body(...)
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    schedule_id = payload.get("id")
    if not schedule_id:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": "id is required"})
    try:
        return await _service(db, principal).update_wave_release_schedule(
            schedule_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/wave-release-schedules/{id}/run-now", response_model=ScheduleRunOut, dependencies=[Depends(require_scope(WMS_WAVE))]
)
async def run_wave_release_schedule(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).run_wave_release_schedule(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/pick-tasks", response_model=PickTaskPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_pick_tasks(
    db: DbDep, principal: PrincipalDep, wave_id: str | None = None, status: str | None = None,
    task_type: str | None = None, assigned_user_id: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_pick_tasks(
        wave_id=wave_id, status=status, task_type=task_type, assigned_user_id=assigned_user_id,
        limit=_limit(limit),
    )}


@router.get("/pick-tasks/{id}", response_model=PickTaskOut, dependencies=[Depends(require_scope(WMS_READ))])
async def get_pick_task(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_pick_task(id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pick-tasks/{id}/assign", response_model=PickTaskOut, dependencies=[Depends(require_scope(WMS_PICK))])
async def assign_pick_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TaskAssignIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).assign_pick_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pick-tasks/{id}/start", response_model=PickTaskOut, dependencies=[Depends(require_scope(WMS_PICK))])
async def start_pick_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).start_pick_task(
            id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pick-tasks/{id}/complete", response_model=PickTaskOut, dependencies=[Depends(require_scope(WMS_PICK))])
async def complete_pick_task(
    id: str, db: DbDep, principal: PrincipalDep, body: PickCompleteRequest | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).complete_pick_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pick-tasks/{id}/reverse", response_model=PickTaskOut, dependencies=[Depends(require_scope(WMS_PICK))])
async def reverse_pick_task(
    id: str, db: DbDep, principal: PrincipalDep, body: CancelTaskIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).reverse_pick_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/pack-tasks", response_model=PackTaskPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_pack_tasks(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_pack_tasks(status=status, limit=_limit(limit))}


@router.post("/pack-tasks", response_model=PackTaskOut, status_code=201, dependencies=[Depends(require_scope(WMS_PACK))])
async def create_pack_task(
    db: DbDep, principal: PrincipalDep, body: PackTaskCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_pack_task(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pack-tasks/{id}/cartons", response_model=PackCartonOut, status_code=201, dependencies=[Depends(require_scope(WMS_PACK))])
async def add_pack_carton(
    id: str, db: DbDep, principal: PrincipalDep, body: PackCartonCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).add_pack_carton(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/pack-tasks/{id}/complete", response_model=PackTaskOut, dependencies=[Depends(require_scope(WMS_PACK))])
async def complete_pack_task(
    id: str, db: DbDep, principal: PrincipalDep, body: PackCompleteRequest | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).complete_pack_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/shipments", response_model=ShipmentPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_shipments(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_shipments(
        location_id=location_id, status=status, limit=_limit(limit)
    )}


@router.post("/shipments", response_model=ShipmentOut, status_code=201, dependencies=[Depends(require_scope(WMS_SHIP))])
async def create_shipment(
    db: DbDep, principal: PrincipalDep, body: ShipmentCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_shipment(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


for _action, _method in (("ready", "READY"), ("ship", "SHIPPED"), ("confirm", "SHIPPED"),
                         ("cancel", "CANCELLED")):

    async def _shipment_action(
        id: str, db: DbDep, principal: PrincipalDep,
        body: ShipmentActionRequest | None = Body(default=None),
        idempotency_key: IdempotencyHeader = None, _target: str = _method,
    ) -> dict[str, Any]:
        payload = body.model_dump(exclude_unset=True) if body is not None else {}
        try:
            return await _service(db, principal).transition_shipment(
                id, _target, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
            )
        except Exception as exc:  # noqa: BLE001
            _raise(exc)

    _shipment_action.__name__ = f"shipment_{_action}"
    router.add_api_route(
        f"/shipments/{{id}}/{_action}",
        _shipment_action,
        methods=["POST"],
        response_model=ShipmentOut,
        dependencies=[Depends(require_scope(WMS_SHIP))],
        name=_shipment_action.__name__,
    )


@router.get("/ship-manifests", response_model=ShipManifestPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_ship_manifests(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_ship_manifests(status=status, limit=_limit(limit))}


@router.post("/ship-manifests", response_model=ShipManifestOut, status_code=201, dependencies=[Depends(require_scope(WMS_SHIP))])
async def create_ship_manifest(
    db: DbDep, principal: PrincipalDep, body: ShipManifestCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_ship_manifest(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/ship-manifests/{id}/send", response_model=ShipManifestOut, dependencies=[Depends(require_scope(WMS_SHIP))])
async def send_ship_manifest(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).send_ship_manifest(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ================================================================ replenishment / status ====
for _method, _path, _scope in (
    ("GET", "/api/v1/replenishment-plans", WMS_READ),
    ("POST", "/api/v1/replenishment-plans", WMS_REPLENISH),
    ("POST", "/api/v1/replenishment-plans/{id}/approve", WMS_REPLENISH),
    ("GET", "/api/v1/replenishment-tasks", WMS_READ),
    ("POST", "/api/v1/replenishment-tasks/{id}/complete", WMS_REPLENISH),
    ("POST", "/api/v1/status-change-tasks", WMS_INVENTORY),
    ("POST", "/api/v1/status-change-tasks/{id}/complete", WMS_INVENTORY),
    ("GET", "/api/v1/wms/tasks", WMS_READ),
    ("GET", "/api/v1/wms/fulfillment-generations", WMS_READ),
    ("GET", "/api/v1/wms/order-release-status", WMS_READ),
    ("GET", "/api/v1/wms/carts", WMS_ADMIN),
    ("POST", "/api/v1/wms/carts", WMS_ADMIN),
    ("PATCH", "/api/v1/wms/carts/{id}", WMS_ADMIN),
    ("POST", "/api/v1/wms/device-sessions", WMS_ADMIN),
    ("POST", "/api/v1/wms/device-sessions/{id}/end", WMS_ADMIN),
    ("PATCH", "/api/v1/wms/device-sessions/{id}/heartbeat", WMS_ADMIN),
):
    ROUTE_SCOPES[(_method, _path)] = _scope


@router.get("/replenishment-plans", response_model=ReplenishmentPlanPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_replenishment_plans(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_replenishment_plans(
        location_id=location_id, status=status, limit=_limit(limit)
    )}


@router.post("/replenishment-plans", response_model=ReplenishmentPlanOut, status_code=201, dependencies=[Depends(require_scope(WMS_REPLENISH))])
async def create_replenishment_plan(
    db: DbDep, principal: PrincipalDep, body: ReplenishmentPlanCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_replenishment_plan(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/replenishment-plans/{id}/approve", response_model=ReplenishmentPlanOut, dependencies=[Depends(require_scope(WMS_REPLENISH))])
async def approve_replenishment_plan(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).approve_replenishment_plan(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/replenishment-tasks", response_model=ReplenishmentTaskPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_replenishment_tasks(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_replenishment_tasks(status=status, limit=_limit(limit))}


@router.post("/replenishment-tasks/{id}/complete", response_model=ReplenishmentTaskOut, dependencies=[Depends(require_scope(WMS_REPLENISH))])
async def complete_replenishment_task(
    id: str, db: DbDep, principal: PrincipalDep, body: ReplenishmentTaskComplete | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).complete_replenishment_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/status-change-tasks", response_model=StatusChangeTaskOut, status_code=201, dependencies=[Depends(require_scope(WMS_INVENTORY))])
async def create_status_change_task(
    db: DbDep, principal: PrincipalDep, body: StatusChangeTaskCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_status_change_task(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/status-change-tasks/{id}/complete", response_model=StatusChangeTaskOut, dependencies=[Depends(require_scope(WMS_INVENTORY))]
)
async def complete_status_change_task(
    id: str, db: DbDep, principal: PrincipalDep, body: TaskCompleteRequest | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True) if body is not None else {}
    try:
        return await _service(db, principal).complete_status_change_task(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/wms/tasks", response_model=TaskSummaryPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_tasks(
    db: DbDep, principal: PrincipalDep, task_type: str | None = None, status: str | None = None,
    warehouse_id: str | None = None, assigned_user_id: str | None = None, limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).list_tasks(
        task_type=task_type, status=status, warehouse_id=warehouse_id,
        assigned_user_id=assigned_user_id, limit=_limit(limit),
    )


@router.get("/wms/fulfillment-generations", response_model=FulfillmentGenerationPage, dependencies=[Depends(require_scope(WMS_READ))])
async def list_fulfillment_generations(
    db: DbDep, principal: PrincipalDep, wave_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).fulfillment_generations(
        wave_id=wave_id, status=status, limit=_limit(limit)
    )


@router.get("/wms/order-release-status", response_model=OrderReleaseStatusPage, dependencies=[Depends(require_scope(WMS_READ))])
async def order_release_status(
    db: DbDep, principal: PrincipalDep, location_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).order_release_status(
        location_id=location_id, status=status, limit=_limit(limit)
    )


@router.get("/wms/carts", response_model=CartPage, dependencies=[Depends(require_scope(WMS_ADMIN))])
async def list_carts(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return {"items": await _service(db, principal).list_carts(status=status, limit=_limit(limit))}


@router.post("/wms/carts", response_model=CartOut, status_code=201, dependencies=[Depends(require_scope(WMS_ADMIN))])
async def create_cart(
    db: DbDep, principal: PrincipalDep, body: CartCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_cart(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/wms/carts/{id}", response_model=CartOut, dependencies=[Depends(require_scope(WMS_ADMIN))])
async def update_cart(
    id: str, db: DbDep, principal: PrincipalDep, body: CartUpdate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).update_cart(
            id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/wms/device-sessions", response_model=DeviceSessionOut, status_code=201, dependencies=[Depends(require_scope(WMS_ADMIN))])
async def create_device_session(
    db: DbDep, principal: PrincipalDep, body: DeviceSessionCreate = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_device_session(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch(
    "/wms/device-sessions/{id}/heartbeat", response_model=DeviceSessionOut, dependencies=[Depends(require_scope(WMS_ADMIN))]
)
async def heartbeat_device_session(id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).heartbeat_device_session(id, actor_id=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/wms/device-sessions/{id}/end", response_model=DeviceSessionOut, dependencies=[Depends(require_scope(WMS_ADMIN))])
async def end_device_session(
    id: str, db: DbDep, principal: PrincipalDep, body: TransitionIn | None = Body(default=None),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).end_device_session(
            id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


__all__ = ["ROUTE_SCOPES", "router"]
