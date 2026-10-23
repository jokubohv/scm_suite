"""FS-02 pricing engine API (64 routes, `/api/v1`, catalogued scopes, tenant isolated, idempotent).

The router is a thin transport layer over :class:`app.modules.pricing.service.PricingService`; business
rules live in the service/engine. Every write endpoint requires ``Idempotency-Key`` (durable
``platform.idempotency_key`` replay via ``app.core.idempotency_store``), every route requires exactly one
catalogued scope, and the deploy-dark flag ``pricing.engine.enabled`` gates the whole surface.

Router registration is integrator-owned (``app/api/main.py``/``registry.py``); this module exposes
``router`` and ``ROUTE_SCOPES`` only.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.models import platform as platform_models
from app.modules.pricing import tables as t
from app.modules.pricing.service import PricingService, PricingServiceError

router = APIRouter(prefix="/api/v1", tags=["pricing"])

PRICING_READ = "pricing.read"
LEVEL_MANAGE = "pricing.level.manage"
PRICE_MANAGE = "pricing.price.manage"
SCHEDULE_MANAGE = "pricing.schedule.manage"
RULE_MANAGE = "pricing.rule.manage"
COLLECTION_MANAGE = "pricing.collection.manage"
COUPON_MANAGE = "pricing.coupon.manage"
UPDATE_MANAGE = "pricing.update.manage"
PACKAGE_MASS_UPDATE = "pricing.massupdate"
PRICING_RESOLVE = "pricing.resolve"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/price-levels"): LEVEL_MANAGE,
    ("GET", "/api/v1/price-levels"): PRICING_READ,
    ("GET", "/api/v1/price-levels/{price_level_id}"): PRICING_READ,
    ("PATCH", "/api/v1/price-levels/{price_level_id}"): LEVEL_MANAGE,
    ("POST", "/api/v1/item-groups"): LEVEL_MANAGE,
    ("GET", "/api/v1/item-groups"): PRICING_READ,
    ("GET", "/api/v1/item-groups/{item_group_id}"): PRICING_READ,
    ("PATCH", "/api/v1/item-groups/{item_group_id}"): LEVEL_MANAGE,
    ("GET", "/api/v1/item-groups/{item_group_id}/members"): PRICING_READ,
    ("POST", "/api/v1/item-groups/{item_group_id}/members"): LEVEL_MANAGE,
    ("DELETE", "/api/v1/item-groups/{item_group_id}/members/{item_id}"): LEVEL_MANAGE,
    ("POST", "/api/v1/customer-groups"): LEVEL_MANAGE,
    ("GET", "/api/v1/customer-groups"): PRICING_READ,
    ("GET", "/api/v1/customer-groups/{customer_group_id}"): PRICING_READ,
    ("PATCH", "/api/v1/customer-groups/{customer_group_id}"): LEVEL_MANAGE,
    ("GET", "/api/v1/customer-groups/{customer_group_id}/members"): PRICING_READ,
    ("POST", "/api/v1/customer-groups/{customer_group_id}/members"): LEVEL_MANAGE,
    ("DELETE", "/api/v1/customer-groups/{customer_group_id}/members/{external_customer_ref}"): LEVEL_MANAGE,
    ("POST", "/api/v1/pricing-groups"): LEVEL_MANAGE,
    ("GET", "/api/v1/pricing-groups"): PRICING_READ,
    ("GET", "/api/v1/pricing-groups/{pricing_group_id}"): PRICING_READ,
    ("PATCH", "/api/v1/pricing-groups/{pricing_group_id}"): LEVEL_MANAGE,
    ("POST", "/api/v1/item-prices"): PRICE_MANAGE,
    ("GET", "/api/v1/item-prices"): PRICING_READ,
    ("GET", "/api/v1/item-prices/{item_price_id}"): PRICING_READ,
    ("PATCH", "/api/v1/item-prices/{item_price_id}"): PRICE_MANAGE,
    ("POST", "/api/v1/item-prices/bulk-upsert"): PRICE_MANAGE,
    ("POST", "/api/v1/item-prices/swap-levels"): PACKAGE_MASS_UPDATE,
    ("POST", "/api/v1/item-prices/purchase-price-updates"): PACKAGE_MASS_UPDATE,
    ("POST", "/api/v1/price-schedules"): SCHEDULE_MANAGE,
    ("GET", "/api/v1/price-schedules"): PRICING_READ,
    ("GET", "/api/v1/price-schedules/{price_schedule_id}"): PRICING_READ,
    ("PATCH", "/api/v1/price-schedules/{price_schedule_id}"): SCHEDULE_MANAGE,
    ("GET", "/api/v1/price-schedules/{price_schedule_id}/breaks"): PRICING_READ,
    ("PUT", "/api/v1/price-schedules/{price_schedule_id}/breaks"): SCHEDULE_MANAGE,
    ("POST", "/api/v1/price-rules"): RULE_MANAGE,
    ("GET", "/api/v1/price-rules"): PRICING_READ,
    ("GET", "/api/v1/price-rules/{price_rule_id}"): PRICING_READ,
    ("PATCH", "/api/v1/price-rules/{price_rule_id}"): RULE_MANAGE,
    ("PUT", "/api/v1/price-rules/{price_rule_id}/criteria"): RULE_MANAGE,
    ("PUT", "/api/v1/price-rules/{price_rule_id}/actions"): RULE_MANAGE,
    ("POST", "/api/v1/item-collections"): COLLECTION_MANAGE,
    ("GET", "/api/v1/item-collections"): PRICING_READ,
    ("GET", "/api/v1/item-collections/{collection_id}"): PRICING_READ,
    ("PATCH", "/api/v1/item-collections/{collection_id}"): COLLECTION_MANAGE,
    ("GET", "/api/v1/item-collections/{collection_id}/members"): PRICING_READ,
    ("POST", "/api/v1/item-collections/{collection_id}/members"): COLLECTION_MANAGE,
    ("DELETE", "/api/v1/item-collections/{collection_id}/members/{item_id}"): COLLECTION_MANAGE,
    ("POST", "/api/v1/item-collections/{collection_id}/recompute"): COLLECTION_MANAGE,
    ("POST", "/api/v1/coupons"): COUPON_MANAGE,
    ("GET", "/api/v1/coupons"): PRICING_READ,
    ("GET", "/api/v1/coupons/{coupon_id}"): PRICING_READ,
    ("PATCH", "/api/v1/coupons/{coupon_id}"): COUPON_MANAGE,
    ("POST", "/api/v1/coupons/validate"): PRICING_RESOLVE,
    ("POST", "/api/v1/price-updates"): UPDATE_MANAGE,
    ("GET", "/api/v1/price-updates"): PRICING_READ,
    ("GET", "/api/v1/price-updates/{price_update_id}"): PRICING_READ,
    ("PATCH", "/api/v1/price-updates/{price_update_id}"): UPDATE_MANAGE,
    ("POST", "/api/v1/price-updates/{price_update_id}/apply"): UPDATE_MANAGE,
    ("POST", "/api/v1/price-updates/{price_update_id}/cancel"): UPDATE_MANAGE,
    ("POST", "/api/v1/price-updates/{price_update_id}/revert"): UPDATE_MANAGE,
    ("POST", "/api/v1/prices/resolve"): PRICING_RESOLVE,
    ("GET", "/api/v1/prices/effective"): PRICING_RESOLVE,
    ("POST", "/api/v1/price-lists/generate"): PRICING_READ,
}

SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


# --------------------------------------------------------------------------- dependencies ------
def _service(db: DbDep) -> PricingService:
    return PricingService(db)


ServiceDep = Annotated[PricingService, Depends(_service)]


async def _engine_enabled(db: DbDep, principal: PrincipalDep) -> None:
    table = platform_models.metadata.tables["platform.feature_flag"]
    result = await db.execute(
        select(table.c.is_enabled).where(
            table.c.tenant_id == principal.tenant_id, table.c.flag_key == "pricing.engine.enabled"
        )
    )
    enabled = result.scalar_one_or_none()
    if enabled is None or not enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PRICING_ENGINE_DISABLED",
                "message": "pricing.engine.enabled is off for this tenant",
                "details": [],
            },
        )


EngineGate = Annotated[None, Depends(_engine_enabled)]
Read = Annotated[object, Depends(require_scope(PRICING_READ))]
LevelManager = Annotated[object, Depends(require_scope(LEVEL_MANAGE))]
PriceManager = Annotated[object, Depends(require_scope(PRICE_MANAGE))]
ScheduleManager = Annotated[object, Depends(require_scope(SCHEDULE_MANAGE))]
RuleManager = Annotated[object, Depends(require_scope(RULE_MANAGE))]
CollectionManager = Annotated[object, Depends(require_scope(COLLECTION_MANAGE))]
CouponManager = Annotated[object, Depends(require_scope(COUPON_MANAGE))]
UpdateManager = Annotated[object, Depends(require_scope(UPDATE_MANAGE))]
MassUpdater = Annotated[object, Depends(require_scope(PACKAGE_MASS_UPDATE))]
Resolver = Annotated[object, Depends(require_scope(PRICING_RESOLVE))]


# --------------------------------------------------------------------------- helpers ----------
def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, PricingServiceError):
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "details": exc.details}
        ) from exc
    if isinstance(exc, Exception) and hasattr(exc, "status_code") and hasattr(exc, "code"):
        raise HTTPException(
            status_code=int(exc.status_code),
            detail={
                "code": str(exc.code),
                "message": str(getattr(exc, "message", "")),
                "details": dict(getattr(exc, "details", {}) or {}),
            },
        ) from exc
    raise exc


def _not_found(code: str = "NOT_FOUND", message: str = "resource not found") -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message, "details": []})


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


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


# --------------------------------------------------------------------------- schemas ----------
class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    external_customer_ref: str | None = None
    customer_group_id: UUID | None = None
    price_level_id: UUID | None = None
    currency_code: str | None = None
    document_date: date | None = None
    coupon_code: str | None = None
    cost_context: dict[str, Any] | None = None
    include_trace: bool | None = None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class BulkUpsertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_from: date
    effective_to: date | None = None
    price_level_id: UUID | None = None
    currency_code: str
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=5000)


router.dependencies.append(Depends(_engine_enabled))


class CouponValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    item_id: UUID | None = None
    quantity: str | None = None
    unit_price: str | None = None
    currency_code: str | None = None
    document_date: date | None = None


# --------------------------------------------------------------------------- price levels -----
@router.post("/price-levels", status_code=201, dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def create_price_level(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:price-levels:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/price-levels",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_price_level(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001 - typed mapping below
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"price_level": row},
        resource_id=row["id"],
    )


@router.get("/price-levels", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_price_levels(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_inactive: bool | None = None,
    currency_code: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if is_inactive is not None:
        filters["is_inactive"] = is_inactive
    if currency_code:
        filters["currency_code"] = currency_code.upper()
    items = await _service(db).list_master(t.price_level, principal.tenant_id, filters=filters, limit=_limit(limit))
    return {"items": items}


@router.get("/price-levels/{price_level_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_price_level(price_level_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db).get_master(t.price_level, principal.tenant_id, price_level_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/price-levels/{price_level_id}", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def patch_price_level(
    price_level_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: LevelManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    try:
        return {
            "price_level": await _service(db).update_price_level(
                tenant_id=principal.tenant_id,
                row_id=price_level_id,
                values=body,
                actor_id=principal.identity_id,
                expected_version=_version(if_match),
            )
        }
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# --------------------------------------------------------------------------- item groups -----
@router.post("/item-groups", status_code=201, dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def create_item_group(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-groups:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-groups",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_item_group(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"item_group": row},
        resource_id=row["id"],
    )


@router.get("/item-groups", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_item_groups(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_inactive: bool | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "items": await _service(db).list_master(
            t.item_group,
            principal.tenant_id,
            filters={"is_inactive": is_inactive} if is_inactive is not None else None,
            limit=_limit(limit),
        )
    }


@router.get("/item-groups/{item_group_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_item_group(item_group_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    service = _service(db)
    try:
        group = await service.get_master(t.item_group, principal.tenant_id, item_group_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    members = await service.repo.list_rows(
        t.item_group_member, principal.tenant_id, filters={"item_group_id": UUID(item_group_id)}, limit=500
    )
    return {"item_group": group, "items": members}


@router.patch("/item-groups/{item_group_id}", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def patch_item_group(
    item_group_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: LevelManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    try:
        await service.get_master(t.item_group, principal.tenant_id, item_group_id)
        row = await service.repo.update_row(
            t.item_group,
            principal.tenant_id,
            item_group_id,
            {key: body[key] for key in ("name", "is_inactive") if key in body},
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"item_group": row}


@router.get("/item-groups/{item_group_id}/members", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_item_group_members(item_group_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    items = await _service(db).repo.list_rows(
        t.item_group_member, principal.tenant_id, filters={"item_group_id": UUID(item_group_id)}, limit=500
    )
    return {"items": items}


@router.post("/item-groups/{item_group_id}/members", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def add_item_group_members(
    item_group_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:item-groups:{item_group_id}:members"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/item-groups/{item_group_id}/members",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    added, rejected = [], []
    service = _service(db)
    for item_id in body.get("item_ids") or []:
        try:
            await service.add_item_group_member(
                tenant_id=principal.tenant_id,
                group_id=item_group_id,
                item_id=str(item_id),
                actor_id=principal.identity_id,
            )
            added.append(str(item_id))
        except Exception as exc:  # noqa: BLE001
            rejected.append({"item_id": str(item_id), "code": getattr(exc, "code", "VALIDATION_FAILED")})
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body={"added": added, "rejected": rejected},
        resource_id=None,
    )


@router.delete(
    "/item-groups/{item_group_id}/members/{item_id}",
    status_code=204,
    dependencies=[Depends(require_scope(LEVEL_MANAGE))],
)
async def remove_item_group_member(
    item_group_id: str, item_id: str, db: DbDep, principal: PrincipalDep, _: LevelManager
) -> None:
    service = _service(db)
    deleted = await service.repo.delete_row(t.item_group_member, principal.tenant_id, None) if False else None
    del deleted
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(t.item_group_member).where(
            t.item_group_member.c.tenant_id == UUID(principal.tenant_id),
            t.item_group_member.c.item_group_id == UUID(item_group_id),
            t.item_group_member.c.item_id == UUID(item_id),
        )
    )
    return None


# --------------------------------------------------------------------------- customer groups --
@router.post("/customer-groups", status_code=201, dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def create_customer_group(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:customer-groups:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/customer-groups",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_customer_group(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"customer_group": row},
        resource_id=row["id"],
    )


@router.get("/customer-groups", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_customer_groups(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_inactive: bool | None = None,
    external_customer_ref: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    service = _service(db)
    filters: dict[str, Any] = {}
    if is_inactive is not None:
        filters["is_inactive"] = is_inactive
    if external_customer_ref:
        member_rows = await service.repo.list_rows(
            t.customer_group_member,
            principal.tenant_id,
            filters={"external_customer_ref": external_customer_ref},
            limit=500,
        )
        ids = {row["customer_group_id"] for row in member_rows}
        items = [
            row
            for row in await service.list_master(
                t.customer_group, principal.tenant_id, filters=filters, limit=_limit(limit)
            )
            if row["id"] in ids
        ]
    else:
        items = await service.list_master(t.customer_group, principal.tenant_id, filters=filters, limit=_limit(limit))
    return {"items": items}


@router.get("/customer-groups/{customer_group_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_customer_group(customer_group_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    service = _service(db)
    try:
        group = await service.get_master(t.customer_group, principal.tenant_id, customer_group_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    members = await service.repo.list_rows(
        t.customer_group_member, principal.tenant_id, filters={"customer_group_id": UUID(customer_group_id)}, limit=500
    )
    return {"customer_group": group, "members": members}


@router.patch("/customer-groups/{customer_group_id}", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def patch_customer_group(
    customer_group_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: LevelManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    try:
        await service.get_master(t.customer_group, principal.tenant_id, customer_group_id)
        row = await service.repo.update_row(
            t.customer_group,
            principal.tenant_id,
            customer_group_id,
            {key: body[key] for key in ("name", "notes", "is_inactive") if key in body},
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"customer_group": row}


@router.get("/customer-groups/{customer_group_id}/members", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_customer_group_members(
    customer_group_id: str, db: DbDep, principal: PrincipalDep, _: Read
) -> dict[str, Any]:
    return {
        "items": await _service(db).repo.list_rows(
            t.customer_group_member,
            principal.tenant_id,
            filters={"customer_group_id": UUID(customer_group_id)},
            limit=500,
        )
    }


@router.post("/customer-groups/{customer_group_id}/members", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def add_customer_group_members(
    customer_group_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:customer-groups:{customer_group_id}:members"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/customer-groups/{customer_group_id}/members",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    service = _service(db)
    added, rejected = [], []
    for ref in body.get("external_customer_refs") or []:
        try:
            await service.add_customer_group_member(
                tenant_id=principal.tenant_id,
                group_id=customer_group_id,
                external_ref=str(ref),
                actor_id=principal.identity_id,
            )
            added.append(str(ref))
        except Exception as exc:  # noqa: BLE001
            rejected.append({"external_customer_ref": str(ref), "code": getattr(exc, "code", "VALIDATION_FAILED")})
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body={"added": added, "rejected": rejected},
    )


@router.delete(
    "/customer-groups/{customer_group_id}/members/{external_customer_ref}",
    status_code=204,
    dependencies=[Depends(require_scope(LEVEL_MANAGE))],
)
async def remove_customer_group_member(
    customer_group_id: str, external_customer_ref: str, db: DbDep, principal: PrincipalDep, _: LevelManager
) -> None:
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(t.customer_group_member).where(
            t.customer_group_member.c.tenant_id == UUID(principal.tenant_id),
            t.customer_group_member.c.customer_group_id == UUID(customer_group_id),
            t.customer_group_member.c.external_customer_ref == external_customer_ref,
        )
    )
    return None


# --------------------------------------------------------------------------- pricing groups ---
@router.post("/pricing-groups", status_code=201, dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def create_pricing_group(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: LevelManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:pricing-groups:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/pricing-groups",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_pricing_group(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"pricing_group": row},
        resource_id=row["id"],
    )


@router.get("/pricing-groups", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_pricing_groups(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    item_group_id: UUID | None = None,
    customer_group_id: UUID | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters = {}
    if item_group_id is not None:
        filters["item_group_id"] = item_group_id
    if customer_group_id is not None:
        filters["customer_group_id"] = customer_group_id
    return {
        "items": await _service(db).list_master(
            t.pricing_group, principal.tenant_id, filters=filters or None, limit=_limit(limit)
        )
    }


@router.get("/pricing-groups/{pricing_group_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_pricing_group(pricing_group_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db).get_master(t.pricing_group, principal.tenant_id, pricing_group_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/pricing-groups/{pricing_group_id}", dependencies=[Depends(require_scope(LEVEL_MANAGE))])
async def patch_pricing_group(
    pricing_group_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: LevelManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    values: dict[str, Any] = {}
    for key in ("price_level_id", "is_default"):
        if key in body:
            values[key] = UUID(body[key]) if key.endswith("_id") and body[key] else body[key]
    if "discount_percent" in body:
        values["discount_percent"] = body["discount_percent"]
    if "inactive" in body:
        values["is_default"] = False
    try:
        await service.get_master(t.pricing_group, principal.tenant_id, pricing_group_id)
        row = await service.repo.update_row(
            t.pricing_group,
            principal.tenant_id,
            pricing_group_id,
            values,
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"pricing_group": row}


# --------------------------------------------------------------------------- item prices ------
@router.post("/item-prices", status_code=201, dependencies=[Depends(require_scope(PRICE_MANAGE))])
async def create_item_price(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: PriceManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-prices:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-prices",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_item_price(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"item_price": row},
        resource_id=row["id"],
    )


@router.get("/item-prices", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_item_prices(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    item_id: UUID | None = None,
    price_level_id: UUID | None = None,
    currency_code: str | None = None,
    as_of: date | None = None,
    active: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    service = _service(db)
    filters: dict[str, Any] = {}
    if item_id is not None:
        filters["item_id"] = item_id
    if price_level_id is not None:
        filters["price_level_id"] = price_level_id
    if currency_code:
        filters["currency_code"] = currency_code.upper()
    if active is not None:
        filters["is_active"] = active
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    rows = await service.repo.list_rows(
        t.item_price,
        principal.tenant_id,
        filters=filters or None,
        order_by=(t.item_price.c.item_id, t.item_price.c.qty_min),
        limit=_limit(limit) + 1,
        offset=offset,
    )
    next_cursor = str(offset + _limit(limit)) if len(rows) > _limit(limit) else None
    rows = rows[: _limit(limit)]
    if as_of is not None:
        rows = [
            row
            for row in rows
            if row["effective_from"] <= as_of.isoformat()
            and (row["effective_to"] is None or row["effective_to"] >= as_of.isoformat())
        ]
    return {"items": rows, "next_cursor": next_cursor}


@router.get("/item-prices/{item_price_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_item_price(item_price_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db).get_master(t.item_price, principal.tenant_id, item_price_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/item-prices/{item_price_id}", dependencies=[Depends(require_scope(PRICE_MANAGE))])
async def patch_item_price(
    item_price_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: PriceManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    try:
        return {
            "item_price": await _service(db).update_item_price(
                tenant_id=principal.tenant_id,
                row_id=item_price_id,
                values=body,
                actor_id=principal.identity_id,
                expected_version=_version(if_match),
            )
        }
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/item-prices/bulk-upsert", status_code=201, dependencies=[Depends(require_scope(PRICE_MANAGE))])
async def bulk_upsert_item_prices(
    body: BulkUpsertIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: PriceManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-prices:bulk-upsert"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-prices/bulk-upsert",
        body=body.model_dump(mode="json"),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).bulk_upsert_prices(
            tenant_id=principal.tenant_id, values=body.model_dump(), actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body=result,
        resource_id=result.get("price_update_id"),
    )


@router.post("/item-prices/swap-levels", status_code=202, dependencies=[Depends(require_scope(PACKAGE_MASS_UPDATE))])
async def swap_price_levels(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: MassUpdater,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-prices:swap-levels"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-prices/swap-levels",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        preview = await _service(db).repo.list_rows(
            t.item_price, principal.tenant_id, filters={"price_level_id": UUID(body.get("level_a_id"))}, limit=500
        )
        job = await _service(db).enqueue_job(
            tenant_id=principal.tenant_id,
            name="pricing.level_swap",
            payload={**body, "run_key": idempotency_key},
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=202,
        body={"job_run_id": job["job_run_id"], "preview_row_count": len(preview)},
        resource_id=job["job_run_id"],
    )


@router.post(
    "/item-prices/purchase-price-updates", status_code=202, dependencies=[Depends(require_scope(PACKAGE_MASS_UPDATE))]
)
async def purchase_price_updates(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: MassUpdater,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-prices:purchase-price-updates"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-prices/purchase-price-updates",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        job = await _service(db).enqueue_job(
            tenant_id=principal.tenant_id,
            name="pricing.purchase_price_update",
            payload={**body, "run_key": idempotency_key},
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=202,
        body={"job_run_id": job["job_run_id"]},
        resource_id=job["job_run_id"],
    )


# --------------------------------------------------------------------------- schedules -------
@router.post("/price-schedules", status_code=201, dependencies=[Depends(require_scope(SCHEDULE_MANAGE))])
async def create_price_schedule(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: ScheduleManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:price-schedules:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/price-schedules",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_schedule(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"price_schedule": row},
        resource_id=row["id"],
    )


@router.get("/price-schedules", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_price_schedules(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_inactive: bool | None = None,
    calculation_mode: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if is_inactive is not None:
        filters["is_inactive"] = is_inactive
    if calculation_mode:
        filters["calculation_mode"] = calculation_mode.upper()
    return {
        "items": await _service(db).list_master(
            t.price_schedule, principal.tenant_id, filters=filters or None, limit=_limit(limit)
        )
    }


@router.get("/price-schedules/{price_schedule_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_price_schedule(price_schedule_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    result = await _service(db).repo.schedule_with_breaks(principal.tenant_id, UUID(price_schedule_id))
    if result is None:
        raise _not_found()
    return result


@router.patch("/price-schedules/{price_schedule_id}", dependencies=[Depends(require_scope(SCHEDULE_MANAGE))])
async def patch_price_schedule(
    price_schedule_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: ScheduleManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    values = {
        key: (body[key].upper() if key == "calculation_mode" and body.get(key) else body[key])
        for key in ("name", "calculation_mode", "is_marginal", "is_inactive")
        if key in body
    }
    try:
        await service.get_master(t.price_schedule, principal.tenant_id, price_schedule_id)
        row = await service.repo.update_row(
            t.price_schedule,
            principal.tenant_id,
            price_schedule_id,
            values,
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"price_schedule": row}


@router.get("/price-schedules/{price_schedule_id}/breaks", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_schedule_breaks(price_schedule_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    return {
        "items": await _service(db).repo.list_rows(
            t.price_schedule_break,
            principal.tenant_id,
            filters={"price_schedule_id": UUID(price_schedule_id)},
            order_by=(t.price_schedule_break.c.qty_from,),
            limit=500,
        )
    }


@router.put("/price-schedules/{price_schedule_id}/breaks", dependencies=[Depends(require_scope(SCHEDULE_MANAGE))])
async def replace_schedule_breaks(
    price_schedule_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: ScheduleManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-schedules:{price_schedule_id}:breaks"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="PUT",
        path=f"/api/v1/price-schedules/{price_schedule_id}/breaks",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).replace_schedule_breaks(
            tenant_id=principal.tenant_id,
            schedule_id=price_schedule_id,
            breaks=body.get("breaks") or [],
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=result)


# --------------------------------------------------------------------------- rules -----------
@router.post("/price-rules", status_code=201, dependencies=[Depends(require_scope(RULE_MANAGE))])
async def create_price_rule(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: RuleManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:price-rules:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/price-rules",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).create_rule(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body=result,
        resource_id=result["price_rule"]["id"],
    )


@router.get("/price-rules", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_price_rules(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_active: bool | None = None,
    priority: int | None = None,
    starts_before: date | None = None,
    ends_after: date | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if is_active is not None:
        filters["is_active"] = is_active
    if priority is not None:
        filters["priority"] = priority
    rows = await _service(db).list_master(
        t.price_rule, principal.tenant_id, filters=filters or None, limit=_limit(limit)
    )
    if starts_before is not None:
        boundary = starts_before.isoformat()
        rows = [row for row in rows if row["start_date"] is not None and row["start_date"] <= boundary]
    if ends_after is not None:
        boundary = ends_after.isoformat()
        rows = [row for row in rows if row["end_date"] is None or row["end_date"] >= boundary]
    return {"items": rows}


@router.get("/price-rules/{price_rule_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_price_rule(price_rule_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    result = await _service(db).repo.rule_with_parts(principal.tenant_id, UUID(price_rule_id))
    if result is None:
        raise _not_found()
    return result


@router.patch("/price-rules/{price_rule_id}", dependencies=[Depends(require_scope(RULE_MANAGE))])
async def patch_price_rule(
    price_rule_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: RuleManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    try:
        return {
            "price_rule": await _service(db).update_rule(
                tenant_id=principal.tenant_id,
                rule_id=price_rule_id,
                values=body,
                actor_id=principal.identity_id,
                expected_version=_version(if_match),
            )
        }
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/price-rules/{price_rule_id}/criteria", dependencies=[Depends(require_scope(RULE_MANAGE))])
async def replace_rule_criteria(
    price_rule_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: RuleManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-rules:{price_rule_id}:criteria"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="PUT",
        path=f"/api/v1/price-rules/{price_rule_id}/criteria",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    service = _service(db)
    try:
        await service.get_master(t.price_rule, principal.tenant_id, price_rule_id)
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(t.price_rule_criteria).where(
                t.price_rule_criteria.c.tenant_id == UUID(principal.tenant_id),
                t.price_rule_criteria.c.price_rule_id == UUID(price_rule_id),
            )
        )
        inserted = []
        for raw in body.get("criteria") or []:
            payload = {
                "price_rule_id": UUID(price_rule_id),
                "criterion": str(raw.get("criterion")).upper(),
                "operator": str(raw.get("operator")).upper(),
                "value_text": raw.get("value_text"),
                "value_number": raw.get("value_number"),
                "value_date": raw.get("value_date"),
                "group_no": int(raw.get("group_no", 1)),
            }
            inserted.append(
                await service.repo.insert_row(
                    t.price_rule_criteria, principal.tenant_id, payload, principal.identity_id
                )
            )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body={"criteria": inserted}
    )


@router.put("/price-rules/{price_rule_id}/actions", dependencies=[Depends(require_scope(RULE_MANAGE))])
async def replace_rule_action(
    price_rule_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: RuleManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-rules:{price_rule_id}:actions"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="PUT",
        path=f"/api/v1/price-rules/{price_rule_id}/actions",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    service = _service(db)
    action = body.get("action") or {}
    try:
        await service.get_master(t.price_rule, principal.tenant_id, price_rule_id)
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(t.price_rule_action).where(
                t.price_rule_action.c.tenant_id == UUID(principal.tenant_id),
                t.price_rule_action.c.price_rule_id == UUID(price_rule_id),
            )
        )
        payload = {
            "price_rule_id": UUID(price_rule_id),
            "action_type": str(action.get("action_type")).upper(),
            "price_level_id": action.get("price_level_id"),
            "value": action.get("value"),
            "cost_basis": action.get("cost_basis"),
            "apply_to": str(action.get("apply_to") or "UNIT").upper(),
        }
        inserted = await service.repo.insert_row(
            t.price_rule_action, principal.tenant_id, payload, principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body={"action": inserted}
    )


# --------------------------------------------------------------------------- collections -----
@router.post("/item-collections", status_code=201, dependencies=[Depends(require_scope(COLLECTION_MANAGE))])
async def create_item_collection(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: CollectionManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:item-collections:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/item-collections",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_collection(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"item_collection": row},
        resource_id=row["id"],
    )


@router.get("/item-collections", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_item_collections(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_dynamic: bool | None = None,
    is_inactive: bool | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if is_dynamic is not None:
        filters["is_dynamic"] = is_dynamic
    if is_inactive is not None:
        filters["is_inactive"] = is_inactive
    return {
        "items": await _service(db).list_master(
            t.item_collection, principal.tenant_id, filters=filters or None, limit=_limit(limit)
        )
    }


@router.get("/item-collections/{collection_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_item_collection(collection_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    service = _service(db)
    try:
        collection = await service.get_master(t.item_collection, principal.tenant_id, collection_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    members = await service.repo.list_rows(
        t.item_collection_member, principal.tenant_id, filters={"item_collection_id": UUID(collection_id)}, limit=1000
    )
    return {"item_collection": collection, "members": members}


@router.patch("/item-collections/{collection_id}", dependencies=[Depends(require_scope(COLLECTION_MANAGE))])
async def patch_item_collection(
    collection_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: CollectionManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    try:
        await service.get_master(t.item_collection, principal.tenant_id, collection_id)
        values = {key: body[key] for key in ("name", "criteria", "is_inactive") if key in body}
        if "is_active" in body:
            values["is_inactive"] = not bool(body["is_active"])
        row = await service.repo.update_row(
            t.item_collection,
            principal.tenant_id,
            collection_id,
            values,
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"item_collection": row}


@router.get("/item-collections/{collection_id}/members", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_collection_members(collection_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    return {
        "items": await _service(db).repo.list_rows(
            t.item_collection_member,
            principal.tenant_id,
            filters={"item_collection_id": UUID(collection_id)},
            limit=1000,
        )
    }


@router.post("/item-collections/{collection_id}/members", dependencies=[Depends(require_scope(COLLECTION_MANAGE))])
async def add_collection_members(
    collection_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: CollectionManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:item-collections:{collection_id}:members"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/item-collections/{collection_id}/members",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).replace_collection_members(
            tenant_id=principal.tenant_id,
            collection_id=collection_id,
            item_ids=[str(item) for item in body.get("item_ids") or []],
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body={"added": result["members"], "rejected": []},
        resource_id=None,
    )


@router.delete(
    "/item-collections/{collection_id}/members/{item_id}",
    status_code=204,
    dependencies=[Depends(require_scope(COLLECTION_MANAGE))],
)
async def remove_collection_member(
    collection_id: str, item_id: str, db: DbDep, principal: PrincipalDep, _: CollectionManager
) -> None:
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(t.item_collection_member).where(
            t.item_collection_member.c.tenant_id == UUID(principal.tenant_id),
            t.item_collection_member.c.item_collection_id == UUID(collection_id),
            t.item_collection_member.c.item_id == UUID(item_id),
        )
    )
    return None


@router.post(
    "/item-collections/{collection_id}/recompute",
    status_code=202,
    dependencies=[Depends(require_scope(COLLECTION_MANAGE))],
)
async def recompute_collection(
    collection_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: CollectionManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:item-collections:{collection_id}:recompute"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/item-collections/{collection_id}/recompute",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        job = await _service(db).enqueue_job(
            tenant_id=principal.tenant_id,
            name="pricing.recompute_dynamic_collections",
            payload={**body, "collection_id": collection_id, "run_key": idempotency_key},
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=202,
        body={"job_run_id": job["job_run_id"]},
        resource_id=job["job_run_id"],
    )


# --------------------------------------------------------------------------- coupons ---------
@router.post("/coupons", status_code=201, dependencies=[Depends(require_scope(COUPON_MANAGE))])
async def create_coupon(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: CouponManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:coupons:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/coupons",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        row = await _service(db).create_coupon(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body={"coupon": row},
        resource_id=row["id"],
    )


@router.get("/coupons", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_coupons(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    is_active: bool | None = None,
    starts_before: date | None = None,
    ends_after: date | None = None,
    item_id: UUID | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if is_active is not None:
        filters["is_active"] = is_active
    if item_id is not None:
        filters["item_id"] = item_id
    rows = await _service(db).list_master(t.coupon, principal.tenant_id, filters=filters or None, limit=_limit(limit))
    if starts_before is not None:
        rows = [row for row in rows if row["start_date"] <= starts_before.isoformat()]
    if ends_after is not None:
        rows = [row for row in rows if row["end_date"] >= ends_after.isoformat()]
    return {"items": rows}


@router.get("/coupons/{coupon_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_coupon(coupon_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    service = _service(db)
    try:
        coupon = await service.get_master(t.coupon, principal.tenant_id, coupon_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    link_table = platform_models.metadata.tables["platform.document_link"]
    redemptions = await service.repo.list_rows(t.coupon, principal.tenant_id, limit=1)  # keep query cheap; usage below
    del redemptions
    links = await db.execute(
        select(link_table.c.to_type, link_table.c.to_id, link_table.c.amount_base).where(
            link_table.c.tenant_id == UUID(principal.tenant_id),
            link_table.c.from_type == "COUPON",
            link_table.c.from_id == UUID(coupon_id),
        )
    )
    return {
        "coupon": coupon,
        "uses_count": coupon["uses_count"],
        "redemptions": [dict(row) for row in links.mappings().all()],
    }


@router.patch("/coupons/{coupon_id}", dependencies=[Depends(require_scope(COUPON_MANAGE))])
async def patch_coupon(
    coupon_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, _: CouponManager, if_match: IfMatch = None
) -> dict[str, Any]:
    service = _service(db)
    try:
        await service.get_master(t.coupon, principal.tenant_id, coupon_id)
        values = {key: body[key] for key in ("name", "value", "end_date", "max_uses", "is_active") if key in body}
        row = await service.repo.update_row(
            t.coupon, principal.tenant_id, coupon_id, values, principal.identity_id, expected_version=_version(if_match)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if row is None:
        raise _not_found()
    return {"coupon": row}


@router.post("/coupons/validate", dependencies=[Depends(require_scope(PRICING_RESOLVE))])
async def validate_coupon(body: CouponValidateIn, db: DbDep, principal: PrincipalDep, _: Resolver) -> dict[str, Any]:
    service = _service(db)
    payload = {
        "coupon_code": body.code,
        "item_id": body.item_id,
        "quantity": body.quantity or "1",
        "unit_price": body.unit_price or "0",
        "currency_code": body.currency_code,
        "document_date": body.document_date,
    }
    if body.item_id is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "item_id is required for coupon validation", "details": []},
        )
    try:
        return await service.validate_coupon(tenant_id=principal.tenant_id, payload=payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# --------------------------------------------------------------------------- price updates ---
@router.post("/price-updates", status_code=201, dependencies=[Depends(require_scope(UPDATE_MANAGE))])
async def create_price_update(
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: UpdateManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "pricing:price-updates:create"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path="/api/v1/price-updates",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).create_price_update(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=201,
        body=result,
        resource_id=result["price_update"]["id"],
    )


@router.get("/price-updates", dependencies=[Depends(require_scope(PRICING_READ))])
async def list_price_updates(
    db: DbDep,
    principal: PrincipalDep,
    _: Read,
    status: str | None = None,
    update_type: str | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    service = _service(db)
    filters: dict[str, Any] = {}
    if status:
        filters["status"] = status.upper()
    if update_type:
        filters["update_type"] = update_type.upper()
    rows = await service.list_master(t.price_update, principal.tenant_id, filters=filters or None, limit=_limit(limit))
    if effective_from is not None:
        rows = [row for row in rows if row["effective_start"] >= effective_from.isoformat()]
    if effective_to is not None:
        rows = [row for row in rows if row["effective_end"] is None or row["effective_end"] <= effective_to.isoformat()]
    return {"items": rows}


@router.get("/price-updates/{price_update_id}", dependencies=[Depends(require_scope(PRICING_READ))])
async def get_price_update(price_update_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        result = await _service(db).price_update_detail(tenant_id=principal.tenant_id, update_id=price_update_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    applied_rows = [row for row in await _service(db).repo.list_rows(t.item_price, principal.tenant_id, limit=500)]
    result["applied_rows"] = len(applied_rows) if result["price_update"]["status"] == "APPLIED" else 0
    return result


@router.patch("/price-updates/{price_update_id}", dependencies=[Depends(require_scope(UPDATE_MANAGE))])
async def patch_price_update(
    price_update_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    _: UpdateManager,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    service = _service(db)
    try:
        row = await service.get_master(t.price_update, principal.tenant_id, price_update_id)
        if row["status"] != "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "INVALID_STATE_TRANSITION",
                    "message": "only PENDING updates can be edited",
                    "details": [],
                },
            )
        values = {key: body[key] for key in ("effective_start", "effective_end", "priority") if key in body}
        updated = await service.repo.update_row(
            t.price_update,
            principal.tenant_id,
            price_update_id,
            values,
            principal.identity_id,
            expected_version=_version(if_match),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    if updated is None:
        raise _not_found()
    return {"price_update": updated}


@router.post("/price-updates/{price_update_id}/apply", dependencies=[Depends(require_scope(UPDATE_MANAGE))])
async def apply_price_update(
    price_update_id: str,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: UpdateManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-updates:{price_update_id}:apply"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/price-updates/{price_update_id}/apply",
        body=body,
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).apply_price_update(
            tenant_id=principal.tenant_id,
            update_id=price_update_id,
            as_of=date.fromisoformat(body["as_of"]) if body.get("as_of") else None,
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body=result,
        resource_id=price_update_id,
    )


@router.post("/price-updates/{price_update_id}/cancel", dependencies=[Depends(require_scope(UPDATE_MANAGE))])
async def cancel_price_update(
    price_update_id: str,
    body: ReasonIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: UpdateManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-updates:{price_update_id}:cancel"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/price-updates/{price_update_id}/cancel",
        body=body.model_dump(),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).cancel_price_update(
            tenant_id=principal.tenant_id, update_id=price_update_id, reason=body.reason, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body=result,
        resource_id=price_update_id,
    )


@router.post("/price-updates/{price_update_id}/revert", dependencies=[Depends(require_scope(UPDATE_MANAGE))])
async def revert_price_update(
    price_update_id: str,
    body: ReasonIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: UpdateManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = f"pricing:price-updates:{price_update_id}:revert"
    store, replay = await _idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        method="POST",
        path=f"/api/v1/price-updates/{price_update_id}/revert",
        body=body.model_dump(),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await _service(db).revert_price_update(
            tenant_id=principal.tenant_id, update_id=price_update_id, reason=body.reason, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint,
        key=idempotency_key,
        status=200,
        body=result,
        resource_id=price_update_id,
    )


# --------------------------------------------------------------------------- resolution ------
@router.post("/prices/resolve", dependencies=[Depends(require_scope(PRICING_RESOLVE))])
async def resolve_prices(body: ResolveIn, db: DbDep, principal: PrincipalDep, _: Resolver) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        return await _service(db).resolve(tenant_id=principal.tenant_id, payload=payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/prices/effective", dependencies=[Depends(require_scope(PRICING_RESOLVE))])
async def effective_price(
    item_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    _: Resolver,
    currency_code: str = Query(min_length=3, max_length=3),
    price_level_id: UUID | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    try:
        return await _service(db).effective_price(
            tenant_id=principal.tenant_id,
            payload={
                "item_id": str(item_id),
                "price_level_id": str(price_level_id) if price_level_id else None,
                "currency_code": currency_code,
                "effective_date": as_of.isoformat() if as_of else None,
            },
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/price-lists/generate", dependencies=[Depends(require_scope(PRICING_READ))])
async def generate_price_list(body: dict[str, Any], db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db).price_list(tenant_id=principal.tenant_id, payload=body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


__all__ = ["ROUTE_SCOPES", "router"]
