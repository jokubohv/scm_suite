"""FS-12 quality-management API (30 routes, `/api/v1`, catalogued scopes).

Thin transport over :class:`app.modules.quality.service.QualityService`. Writes require
``Idempotency-Key`` (durable replay), reads are data-scope filtered, every route requires exactly one
catalogued scope, and the deploy-dark flag ``quality.management.enabled`` gates the surface. Router
registration is integrator-owned; exports ``router`` + ``ROUTE_SCOPES``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.models import platform as platform_models
from app.modules.quality.errors import QualityError
from app.modules.quality.service import QualityService

router = APIRouter(prefix="/api/v1", tags=["quality"])

QUALITY_READ = "quality.read"
QUALITY_SPEC_WRITE = "quality.spec.write"
QUALITY_INSPECT = "quality.inspect"
QUALITY_DISPOSITION = "quality.disposition"
QUALITY_COA_WRITE = "quality.coa.write"
QUALITY_ADMIN = "quality.admin"

DOMAIN_FLAG = "quality.management.enabled"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/quality/specs"): QUALITY_SPEC_WRITE,
    ("GET", "/api/v1/quality/specs"): QUALITY_READ,
    ("GET", "/api/v1/quality/specs/{spec_id}"): QUALITY_READ,
    ("POST", "/api/v1/quality/specs/{spec_id}/versions"): QUALITY_SPEC_WRITE,
    ("POST", "/api/v1/quality/specs/{spec_id}/activate"): QUALITY_SPEC_WRITE,
    ("POST", "/api/v1/quality/specs/{spec_id}/retire"): QUALITY_SPEC_WRITE,
    ("POST", "/api/v1/quality/inspection-plans"): QUALITY_SPEC_WRITE,
    ("GET", "/api/v1/quality/inspection-plans"): QUALITY_READ,
    ("GET", "/api/v1/quality/inspection-plans/{plan_id}"): QUALITY_READ,
    ("PATCH", "/api/v1/quality/inspection-plans"): QUALITY_SPEC_WRITE,
    ("PATCH", "/api/v1/quality/inspection-plans/{plan_id}"): QUALITY_SPEC_WRITE,
    ("POST", "/api/v1/quality/inspection-queues"): QUALITY_ADMIN,
    ("GET", "/api/v1/quality/inspection-queues"): QUALITY_READ,
    ("PATCH", "/api/v1/quality/inspection-queues"): QUALITY_ADMIN,
    ("GET", "/api/v1/quality/inspection-queues/{queue_id}/items"): QUALITY_READ,
    ("POST", "/api/v1/quality/inspection-triggers"): QUALITY_ADMIN,
    ("GET", "/api/v1/quality/inspection-triggers"): QUALITY_READ,
    ("PATCH", "/api/v1/quality/inspection-triggers"): QUALITY_ADMIN,
    ("PATCH", "/api/v1/quality/inspection-triggers/{trigger_id}"): QUALITY_ADMIN,
    ("POST", "/api/v1/quality/inspection-triggers/{trigger_id}/reset-counter"): QUALITY_ADMIN,
    ("POST", "/api/v1/quality/inspection-triggers/{trigger_id}/evaluate"): QUALITY_ADMIN,
    ("GET", "/api/v1/quality/inspections"): QUALITY_READ,
    ("POST", "/api/v1/quality/inspections"): QUALITY_INSPECT,
    ("GET", "/api/v1/quality/inspections/{inspection_id}"): QUALITY_READ,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/start"): QUALITY_INSPECT,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/results"): QUALITY_INSPECT,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/complete"): QUALITY_DISPOSITION,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/waive"): QUALITY_DISPOSITION,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/cancel"): QUALITY_DISPOSITION,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/hold"): QUALITY_DISPOSITION,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/release-hold"): QUALITY_DISPOSITION,
    ("POST", "/api/v1/quality/inspections/{inspection_id}/images"): QUALITY_INSPECT,
    ("POST", "/api/v1/quality/coas"): QUALITY_COA_WRITE,
    ("GET", "/api/v1/quality/coas"): QUALITY_READ,
    ("GET", "/api/v1/quality/coas/{coa_id}"): QUALITY_READ,
    ("POST", "/api/v1/quality/coas/{coa_id}/issue"): QUALITY_COA_WRITE,
    ("POST", "/api/v1/quality/coas/{coa_id}/revoke"): QUALITY_COA_WRITE,
    ("GET", "/api/v1/quality/settings"): QUALITY_READ,
    ("PATCH", "/api/v1/quality/settings"): QUALITY_ADMIN,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


def _service(db: DbDep) -> QualityService:
    return QualityService(db)


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, QualityError):
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
                "message": "quality.management.enabled is off for this tenant",
                "details": [],
            },
        )


Gate = Annotated[None, Depends(_domain_enabled)]


def _page(items: list[dict[str, Any]], *, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "next_cursor": str(offset + len(items)) if len(items) >= limit else None}


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
    store2 = store
    await store2.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        response_status=status,
        response_body=payload,
        resource_id=str(result.get("id")) if isinstance(result, dict) and result.get("id") else None,
    )
    return payload


# =========================================================================== specs =============
@router.post("/quality/specs", status_code=201, dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def create_spec(
    body: dict[str, Any],
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
        endpoint_id="quality:specs:create",
        method="POST",
        path="/api/v1/quality/specs",
        body=body,
        key=idempotency_key,
        action=_service(db).create_spec(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="spec",
    )


@router.get("/quality/specs", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_specs(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    item_id: UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_specs(principal.tenant_id, item_id=item_id, status=status, limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


@router.get("/quality/specs/{spec_id}", dependencies=[Depends(require_scope(QUALITY_READ))])
async def get_spec(spec_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        spec = await _service(db).get_spec(principal.tenant_id, spec_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"spec": spec, "version": spec["version"]}


@router.post(
    "/quality/specs/{spec_id}/versions", status_code=201, dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))]
)
async def new_spec_version(
    spec_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:specs:{spec_id}:versions",
        method="POST",
        path=f"/api/v1/quality/specs/{spec_id}/versions",
        body=body,
        key=idempotency_key,
        action=_service(db).new_spec_version(
            tenant_id=principal.tenant_id, spec_id=spec_id, values=body, actor_id=principal.identity_id
        ),
        status=201,
        envelope="spec",
    )


@router.post("/quality/specs/{spec_id}/activate", dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def activate_spec(
    spec_id: UUID, db: DbDep, principal: PrincipalDep, response: Response, gate: Gate, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    del gate
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"quality:specs:{spec_id}:activate",
        method="POST",
        path=f"/api/v1/quality/specs/{spec_id}/activate",
        body={},
        key=idempotency_key,
        action=_service(db).activate_spec(
            tenant_id=principal.tenant_id, spec_id=spec_id, actor_id=principal.identity_id
        ),
        envelope="spec",
    )


@router.post("/quality/specs/{spec_id}/retire", dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def retire_spec(
    spec_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:specs:{spec_id}:retire",
        method="POST",
        path=f"/api/v1/quality/specs/{spec_id}/retire",
        body=body,
        key=idempotency_key,
        action=_service(db).retire_spec(
            tenant_id=principal.tenant_id,
            spec_id=spec_id,
            reason=body.get("reason"),
            effective_to=date.fromisoformat(body["effective_to"]) if body.get("effective_to") else None,
            actor_id=principal.identity_id,
        ),
        envelope="spec",
    )


# =========================================================================== plans =============
@router.post("/quality/inspection-plans", status_code=201, dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def create_plan(
    body: dict[str, Any],
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
        endpoint_id="quality:plans:create",
        method="POST",
        path="/api/v1/quality/inspection-plans",
        body=body,
        key=idempotency_key,
        action=_service(db).create_plan(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="plan",
    )


@router.get("/quality/inspection-plans", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_plans(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    item_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_plans(principal.tenant_id, item_id=item_id, limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


@router.get("/quality/inspection-plans/{plan_id}", dependencies=[Depends(require_scope(QUALITY_READ))])
async def get_plan(plan_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        plan = await _service(db).get_plan(principal.tenant_id, plan_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"plan": plan, "version": plan["version"]}


@router.patch("/quality/inspection-plans/{plan_id}", dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def patch_plan(
    plan_id: UUID, body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    try:
        plan = await _service(db).update_plan(
            tenant_id=principal.tenant_id, plan_id=plan_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"plan": plan, "version": plan["version"]}


# =========================================================================== queues ============
@router.post("/quality/inspection-queues", status_code=201, dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def create_queue(
    body: dict[str, Any],
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
        endpoint_id="quality:queues:create",
        method="POST",
        path="/api/v1/quality/inspection-queues",
        body=body,
        key=idempotency_key,
        action=_service(db).create_queue(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="queue",
    )


@router.get("/quality/inspection-queues", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_queues(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_queues(principal.tenant_id, limit=limit, offset=offset), limit=limit, offset=offset
    )


@router.get("/quality/inspection-queues/{queue_id}/items", dependencies=[Depends(require_scope(QUALITY_READ))])
async def queue_items(
    queue_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).queue_items(principal.tenant_id, queue_id, limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


# =========================================================================== triggers ==========
@router.post("/quality/inspection-triggers", status_code=201, dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def create_trigger(
    body: dict[str, Any],
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
        endpoint_id="quality:triggers:create",
        method="POST",
        path="/api/v1/quality/inspection-triggers",
        body=body,
        key=idempotency_key,
        action=_service(db).create_trigger(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="trigger",
    )


@router.get("/quality/inspection-triggers", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_triggers(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    trigger_event: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_triggers(principal.tenant_id, trigger_event=trigger_event, limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


@router.patch("/quality/inspection-triggers/{trigger_id}", dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def patch_trigger(
    trigger_id: UUID, body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    try:
        trigger = await _service(db).update_trigger(
            tenant_id=principal.tenant_id, trigger_id=trigger_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"trigger": trigger}


@router.post(
    "/quality/inspection-triggers/{trigger_id}/reset-counter", dependencies=[Depends(require_scope(QUALITY_ADMIN))]
)
async def reset_counter(
    trigger_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:triggers:{trigger_id}:reset",
        method="POST",
        path=f"/api/v1/quality/inspection-triggers/{trigger_id}/reset-counter",
        body=body,
        key=idempotency_key,
        action=_service(db).reset_trigger_counter(
            tenant_id=principal.tenant_id, trigger_id=trigger_id, actor_id=principal.identity_id
        ),
        envelope="",
    )


@router.post("/quality/inspection-triggers/{trigger_id}/evaluate", dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def evaluate_trigger(
    trigger_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:triggers:{trigger_id}:evaluate",
        method="POST",
        path=f"/api/v1/quality/inspection-triggers/{trigger_id}/evaluate",
        body=body,
        key=idempotency_key,
        action=_service(db).evaluate_trigger(
            tenant_id=principal.tenant_id, trigger_id=trigger_id, event=body, actor_id=principal.identity_id
        ),
        envelope="",
    )


# =========================================================================== inspections =======
@router.get("/quality/inspections", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_inspections(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    status: str | None = None,
    item_id: UUID | None = None,
    source_entity_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_inspections(
            principal.tenant_id,
            status=status,
            item_id=item_id,
            source_entity_type=source_entity_type,
            limit=limit,
            offset=offset,
        ),
        limit=limit,
        offset=offset,
    )


@router.post("/quality/inspections", status_code=201, dependencies=[Depends(require_scope(QUALITY_INSPECT))])
async def create_inspection(
    body: dict[str, Any],
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
        endpoint_id="quality:inspections:create",
        method="POST",
        path="/api/v1/quality/inspections",
        body=body,
        key=idempotency_key,
        action=_service(db).create_inspection_on_demand(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        ),
        status=201,
        envelope="inspection",
    )


@router.get("/quality/inspections/{inspection_id}", dependencies=[Depends(require_scope(QUALITY_READ))])
async def get_inspection(inspection_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        inspection = await _service(db).get_inspection(principal.tenant_id, inspection_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"inspection": inspection, "version": inspection["version"]}


@router.post("/quality/inspections/{inspection_id}/start", dependencies=[Depends(require_scope(QUALITY_INSPECT))])
async def start_inspection(
    inspection_id: UUID, body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    try:
        inspection = await _service(db).start_inspection(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            inspector_id=body.get("inspector_id"),
            actor_id=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"inspection": inspection, "version": inspection["version"]}


@router.post("/quality/inspections/{inspection_id}/results", dependencies=[Depends(require_scope(QUALITY_INSPECT))])
async def record_results(
    inspection_id: UUID, body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    try:
        result = await _service(db).record_results(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            results=body.get("results") or [],
            recorded_by=principal.identity_id,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return result


@router.post(
    "/quality/inspections/{inspection_id}/complete", dependencies=[Depends(require_scope(QUALITY_DISPOSITION))]
)
async def complete_inspection(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:complete",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/complete",
        body=body,
        key=idempotency_key,
        action=_service(db).complete_inspection(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            disposition=body.get("disposition"),
            notes=body.get("notes"),
            actor_id=principal.identity_id,
        ),
        envelope="inspection",
    )


@router.post("/quality/inspections/{inspection_id}/waive", dependencies=[Depends(require_scope(QUALITY_DISPOSITION))])
async def waive_inspection(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:waive",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/waive",
        body=body,
        key=idempotency_key,
        action=_service(db).waive_inspection(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            reason=str(body.get("reason") or ""),
            approval_ref=body.get("approval_ref"),
            actor_id=principal.identity_id,
        ),
        envelope="inspection",
    )


@router.post("/quality/inspections/{inspection_id}/cancel", dependencies=[Depends(require_scope(QUALITY_DISPOSITION))])
async def cancel_inspection(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:cancel",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/cancel",
        body=body,
        key=idempotency_key,
        action=_service(db).cancel_inspection(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            reason=str(body.get("reason") or ""),
            actor_id=principal.identity_id,
        ),
        envelope="inspection",
    )


@router.post("/quality/inspections/{inspection_id}/hold", dependencies=[Depends(require_scope(QUALITY_DISPOSITION))])
async def hold_inspection(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:hold",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/hold",
        body=body,
        key=idempotency_key,
        action=_service(db).apply_hold(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            positions=body.get("positions") or [],
            actor_id=principal.identity_id,
        ),
        envelope="",
    )


@router.post(
    "/quality/inspections/{inspection_id}/release-hold", dependencies=[Depends(require_scope(QUALITY_DISPOSITION))]
)
async def release_hold(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:release",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/release-hold",
        body=body,
        key=idempotency_key,
        action=_service(db).release_hold(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            positions=body.get("positions") or [],
            actor_id=principal.identity_id,
        ),
        envelope="",
    )


@router.post(
    "/quality/inspections/{inspection_id}/images",
    status_code=201,
    dependencies=[Depends(require_scope(QUALITY_INSPECT))],
)
async def attach_image(
    inspection_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:inspections:{inspection_id}:images",
        method="POST",
        path=f"/api/v1/quality/inspections/{inspection_id}/images",
        body=body,
        key=idempotency_key,
        action=_service(db).attach_image(
            tenant_id=principal.tenant_id,
            inspection_id=inspection_id,
            attachment_id=str(body.get("attachment_id") or ""),
            actor_id=principal.identity_id,
        ),
        status=201,
        envelope="",
    )


# =========================================================================== COA ===============
@router.post("/quality/coas", status_code=201, dependencies=[Depends(require_scope(QUALITY_COA_WRITE))])
async def create_coa(
    body: dict[str, Any],
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
        endpoint_id="quality:coas:create",
        method="POST",
        path="/api/v1/quality/coas",
        body=body,
        key=idempotency_key,
        action=_service(db).create_coa(tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id),
        status=201,
        envelope="coa",
    )


@router.get("/quality/coas", dependencies=[Depends(require_scope(QUALITY_READ))])
async def list_coas(
    db: DbDep,
    principal: PrincipalDep,
    gate: Gate,
    item_id: UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del gate
    return _page(
        await _service(db).list_coas(principal.tenant_id, item_id=item_id, status=status, limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


@router.get("/quality/coas/{coa_id}", dependencies=[Depends(require_scope(QUALITY_READ))])
async def get_coa(coa_id: UUID, db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        coa = await _service(db).get_coa(principal.tenant_id, coa_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"coa": coa, "version": coa["version"]}


@router.post("/quality/coas/{coa_id}/issue", dependencies=[Depends(require_scope(QUALITY_COA_WRITE))])
async def issue_coa(
    coa_id: UUID,
    body: dict[str, Any],
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    gate: Gate,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    del gate
    issued_at = datetime.fromisoformat(body["issued_at"]) if body.get("issued_at") else None
    return await _run_write(
        response=response,
        db=db,
        principal=principal,
        endpoint_id=f"quality:coas:{coa_id}:issue",
        method="POST",
        path=f"/api/v1/quality/coas/{coa_id}/issue",
        body=body,
        key=idempotency_key,
        action=_service(db).issue_coa(
            tenant_id=principal.tenant_id, coa_id=coa_id, issued_at=issued_at, actor_id=principal.identity_id
        ),
        envelope="coa",
    )


@router.post("/quality/coas/{coa_id}/revoke", dependencies=[Depends(require_scope(QUALITY_COA_WRITE))])
async def revoke_coa(
    coa_id: UUID,
    body: dict[str, Any],
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
        endpoint_id=f"quality:coas:{coa_id}:revoke",
        method="POST",
        path=f"/api/v1/quality/coas/{coa_id}/revoke",
        body=body,
        key=idempotency_key,
        action=_service(db).revoke_coa(
            tenant_id=principal.tenant_id,
            coa_id=coa_id,
            reason=str(body.get("reason") or ""),
            actor_id=principal.identity_id,
        ),
        envelope="coa",
    )


# =========================================================================== settings ==========
@router.get("/quality/settings", dependencies=[Depends(require_scope(QUALITY_READ))])
async def get_settings(db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    return {"settings": await _service(db).get_settings(principal.tenant_id)}


@router.patch("/quality/settings", dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def patch_settings(body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate) -> dict[str, Any]:
    del gate
    try:
        settings = await _service(db).update_settings(
            tenant_id=principal.tenant_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"settings": settings}


# ========================================================== collection updates ================
# The frozen FS-12 contract declares collection-level PATCH operations where the document id travels in
# the body (matching the existing by-id PATCH handlers); these delegate to the same service methods.
def _body_id(body: dict[str, Any], *names: str) -> UUID:
    for name in names:
        if body.get(name):
            return UUID(str(body[name]))
    raise HTTPException(
        status_code=422,
        detail={"code": "VALIDATION_FAILED", "message": f"one of {', '.join(names)} is required", "details": []},
    )


@router.patch("/quality/inspection-plans", dependencies=[Depends(require_scope(QUALITY_SPEC_WRITE))])
async def patch_plan_collection(
    body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    plan_id = _body_id(body, "id", "plan_id")
    try:
        plan = await _service(db).update_plan(
            tenant_id=principal.tenant_id, plan_id=plan_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"plan": plan, "version": plan["version"]}


@router.patch("/quality/inspection-queues", dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def patch_queue_collection(
    body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    queue_id = _body_id(body, "id", "queue_id")
    try:
        queue = await _service(db).update_queue(
            tenant_id=principal.tenant_id, queue_id=queue_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"queue": queue, "version": queue["version"]}


@router.patch("/quality/inspection-triggers", dependencies=[Depends(require_scope(QUALITY_ADMIN))])
async def patch_trigger_collection(
    body: dict[str, Any], db: DbDep, principal: PrincipalDep, gate: Gate
) -> dict[str, Any]:
    del gate
    trigger_id = _body_id(body, "id", "trigger_id")
    try:
        trigger = await _service(db).update_trigger(
            tenant_id=principal.tenant_id, trigger_id=trigger_id, values=body, actor_id=principal.identity_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return {"trigger": trigger}


# ------------------------------------------------------------------ canonical {id} aliases ----
# The frozen FS-12 contract declares canonical ``{id}`` resource placeholders while the implementation
# historically mounted resource-specific names (``{spec_id}``/``{coa_id}``/...). These aliases mount the
# contract-declared operations without changing concrete URLs or behaviour: the wrapper renames ``id``
# back to the implementation parameter and delegates to the same handler.
import inspect as _inspect  # noqa: E402

_QUALITY_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/quality/specs/{spec_id}", "/quality/specs/{id}", "spec_id"),
    ("POST", "/api/v1/quality/specs/{spec_id}/versions", "/quality/specs/{id}/versions", "spec_id"),
    ("POST", "/api/v1/quality/specs/{spec_id}/activate", "/quality/specs/{id}/activate", "spec_id"),
    ("POST", "/api/v1/quality/specs/{spec_id}/retire", "/quality/specs/{id}/retire", "spec_id"),
    ("GET", "/api/v1/quality/inspection-queues/{queue_id}/items", "/quality/inspection-queues/{id}/items", "queue_id"),
    ("POST", "/api/v1/quality/inspection-triggers/{trigger_id}/reset-counter", "/quality/inspection-triggers/{id}/reset-counter", "trigger_id"),
    ("GET", "/api/v1/quality/inspections/{inspection_id}", "/quality/inspections/{id}", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/start", "/quality/inspections/{id}/start", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/results", "/quality/inspections/{id}/results", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/complete", "/quality/inspections/{id}/complete", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/waive", "/quality/inspections/{id}/waive", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/cancel", "/quality/inspections/{id}/cancel", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/hold", "/quality/inspections/{id}/hold", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/release-hold", "/quality/inspections/{id}/release-hold", "inspection_id"),
    ("POST", "/api/v1/quality/inspections/{inspection_id}/images", "/quality/inspections/{id}/images", "inspection_id"),
    ("GET", "/api/v1/quality/coas/{coa_id}", "/quality/coas/{id}", "coa_id"),
    ("POST", "/api/v1/quality/coas/{coa_id}/issue", "/quality/coas/{id}/issue", "coa_id"),
    ("POST", "/api/v1/quality/coas/{coa_id}/revoke", "/quality/coas/{id}/revoke", "coa_id"),
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


def _register_canonical_aliases() -> None:
    routes_by_path: dict[tuple[str, str], Any] = {}
    for route in router.routes:
        for method in getattr(route, "methods", None) or set():
            routes_by_path[(method.upper(), getattr(route, "path", ""))] = route
    for method, declared_path, relative_path, implementation_param in _QUALITY_CANONICAL_ALIASES:
        alias_route: Any = routes_by_path.get((method, declared_path))
        if alias_route is None:  # pragma: no cover - defensive: drift detected by route-surface tests
            continue
        alias = _alias_endpoint(alias_route.endpoint, implementation_param)
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=int(getattr(alias_route, "status_code", None) or 200),
            dependencies=[Depends(require_scope(ROUTE_SCOPES[(method, declared_path)]))],
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = ROUTE_SCOPES[(method, declared_path)]


_register_canonical_aliases()


__all__ = ["ROUTE_SCOPES", "router"]
