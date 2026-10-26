"""FS-06 numbered-inventory API (14 routes, `/api/v1`, catalogued scopes).

Thin transport over :class:`app.modules.traceability_numbered.service.NumberedService`. Serial current
state/history are derived by the 0013 posting triggers — the only write endpoints here are lot identity
mutations and serial registration. Writes require ``Idempotency-Key`` (durable
``platform.idempotency_key`` replay), mutable lots use ``If-Match``, every route requires one catalogued
scope, and the deploy-dark flag ``inventory.numbered.enabled`` gates the surface. Router registration is
integrator-owned (`app/api/main.py`); this module exports ``router`` + ``ROUTE_SCOPES``.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas.traceability import TraceabilityResponse
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.models import platform as platform_models
from app.modules.traceability_numbered.errors import NumberedError
from app.modules.traceability_numbered.service import NumberedService

router = APIRouter(prefix="/api/v1", tags=["traceability-numbered"])

LOT_READ = "inventory.lot.read"
LOT_WRITE = "inventory.lot.write"
LOT_INACTIVATE = "inventory.lot.inactivate"
SERIAL_READ = "inventory.serial.read"
SERIAL_REGISTER = "inventory.serial.register"
NUMBERED_VALIDATE = "inventory.numbered.validate"
NUMBERED_RECONCILE = "inventory.numbered.reconcile"

DOMAIN_FLAG = "inventory.numbered.enabled"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/lots"): LOT_WRITE,
    ("GET", "/api/v1/lots"): LOT_READ,
    ("GET", "/api/v1/lots/{lot_id}"): LOT_READ,
    ("PATCH", "/api/v1/lots/{lot_id}"): LOT_WRITE,
    ("POST", "/api/v1/lots/{lot_id}/inactivate"): LOT_INACTIVATE,
    ("GET", "/api/v1/items/{item_id}/lots"): LOT_READ,
    ("POST", "/api/v1/serials"): SERIAL_REGISTER,
    ("GET", "/api/v1/serials/{serial_id}"): SERIAL_READ,
    ("GET", "/api/v1/serials/{serial_id}/history"): SERIAL_READ,
    ("GET", "/api/v1/serials"): SERIAL_READ,
    ("GET", "/api/v1/items/{item_id}/serials"): SERIAL_READ,
    ("POST", "/api/v1/numbered-inventory/validate"): NUMBERED_VALIDATE,
    ("POST", "/api/v1/numbered-inventory/fefo-plan"): LOT_READ,
    ("GET", "/api/v1/numbered-inventory/search"): SERIAL_READ,
    ("GET", "/api/v1/numbered-inventory/deferred-fks/status"): NUMBERED_RECONCILE,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


class LotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    lot_number: str
    manufacture_date: date | None = None
    expiry_date: date | None = None
    cost_basis: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    notes: str | None = None


class LotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manufacture_date: date | None = None
    expiry_date: date | None = None
    cost_basis: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    notes: str | None = None


class InactivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


class SerialRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    serial_number: str
    specific_cost: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?$")
    ownership: str = "OWNED"
    customer_ref: str | None = None


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    transaction_type: str
    line: dict[str, Any]


class FefoPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    location_id: UUID | None = None
    qty: str = Field(pattern=r"^\d+(\.\d+)?$")
    as_of_date: date | None = None
    inventory_status_id: UUID | None = None
    ownership: str | None = None


def _service(db: DbDep) -> NumberedService:
    return NumberedService(db)


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, NumberedError):
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


async def _domain_enabled(db: DbDep, principal: PrincipalDep) -> None:
    table = platform_models.metadata.tables["platform.feature_flag"]
    result = await db.execute(
        select(table.c.is_enabled).where(table.c.tenant_id == principal.tenant_id, table.c.flag_key == DOMAIN_FLAG)
    )
    if result.scalar_one_or_none() is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DOMAIN_DISABLED",
                "message": "inventory.numbered.enabled is off for this tenant",
                "details": [{"flag": DOMAIN_FLAG}],
            },
        )


Gate = Annotated[None, Depends(_domain_enabled)]


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
    except Exception:  # noqa: BLE001
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
    return await _finish(
        store,
        principal=principal,
        endpoint_id=endpoint_id,
        key=key,
        status=status,
        body=payload,
        resource_id=result.get("id") if isinstance(result, dict) else None,
    )


# =========================================================================== lots ===============
@router.post("/lots", status_code=201, dependencies=[Depends(require_scope(LOT_WRITE))], response_model=TraceabilityResponse)
async def create_lot(
    body: LotCreate,
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
        endpoint_id="traceability_numbered:lots:create",
        method="POST",
        path="/api/v1/lots",
        body=body.model_dump(mode="json"),
        key=idempotency_key,
        action=service.register_lot(
            tenant_id=principal.tenant_id,
            item_id=body.item_id,
            lot_number=body.lot_number,
            manufacture_date=body.manufacture_date,
            expiry_date=body.expiry_date,
            cost_basis=body.cost_basis,
            notes=body.notes,
            actor_id=principal.identity_id,
        ),
        status=201,
        envelope="lot",
    )


@router.get("/lots", dependencies=[Depends(require_scope(LOT_READ))], response_model=TraceabilityResponse)
async def list_lots(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    item_id: UUID | None = None,
    lot_number: str | None = None,
    expiry_before: date | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_lots(
        principal.tenant_id,
        item_id=item_id,
        lot_number_prefix=lot_number,
        expiry_before=expiry_before,
        is_active=active,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


@router.get("/lots/{lot_id}", dependencies=[Depends(require_scope(LOT_READ))], response_model=TraceabilityResponse)
async def get_lot(
    lot_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate, include_positions: bool = False
) -> dict[str, Any]:
    del gate
    try:
        lot = await _service(db).get_lot(principal.tenant_id, lot_id, include_positions=include_positions)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"lot": lot, "version": lot["version"]}


@router.patch("/lots/{lot_id}", dependencies=[Depends(require_scope(LOT_WRITE))], response_model=TraceabilityResponse)
async def patch_lot(
    lot_id: UUID,
    body: LotPatch,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    del gate
    values = {key: value for key, value in body.model_dump(exclude_unset=True).items() if value is not None}
    try:
        lot = await _service(db).update_lot(
            tenant_id=principal.tenant_id,
            lot_id=lot_id,
            values=values,
            actor_id=principal.identity_id,
            expected_version=_version(if_match),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"lot": lot, "version": lot["version"]}


@router.post("/lots/{lot_id}/inactivate", dependencies=[Depends(require_scope(LOT_INACTIVATE))], response_model=TraceabilityResponse)
async def inactivate_lot(
    lot_id: UUID,
    body: InactivateIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"traceability_numbered:lots:{lot_id}:inactivate",
        method="POST",
        path=f"/api/v1/lots/{lot_id}/inactivate",
        body=body.model_dump(mode="json"),
        key=idempotency_key,
        action=_service(db).inactivate_lot(
            tenant_id=principal.tenant_id, lot_id=lot_id, reason=body.reason, actor_id=principal.identity_id
        ),
        envelope="lot",
    )


@router.get("/items/{item_id}/lots", dependencies=[Depends(require_scope(LOT_READ))], response_model=TraceabilityResponse)
async def list_item_lots(
    item_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    lot_number: str | None = None,
    expiry_before: date | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_lots(
        principal.tenant_id,
        item_id=item_id,
        lot_number_prefix=lot_number,
        expiry_before=expiry_before,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


# =========================================================================== serials ============
@router.post("/serials", status_code=201, dependencies=[Depends(require_scope(SERIAL_REGISTER))], response_model=TraceabilityResponse)
async def register_serial(
    body: SerialRegister,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id="traceability_numbered:serials:register",
        method="POST",
        path="/api/v1/serials",
        body=body.model_dump(mode="json"),
        key=idempotency_key,
        action=_service(db).register_serial(
            tenant_id=principal.tenant_id,
            item_id=body.item_id,
            serial_number=body.serial_number,
            specific_cost=body.specific_cost,
            ownership=body.ownership,
            customer_ref=body.customer_ref,
            actor_id=principal.identity_id,
        ),
        status=201,
        envelope="serial",
    )


@router.get("/serials/{serial_id}", dependencies=[Depends(require_scope(SERIAL_READ))], response_model=TraceabilityResponse)
async def get_serial(
    serial_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate, include_history: bool = False
) -> dict[str, Any]:
    del gate
    try:
        serial = await _service(db).get_serial(principal.tenant_id, serial_id, include_history=include_history)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"serial": serial, "version": serial["version"]}


@router.get("/serials/{serial_id}/history", dependencies=[Depends(require_scope(SERIAL_READ))], response_model=TraceabilityResponse)
async def serial_history(
    serial_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    event: str | None = None,
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    from datetime import datetime

    def _parse(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    try:
        items = await _service(db).serial_history(
            principal.tenant_id,
            serial_id,
            event=event,
            from_ts=_parse(from_ts),
            to_ts=_parse(to_ts),
            limit=limit,
            offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return _page(items, limit=limit, offset=offset)


@router.get("/items/{item_id}/serials", dependencies=[Depends(require_scope(SERIAL_READ))], response_model=TraceabilityResponse)
async def list_item_serials(
    item_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    state: str | None = None,
    location_id: UUID | None = None,
    customer_ref: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_serials(
        principal.tenant_id,
        item_id=item_id,
        state=state,
        current_location_id=location_id,
        customer_ref=customer_ref,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


@router.get("/serials", dependencies=[Depends(require_scope(SERIAL_READ))], response_model=TraceabilityResponse)
async def list_serials(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    item_id: UUID | None = None,
    state: str | None = None,
    location_id: UUID | None = None,
    customer_ref: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    items = await _service(db).list_serials(
        principal.tenant_id,
        item_id=item_id,
        state=state,
        current_location_id=location_id,
        customer_ref=customer_ref,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit=limit, offset=offset)


# =========================================================================== numbered =========
@router.post("/numbered-inventory/validate", dependencies=[Depends(require_scope(NUMBERED_VALIDATE))], response_model=TraceabilityResponse)
async def validate_numbered_posting(body: ValidateIn, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    return await _service(db).validate_posting(
        tenant_id=principal.tenant_id,
        item_id=body.item_id,
        transaction_type=body.transaction_type,
        line=body.line,
    )


@router.post("/numbered-inventory/fefo-plan", dependencies=[Depends(require_scope(LOT_READ))], response_model=TraceabilityResponse)
async def fefo_plan(body: FefoPlanIn, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    return await _service(db).plan_fefo(
        tenant_id=principal.tenant_id,
        item_id=body.item_id,
        location_id=body.location_id,
        qty=body.qty,
        as_of_date=body.as_of_date or date.today(),
        inventory_status_id=body.inventory_status_id,
        ownership=body.ownership,
    )


@router.get("/numbered-inventory/search", dependencies=[Depends(require_scope(SERIAL_READ))], response_model=TraceabilityResponse)
async def numbered_search(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    q: str | None = None,
    item_id: UUID | None = None,
    location_id: UUID | None = None,
    state: str | None = None,
    expiry_before: date | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return await _service(db).search(
        principal.tenant_id,
        q=q,
        item_id=item_id,
        location_id=location_id,
        state=state,
        expiry_before=expiry_before,
        limit=limit,
        offset=offset,
    )


@router.get("/numbered-inventory/deferred-fks/status", dependencies=[Depends(require_scope(NUMBERED_RECONCILE))], response_model=TraceabilityResponse)
async def deferred_fks_status(db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate, principal
    return await _service(db).deferred_fk_status()


__all__ = ["ROUTE_SCOPES", "router"]


# ---------------------------------------------------------------- canonical contract aliases -----
# Residual route closure: the contract declares canonical ``{id}`` operations while handlers use
# resource-specific path parameters. These behaviour-preserving aliases delegate to the same handler
# (same signature/deps/scope/status) and only rebind ``id`` -> implementation parameter.
import inspect as _inspect  # noqa: E402
import re as _re  # noqa: E402

_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/items/{id}/lots", "/items/{id}/lots", "item_id"),
    ("GET", "/api/v1/items/{id}/serials", "/items/{id}/serials", "item_id"),
    ("GET", "/api/v1/lots/{id}", "/lots/{id}", "lot_id"),
    ("PATCH", "/api/v1/lots/{id}", "/lots/{id}", "lot_id"),
    ("POST", "/api/v1/lots/{id}/inactivate", "/lots/{id}/inactivate", "lot_id"),
    ("GET", "/api/v1/serials/{id}", "/serials/{id}", "serial_id"),
    ("GET", "/api/v1/serials/{id}/history", "/serials/{id}/history", "serial_id"),
)


def _alias_endpoint(endpoint: Any, implementation_param: str) -> Any:
    signature = _inspect.signature(endpoint)
    parameters = [
        parameter.replace(name="id") if parameter.name == implementation_param else parameter
        for parameter in signature.parameters.values()
    ]

    async def _alias(**kwargs: Any) -> Any:
        kwargs[implementation_param] = kwargs.pop("id")
        return await endpoint(**kwargs)

    _alias.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    _alias.__name__ = f"{endpoint.__name__}_canonical"
    return _alias


def _canonical_norm(path: str) -> str:
    return _re.sub(r"\{[^}]+\}", "{}", path)


def _register_canonical_aliases() -> None:
    routes_by_path: dict[tuple[str, str], Any] = {}
    for registered in router.routes:
        methods = getattr(registered, "methods", None) or set()
        for method in methods:
            routes_by_path[(method.upper(), getattr(registered, "path", ""))] = registered
    for method, declared_path, relative_path, implementation_param in _CANONICAL_ALIASES:
        implementation_path: str | None = next(
            (path for (candidate_method, path) in ROUTE_SCOPES if candidate_method == method and path.endswith(relative_path.split("{id}")[-1]) and _canonical_norm(path) == _canonical_norm(declared_path)),
            None,
        )
        alias_route: Any = routes_by_path.get((method, implementation_path)) if implementation_path else None
        if alias_route is None or implementation_path is None:
            continue
        alias = _alias_endpoint(alias_route.endpoint, implementation_param)
        alias_status = int(getattr(alias_route, "status_code", None) or 200)
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=alias_status,
            response_model=None if alias_status == 204 else TraceabilityResponse,
            dependencies=[Depends(require_scope(ROUTE_SCOPES[(method, implementation_path)]))],
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = ROUTE_SCOPES[(method, implementation_path)]


_register_canonical_aliases()
