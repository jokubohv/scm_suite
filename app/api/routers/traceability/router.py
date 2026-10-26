"""FS-03 storage + inventory-status registry API (29 routes, `/api/v1`, catalogued scopes).

Thin transport over :class:`app.modules.traceability.service.TraceabilityService`; topology/status rules
(and the stock-coupled guard degradation contract) live in the service. Writes require
``Idempotency-Key`` (durable ``platform.idempotency_key`` replay), mutable resources use ``If-Match``,
every route requires one catalogued scope, and the deploy-dark flag ``traceability.registry.enabled``
gates the surface. Router registration is integrator-owned; exposes ``router`` + ``ROUTE_SCOPES``.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.traceability import (
    InventoryStatusCreate,
    InventoryStatusUpdate,
    LocationCreate,
    LocationUpdate,
    TraceabilityRequest,
    TraceabilityResponse,
)
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.models import platform as platform_models
from app.modules.traceability.errors import TraceabilityError
from app.modules.traceability.service import TraceabilityService

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["traceability"], route_class=SCMAPIRoute)

LOCATION_READ = "inventory.location.read"
LOCATION_MANAGE = "inventory.location.manage"
BIN_READ = "inventory.bin.read"
BIN_MANAGE = "inventory.bin.manage"
STATUS_READ = "inventory.status.read"
STATUS_MANAGE = "inventory.status.manage"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/locations"): LOCATION_MANAGE,
    ("GET", "/api/v1/locations"): LOCATION_READ,
    ("GET", "/api/v1/locations/{id}"): LOCATION_READ,
    ("PATCH", "/api/v1/locations/{id}"): LOCATION_MANAGE,
    ("POST", "/api/v1/locations/{id}/activate"): LOCATION_MANAGE,
    ("POST", "/api/v1/locations/{id}/inactivate"): LOCATION_MANAGE,
    ("GET", "/api/v1/locations/{id}/children"): LOCATION_READ,
    ("GET", "/api/v1/locations/{id}/descendants"): LOCATION_READ,
    ("GET", "/api/v1/locations/{id}/bins"): BIN_READ,
    ("GET", "/api/v1/locations/{id}/posting-eligibility"): LOCATION_READ,
    ("POST", "/api/v1/zones"): BIN_MANAGE,
    ("GET", "/api/v1/zones"): BIN_READ,
    ("GET", "/api/v1/zones/{id}"): BIN_READ,
    ("PATCH", "/api/v1/zones/{id}"): BIN_MANAGE,
    ("POST", "/api/v1/zones/{id}/inactivate"): BIN_MANAGE,
    ("POST", "/api/v1/bins"): BIN_MANAGE,
    ("GET", "/api/v1/bins"): BIN_READ,
    ("GET", "/api/v1/bins/{id}"): BIN_READ,
    ("PATCH", "/api/v1/bins/{id}"): BIN_MANAGE,
    ("POST", "/api/v1/bins/{id}/activate"): BIN_MANAGE,
    ("POST", "/api/v1/bins/{id}/inactivate"): BIN_MANAGE,
    ("POST", "/api/v1/inventory-statuses"): STATUS_MANAGE,
    ("GET", "/api/v1/inventory-statuses"): STATUS_READ,
    ("GET", "/api/v1/inventory-statuses/{id}"): STATUS_READ,
    ("PATCH", "/api/v1/inventory-statuses/{id}"): STATUS_MANAGE,
    ("POST", "/api/v1/inventory-statuses/{id}/activate"): STATUS_MANAGE,
    ("POST", "/api/v1/inventory-statuses/{id}/inactivate"): STATUS_MANAGE,
    ("POST", "/api/v1/inventory-statuses/{id}/set-default"): STATUS_MANAGE,
    ("GET", "/api/v1/traceability/bootstrap"): LOCATION_READ,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
DomainFlag = "traceability.registry.enabled"


class InactivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class LifecycleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None

    # --------------------------------------------------------------------------- dependencies ------


def _service(db: DbDep) -> TraceabilityService:
    return TraceabilityService(db)


ServiceDep = Annotated[TraceabilityService, Depends(_service)]


async def _domain_enabled(db: DbDep, principal: PrincipalDep) -> None:
    table = platform_models.metadata.tables["platform.feature_flag"]
    result = await db.execute(
        select(table.c.is_enabled).where(table.c.tenant_id == principal.tenant_id, table.c.flag_key == DomainFlag)
    )
    enabled = result.scalar_one_or_none()
    if enabled is None or not enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOMAIN_DISABLED",
                "message": "traceability.registry.enabled is off for this tenant",
                "details": [],
            },
        )


Gate = Annotated[None, Depends(_domain_enabled)]
LocationRead = Annotated[object, Depends(require_scope(LOCATION_READ))]
LocationManager = Annotated[object, Depends(require_scope(LOCATION_MANAGE))]
BinRead = Annotated[object, Depends(require_scope(BIN_READ))]
BinManager = Annotated[object, Depends(require_scope(BIN_MANAGE))]
StatusRead = Annotated[object, Depends(require_scope(STATUS_READ))]
StatusManager = Annotated[object, Depends(require_scope(STATUS_MANAGE))]


# --------------------------------------------------------------------------- helpers ----------
def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, TraceabilityError):
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


def _version(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "If-Match must be an integer version", "details": []},
        ) from None


def _page(items: list[dict[str, Any]], *, limit: int, offset: int) -> dict[str, Any]:
    next_cursor = str(offset + len(items)) if len(items) >= limit else None
    return {"items": items, "next_cursor": next_cursor}


async def _idempotent(
    *,
    db: DbDep,
    principal: PrincipalDep,
    endpoint_id: str,
    key: str,
    method: str,
    path: str,
    body: Any,
    response: Response,
) -> tuple[IdempotencyStore, dict[str, Any] | None]:
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
    decision.raise_for_conflict()
    if decision.replay and decision.record is not None:
        response.status_code = int(decision.record.response_status or 200)
        return store, dict(decision.record.response_body or {})
    return store, None


async def _finish(
    store: IdempotencyStore,
    *,
    principal: PrincipalDep,
    endpoint_id: str,
    key: str,
    status: int,
    body: dict[str, Any],
    resource_id: Any = None,
) -> dict[str, Any]:
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        response_status=status,
        response_body=body,
        resource_id=str(resource_id) if resource_id is not None else None,
    )
    return body


async def _run_write(
    *,
    response: Response,
    db: DbDep,
    principal: PrincipalDep,
    endpoint_id: str,
    method: str,
    path: str,
    body: Any,
    key: str,
    action: Any,
    status: int = 200,
    envelope: str,
) -> dict[str, Any]:
    try:
        store, replay = await _idempotent(
            db=db,
            principal=principal,
            endpoint_id=endpoint_id,
            key=key,
            method=method,
            path=path,
            body=body,
            response=response,
        )
    except Exception:
        if hasattr(action, "close"):
            action.close()
        raise
    if replay is not None:
        if hasattr(action, "close"):
            action.close()
        return replay
    try:
        result = await action
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    payload = {envelope: result, "version": result.get("version")} if envelope else result
    if isinstance(result, dict) and result.get("warnings"):
        payload["warnings"] = result["warnings"]
    resource_id = None
    if isinstance(result, dict):
        resource_id = result.get("id")
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint_id,
        key=key,
        status=status,
        body=payload,
        resource_id=resource_id,
    )

    # =========================================================================== locations =========


@router.post("/locations", status_code=201, dependencies=[Depends(require_scope(LOCATION_MANAGE))], response_model=TraceabilityResponse)
async def create_location(
    body: LocationCreate,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id="traceability:locations:create",
        method="POST",
        path="/api/v1/locations",
        body=body,
        key=idempotency_key,
        action=service.create_location(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="location",
    )


@router.get("/locations", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def list_locations(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    code: str | None = None,
    location_type: str | None = Query(default=None, alias="type"),
    parent_id: UUID | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    service = _service(db)
    items = await service.list_locations(
        principal.tenant_id,
        code=code,
        location_type=location_type.upper() if location_type else None,
        parent_id=parent_id,
        is_inactive=None if active is None else not active,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


@router.get("/locations/{id}", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def get_location(id: str, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    service = _service(db)
    try:
        location = await service.get_location(principal.tenant_id, id)
        resolved_flags = {
            "bins_enabled_global": await service.bins_enabled_for_tenant(principal.tenant_id),
            "statuses_enabled_global": await service.statuses_enabled_for_tenant(principal.tenant_id),
            "can_post": not location["is_inactive"],
        }
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"location": location, "resolved_flags": resolved_flags}


@router.patch("/locations/{id}", dependencies=[Depends(require_scope(LOCATION_MANAGE))], response_model=TraceabilityResponse)
async def patch_location(
    id: str, body: LocationUpdate, db: DbDep, principal: PrincipalDep, gate: Gate, if_match: IfMatch = None
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    service = _service(db)
    try:
        location = await service.update_location(
            tenant_id=principal.tenant_id,
            location_id=id,
            values=body,
            actor_id=principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"location": location}


@router.post("/locations/{id}/activate", dependencies=[Depends(require_scope(LOCATION_MANAGE))], response_model=TraceabilityResponse)
async def activate_location(
    id: str,
    body: LifecycleIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:locations:{id}:activate",
        method="POST",
        path=f"/api/v1/locations/{id}/activate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.activate_location(tenant_id=principal.tenant_id, location_id=id, actor_id=principal.identity_id),
        envelope="location",
    )


@router.post("/locations/{id}/inactivate", dependencies=[Depends(require_scope(LOCATION_MANAGE))], response_model=TraceabilityResponse)
async def inactivate_location(
    id: str,
    body: InactivateIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:locations:{id}:inactivate",
        method="POST",
        path=f"/api/v1/locations/{id}/inactivate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.inactivate_location(
            tenant_id=principal.tenant_id, location_id=id, reason=body.reason, actor_id=principal.identity_id
        ),
        envelope="location",
    )


@router.get("/locations/{id}/children", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def location_children(
    id: str, db: DbDep, principal: PrincipalDep, gate: Gate, active: bool | None = None
) -> dict[str, Any]:
    del gate
    service = _service(db)
    try:
        items = await service.children(
            principal.tenant_id, id, active_only=bool(active) if active is not None else False
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"items": items}


@router.get("/locations/{id}/descendants", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def location_descendants(
    id: str,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    max_depth: int = Query(default=10, ge=1, le=64),
    active: bool | None = None,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    try:
        items = await service.descendants(principal.tenant_id, id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if active is not None:
        items = [item for item in items if bool(item["is_inactive"]) != bool(active)]
    items = [item for item in items if int(item["depth"]) <= max_depth]
    return {"items": items, "depth": max((int(item["depth"]) for item in items), default=0)}


@router.get("/locations/{id}/bins", dependencies=[Depends(require_scope(BIN_READ))], response_model=TraceabilityResponse)
async def location_bins(
    id: str,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    zone_id: UUID | None = None,
    clickable_only: bool = False,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    service = _service(db)
    try:
        items = await service.location_bins(
            principal.tenant_id,
            id,
            zone_id=zone_id,
            is_active=None if active is None else active,
            pickable_only=clickable_only,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return _page(items, limit=limit, offset=offset)


@router.get("/locations/{id}/posting-eligibility", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def posting_eligibility(
    id: str,
    item_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    bin_id: UUID | None = None,
    inventory_status_id: UUID | None = None,
    ownership: str = Query(default="OWNED"),
    qty_class: str = Query(default="ON_HAND"),
) -> dict[str, Any]:
    del gate
    service = _service(db)
    resolved_bin = bin_id
    if resolved_bin is None:
        # accept bin from the item-location default when caller only targets a location
        default_bin = None
        try:
            flags = await service.repo.item_location_default(principal.tenant_id, item_id)
            default_bin = flags.get("default_bin_id") if flags else None
        except Exception:  # noqa: BLE001
            default_bin = None
        resolved_bin = UUID(str(default_bin)) if default_bin else None
    try:
        result = await service.posting_eligibility(
            principal.tenant_id,
            item_id=str(item_id),
            bin_id=str(resolved_bin) if resolved_bin else None,
            inventory_status_id=str(inventory_status_id) if inventory_status_id else None,
            ownership=ownership,
            qty_class=qty_class,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    result["requested_location_id"] = id
    try:
        location = await service.get_location(principal.tenant_id, id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if str(location["id"]) != str(result["location_id"]):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LOCATION_MISMATCH",
                "message": "resolved bin does not belong to the requested location",
                "details": {"requested_location_id": id, "resolved_location_id": result["location_id"]},
            },
        )
    return result

    # =========================================================================== zones =============


@router.post("/zones", status_code=201, dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def create_zone(
    body: TraceabilityRequest,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id="traceability:zones:create",
        method="POST",
        path="/api/v1/zones",
        body=body,
        key=idempotency_key,
        action=service.create_zone(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="zone",
    )


@router.get("/zones", dependencies=[Depends(require_scope(BIN_READ))], response_model=TraceabilityResponse)
async def list_zones(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    location_id: UUID | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    service = _service(db)
    items = await service.list_zones(principal.tenant_id, location_id=location_id, is_active=active, limit=limit)
    return _page(items, limit=limit, offset=offset)


@router.get("/zones/{id}", dependencies=[Depends(require_scope(BIN_READ))], response_model=TraceabilityResponse)
async def get_zone(id: str, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        zone = await _service(db).get_zone(principal.tenant_id, id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"zone": zone}


@router.patch("/zones/{id}", dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def patch_zone(
    id: str, body: TraceabilityRequest, db: DbDep, principal: PrincipalDep, gate: Gate, if_match: IfMatch = None
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    try:
        zone = await _service(db).update_zone(
            tenant_id=principal.tenant_id,
            zone_id=id,
            values=body,
            actor_id=principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"zone": zone}


@router.post("/zones/{id}/inactivate", dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def inactivate_zone(
    id: str,
    body: LifecycleIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:zones:{id}:inactivate",
        method="POST",
        path=f"/api/v1/zones/{id}/inactivate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.inactivate_zone(tenant_id=principal.tenant_id, zone_id=id, actor_id=principal.identity_id),
        envelope="zone",
    )

    # =========================================================================== bins ==============


@router.post("/bins", status_code=201, dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def create_bin(
    body: TraceabilityRequest,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id="traceability:bins:create",
        method="POST",
        path="/api/v1/bins",
        body=body,
        key=idempotency_key,
        action=service.create_bin(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="bin",
    )


@router.get("/bins", dependencies=[Depends(require_scope(BIN_READ))], response_model=TraceabilityResponse)
async def list_bins(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    location_id: UUID | None = None,
    zone_id: UUID | None = None,
    bin_type: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_bins(
        principal.tenant_id,
        location_id=location_id,
        zone_id=zone_id,
        bin_type=bin_type.upper() if bin_type else None,
        is_active=active,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


@router.get("/bins/{id}", dependencies=[Depends(require_scope(BIN_READ))], response_model=TraceabilityResponse)
async def get_bin(id: str, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        bin_row = await _service(db).get_bin(principal.tenant_id, id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"bin": bin_row}


@router.patch("/bins/{id}", dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def patch_bin(
    id: str, body: TraceabilityRequest, db: DbDep, principal: PrincipalDep, gate: Gate, if_match: IfMatch = None
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    try:
        bin_row = await _service(db).update_bin(
            tenant_id=principal.tenant_id,
            bin_id=id,
            values=body,
            actor_id=principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"bin": bin_row}


@router.post("/bins/{id}/activate", dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def activate_bin(
    id: str,
    body: LifecycleIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:bins:{id}:activate",
        method="POST",
        path=f"/api/v1/bins/{id}/activate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.activate_bin(tenant_id=principal.tenant_id, bin_id=id, actor_id=principal.identity_id),
        envelope="bin",
    )


@router.post("/bins/{id}/inactivate", dependencies=[Depends(require_scope(BIN_MANAGE))], response_model=TraceabilityResponse)
async def inactivate_bin(
    id: str,
    body: InactivateIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:bins:{id}:inactivate",
        method="POST",
        path=f"/api/v1/bins/{id}/inactivate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.inactivate_bin(
            tenant_id=principal.tenant_id, bin_id=id, reason=body.reason, actor_id=principal.identity_id
        ),
        envelope="bin",
    )

    # =========================================================================== statuses ==========


@router.post("/inventory-statuses", status_code=201, dependencies=[Depends(require_scope(STATUS_MANAGE))], response_model=TraceabilityResponse)
async def create_status(
    body: InventoryStatusCreate,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id="traceability:statuses:create",
        method="POST",
        path="/api/v1/inventory-statuses",
        body=body,
        key=idempotency_key,
        action=service.create_status(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="inventory_status",
    )


@router.get("/inventory-statuses", dependencies=[Depends(require_scope(STATUS_READ))], response_model=TraceabilityResponse)
async def list_statuses(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    active: bool | None = None,
    consigned: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_statuses(principal.tenant_id, is_active=active, limit=limit)
    if consigned is not None:
        items = [item for item in items if bool(item["is_consigned"]) == bool(consigned)]
    return _page(items, limit=limit, offset=offset)


@router.get("/inventory-statuses/{id}", dependencies=[Depends(require_scope(STATUS_READ))], response_model=TraceabilityResponse)
async def get_status(id: str, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        status = await _service(db).get_status(principal.tenant_id, id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"inventory_status": status}


@router.patch("/inventory-statuses/{id}", dependencies=[Depends(require_scope(STATUS_MANAGE))], response_model=TraceabilityResponse)
async def patch_status(
    id: str, body: InventoryStatusUpdate, db: DbDep, principal: PrincipalDep, gate: Gate, if_match: IfMatch = None
) -> dict[str, Any]:
    body = body.model_dump(exclude_unset=True)
    del gate
    try:
        status = await _service(db).update_status(
            tenant_id=principal.tenant_id,
            status_id=id,
            values=body,
            actor_id=principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"inventory_status": status}


@router.post("/inventory-statuses/{id}/activate", dependencies=[Depends(require_scope(STATUS_MANAGE))], response_model=TraceabilityResponse)
async def activate_status(
    id: str,
    body: LifecycleIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:statuses:{id}:activate",
        method="POST",
        path=f"/api/v1/inventory-statuses/{id}/activate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.activate_status(tenant_id=principal.tenant_id, status_id=id, actor_id=principal.identity_id),
        envelope="inventory_status",
    )


@router.post("/inventory-statuses/{id}/inactivate", dependencies=[Depends(require_scope(STATUS_MANAGE))], response_model=TraceabilityResponse)
async def inactivate_status(
    id: str,
    body: InactivateIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:statuses:{id}:inactivate",
        method="POST",
        path=f"/api/v1/inventory-statuses/{id}/inactivate",
        body=body.model_dump(),
        key=idempotency_key,
        action=service.inactivate_status(
            tenant_id=principal.tenant_id, status_id=id, reason=body.reason, actor_id=principal.identity_id
        ),
        envelope="inventory_status",
    )


@router.post("/inventory-statuses/{id}/set-default", dependencies=[Depends(require_scope(STATUS_MANAGE))], response_model=TraceabilityResponse)
async def set_default_status(
    id: str, db: DbDep, principal: PrincipalDep, response: Response, gate: Gate, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    del gate
    service = _service(db)
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability:statuses:{id}:set-default",
        method="POST",
        path=f"/api/v1/inventory-statuses/{id}/set-default",
        body={},
        key=idempotency_key,
        action=service.set_default_status(tenant_id=principal.tenant_id, status_id=id, actor_id=principal.identity_id),
        envelope="inventory_status",
    )

    # =========================================================================== bootstrap =========


@router.get("/traceability/bootstrap", dependencies=[Depends(require_scope(LOCATION_READ))], response_model=TraceabilityResponse)
async def bootstrap_state(db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        return await _service(db).bootstrap_state(principal.tenant_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


__all__ = ["ROUTE_SCOPES", "router"]
