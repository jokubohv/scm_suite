"""FS-05 costing & valuation API (`/api/v1`, catalogued scopes, tenant isolated, idempotent writes).

Thin transport over :class:`app.modules.costing.service.CostingService`; business rules live in the
service/engine. Cost-value reads are gated by ``costing.cost.read``; writes require one catalogued scope
and an ``Idempotency-Key``. The deploy-dark flag ``costing.engine.enabled`` gates the surface (default
on, unlike FS-02/FS-03 which are dark by default).

Router registration is integrator-owned (`app/api/main.py`/`registry.py`); this module exposes ``router``
and ``ROUTE_SCOPES`` only.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.contracts.costing import schemas as contracts
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.models import platform as platform_models
from app.modules.costing.errors import CostingError
from app.modules.costing.service import CostingService

router = APIRouter(prefix="/api/v1", tags=["costing"])

COST_READ = "costing.cost.read"
COST_MANAGE = "costing.cost.manage"
STANDARD_ACTIVATE = "costing.standard.activate"
STANDARD_ROLLUP = "costing.standard.rollup"
REVALUATION_CREATE = "costing.revaluation.create"
REVALUATION_POST = "costing.revaluation.post"
REVALUATION_APPROVE = "costing.revaluation.approve"
LANDED_CREATE = "costing.landed_cost.create"
LANDED_POST = "costing.landed_cost.post"
ELC_MANAGE = "costing.estimated_landed_cost.manage"
COGS_CREATE = "costing.cogs_adjustment.create"
COGS_POST = "costing.cogs_adjustment.post"
VARIANCE_VIEW = "costing.variance.view"
GROUP_MANAGE = "costing.group.manage"
RECALC_RUN = "costing.recalc.run"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/costs/item-costs"): COST_READ,
    ("GET", "/api/v1/costs/item-costs/{item_cost_id}"): COST_READ,
    ("GET", "/api/v1/costs/item-costs/{item_cost_id}/layers"): COST_READ,
    ("GET", "/api/v1/costs/item-costs/{item_cost_id}/consumption"): COST_READ,
    ("GET", "/api/v1/costs/valuation"): COST_READ,
    ("GET", "/api/v1/costs/valuation/by-layer"): COST_READ,
    ("GET", "/api/v1/costs/pending-recalculations"): COST_READ,
    ("GET", "/api/v1/cost-categories"): COST_READ,
    ("GET", "/api/v1/cost-categories/{category_id}"): COST_READ,
    ("POST", "/api/v1/cost-categories"): COST_MANAGE,
    ("PATCH", "/api/v1/cost-categories/{category_id}"): COST_MANAGE,
    ("GET", "/api/v1/cost-templates"): COST_READ,
    ("GET", "/api/v1/cost-templates/{template_id}"): COST_READ,
    ("POST", "/api/v1/cost-templates"): COST_MANAGE,
    ("PATCH", "/api/v1/cost-templates/{template_id}"): COST_MANAGE,
    ("GET", "/api/v1/cost-versions"): COST_READ,
    ("GET", "/api/v1/cost-versions/{version_id}"): COST_READ,
    ("POST", "/api/v1/cost-versions"): STANDARD_ACTIVATE,
    ("GET", "/api/v1/cost-versions/{version_id}/planned-standards"): COST_READ,
    ("POST", "/api/v1/cost-versions/{version_id}/planned-standards"): STANDARD_ACTIVATE,
    ("POST", "/api/v1/cost-versions/{version_id}/approve"): STANDARD_ACTIVATE,
    ("POST", "/api/v1/cost-versions/{version_id}/activate"): STANDARD_ACTIVATE,
    ("GET", "/api/v1/standard-costs"): COST_READ,
    ("POST", "/api/v1/standard-costs"): STANDARD_ACTIVATE,
    ("POST", "/api/v1/cost-rollups"): STANDARD_ROLLUP,
    ("GET", "/api/v1/cost-rollups"): COST_READ,
    ("GET", "/api/v1/cost-rollups/{run_id}"): COST_READ,
    ("GET", "/api/v1/cost-rollups/{run_id}/lines"): COST_READ,
    ("POST", "/api/v1/cost-revaluations"): REVALUATION_CREATE,
    ("GET", "/api/v1/cost-revaluations"): COST_READ,
    ("GET", "/api/v1/cost-revaluations/{revaluation_id}"): COST_READ,
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/submit"): REVALUATION_CREATE,
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/approve"): REVALUATION_APPROVE,
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/post"): REVALUATION_POST,
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/reverse"): REVALUATION_POST,
    ("GET", "/api/v1/cost-variances"): VARIANCE_VIEW,
    ("POST", "/api/v1/landed-costs"): LANDED_CREATE,
    ("GET", "/api/v1/landed-costs"): COST_READ,
    ("GET", "/api/v1/landed-costs/{landed_cost_id}"): COST_READ,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/submit"): LANDED_CREATE,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/approve"): LANDED_POST,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/post"): LANDED_POST,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/reverse"): LANDED_POST,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/void"): LANDED_CREATE,
    ("GET", "/api/v1/landed-costs/{landed_cost_id}/allocations"): COST_READ,
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/estimated-costs"): ELC_MANAGE,
    ("GET", "/api/v1/estimated-landed-cost-templates"): COST_READ,
    ("GET", "/api/v1/estimated-landed-cost-templates/{template_id}"): COST_READ,
    ("PATCH", "/api/v1/estimated-landed-cost-templates/{template_id}"): ELC_MANAGE,
    ("POST", "/api/v1/estimated-landed-cost-templates"): ELC_MANAGE,
    ("POST", "/api/v1/cogs-adjustments"): COGS_CREATE,
    ("GET", "/api/v1/cogs-adjustments"): COST_READ,
    ("GET", "/api/v1/cogs-adjustments/{adjustment_id}"): COST_READ,
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/submit"): COGS_CREATE,
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/approve"): COGS_POST,
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/post"): COGS_POST,
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/reverse"): COGS_POST,
    ("POST", "/api/v1/location-costing-groups"): GROUP_MANAGE,
    ("GET", "/api/v1/location-costing-groups"): COST_READ,
    ("GET", "/api/v1/location-costing-groups/{group_id}"): COST_READ,
    ("PATCH", "/api/v1/location-costing-groups/{group_id}"): GROUP_MANAGE,
    ("POST", "/api/v1/location-costing-groups/{group_id}/members"): GROUP_MANAGE,
    ("POST", "/api/v1/location-costing-groups/{group_id}/inactivate"): GROUP_MANAGE,
    ("DELETE", "/api/v1/location-costing-groups/{group_id}/members/{location_id}"): GROUP_MANAGE,
    ("GET", "/api/v1/group-average-balances"): COST_READ,
    ("POST", "/api/v1/cost-recalculations"): RECALC_RUN,
    ("POST", "/api/v1/costs/reconcile"): RECALC_RUN,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, CostingError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


def _not_found(code: str = "NOT_FOUND", message: str = "resource not found") -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message, "details": {}})


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _service(db: AsyncSession, principal: PrincipalDep) -> CostingService:
    return CostingService(db, principal.tenant_id)


async def _idempotent(
    *, db: DbDep, principal: PrincipalDep, endpoint_id: str, key: str, method: str, path: str, body: Any, response: Response
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
    store: IdempotencyStore, *, principal: PrincipalDep, endpoint_id: str, key: str, status: int, body: dict[str, Any], resource_id: Any = None
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


async def _engine_enabled(db: DbDep, principal: PrincipalDep) -> None:
    table = platform_models.metadata.tables["platform.feature_flag"]
    result = await db.execute(
        select(table.c.is_enabled).where(
            table.c.tenant_id == principal.tenant_id, table.c.flag_key == "costing.engine.enabled"
        )
    )
    value = result.scalar_one_or_none()
    if value is False:
        raise HTTPException(
            status_code=503,
            detail={"code": "COSTING_ENGINE_DISABLED", "message": "costing engine is disabled", "details": {}},
        )


router.dependencies.append(Depends(_engine_enabled))

Read = Annotated[object, Depends(require_scope(COST_READ))]
CostManager = Annotated[object, Depends(require_scope(COST_MANAGE))]
StandardActivator = Annotated[object, Depends(require_scope(STANDARD_ACTIVATE))]
RollupRunner = Annotated[object, Depends(require_scope(STANDARD_ROLLUP))]
RevaluationCreator = Annotated[object, Depends(require_scope(REVALUATION_CREATE))]
RevaluationApprover = Annotated[object, Depends(require_scope(REVALUATION_APPROVE))]
RevaluationPoster = Annotated[object, Depends(require_scope(REVALUATION_POST))]
LandedCreator = Annotated[object, Depends(require_scope(LANDED_CREATE))]
LandedPoster = Annotated[object, Depends(require_scope(LANDED_POST))]
ElcManager = Annotated[object, Depends(require_scope(ELC_MANAGE))]
CogsCreator = Annotated[object, Depends(require_scope(COGS_CREATE))]
CogsPoster = Annotated[object, Depends(require_scope(COGS_POST))]
VarianceViewer = Annotated[object, Depends(require_scope(VARIANCE_VIEW))]
GroupManager = Annotated[object, Depends(require_scope(GROUP_MANAGE))]
RecalcRunner = Annotated[object, Depends(require_scope(RECALC_RUN))]


# ------------------------------------------------------------------------- read endpoints -----
@router.get("/costs/item-costs", dependencies=[Depends(require_scope(COST_READ))])
async def list_item_costs(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str | None = None,
    location_id: str | None = None,
    location_costing_group_id: str | None = None,
    lot_id: str | None = None,
    serial_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    del cursor
    return await _service(db, principal).list_item_costs(
        item_id=item_id,
        location_id=location_id,
        location_costing_group_id=location_costing_group_id,
        lot_id=lot_id,
        serial_id=serial_id,
        status=status,
        limit=_limit(limit),
    )


@router.get("/costs/item-costs/{item_cost_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_item_cost(item_cost_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_item_cost(item_cost_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/costs/item-costs/{item_cost_id}/layers", dependencies=[Depends(require_scope(COST_READ))])
async def get_item_cost_layers(
    item_cost_id: str, db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None, as_of: date | None = None
) -> dict[str, Any]:
    del as_of
    try:
        return await _service(db, principal).item_cost_layers(item_cost_id, status=status)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/costs/item-costs/{item_cost_id}/consumption", dependencies=[Depends(require_scope(COST_READ))])
async def get_consumption(item_cost_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).consumption(item_cost_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/costs/valuation", dependencies=[Depends(require_scope(COST_READ))])
async def valuation(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None, location_id: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).valuation(item_id=item_id, location_id=location_id)


@router.get("/costs/valuation/by-layer", dependencies=[Depends(require_scope(COST_READ))])
async def valuation_by_layer(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None, location_id: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).valuation_by_layer(item_id=item_id, location_id=location_id)


@router.get("/costs/pending-recalculations", dependencies=[Depends(require_scope(COST_READ))])
async def pending_recalculations(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).pending_recalculations(item_id=item_id)


# ------------------------------------------------------------------------- categories ---------
@router.get("/cost-categories", dependencies=[Depends(require_scope(COST_READ))])
async def list_categories(db: DbDep, principal: PrincipalDep, active: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_categories(active=active)


@router.get("/cost-categories/{category_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_category(category_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_category(category_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/cost-categories", status_code=201, dependencies=[Depends(require_scope(COST_MANAGE))])
async def create_category(
    body: contracts.CostCategoryCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-categories:create"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-categories", body=body.model_dump(), response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_category(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.patch("/cost-categories/{category_id}", dependencies=[Depends(require_scope(COST_MANAGE))])
async def update_category(
    category_id: str, body: contracts.CostCategoryUpdate, db: DbDep, principal: PrincipalDep,
    response: Response, idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-categories:update"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="PATCH", path=f"/api/v1/cost-categories/{category_id}", body=body.model_dump(), response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_category(category_id, body.model_dump(exclude_unset=True))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=category_id)


# ------------------------------------------------------------------------- templates ----------
@router.get("/cost-templates", dependencies=[Depends(require_scope(COST_READ))])
async def list_templates(db: DbDep, principal: PrincipalDep, inactive: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_templates(inactive=inactive)


@router.get("/cost-templates/{template_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_template(template_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_template(template_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/cost-templates", status_code=201, dependencies=[Depends(require_scope(COST_MANAGE))])
async def create_template(
    body: contracts.CostTemplateCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-templates:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-templates", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_template(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.patch("/cost-templates/{template_id}", dependencies=[Depends(require_scope(COST_MANAGE))])
async def update_template(
    template_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-templates:update"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="PATCH", path=f"/api/v1/cost-templates/{template_id}", body=body, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_template(template_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=template_id)


# ------------------------------------------------------------------------- versions ----------
@router.get("/cost-versions", dependencies=[Depends(require_scope(COST_READ))])
async def list_versions(db: DbDep, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_versions(status=status)


@router.get("/cost-versions/{version_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_version(version_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_version(version_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/cost-versions", status_code=201, dependencies=[Depends(require_scope(STANDARD_ACTIVATE))])
async def create_version(
    body: contracts.CostVersionCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-versions:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-versions", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_version(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/cost-versions/{version_id}/planned-standards", dependencies=[Depends(require_scope(COST_READ))])
async def list_planned_standards(
    version_id: str, db: DbDep, principal: PrincipalDep, item_id: str | None = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_planned_standards(version_id, item_id=item_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/cost-versions/{version_id}/planned-standards", dependencies=[Depends(require_scope(STANDARD_ACTIVATE))])
async def upsert_planned_standards(
    version_id: str, body: contracts.PlannedStandardUpsert, db: DbDep, principal: PrincipalDep,
    response: Response, idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:planned-standards:upsert"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/cost-versions/{version_id}/planned-standards",
                                     body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).upsert_planned_standards(version_id, body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=version_id)


@router.post("/cost-versions/{version_id}/approve", dependencies=[Depends(require_scope(STANDARD_ACTIVATE))])
async def approve_version(
    version_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint = "costing:cost-versions:approve"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/cost-versions/{version_id}/approve",
                                     body=body or {}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).approve_version(version_id, body or {})
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=version_id)


@router.post("/cost-versions/{version_id}/activate", dependencies=[Depends(require_scope(STANDARD_ACTIVATE))])
async def activate_version(
    version_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "costing:cost-versions:activate"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/cost-versions/{version_id}/activate",
                                     body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).activate_version(version_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=version_id)


@router.get("/standard-costs", dependencies=[Depends(require_scope(COST_READ))])
async def list_standard_costs(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None,
    cost_category_id: str | None = None, as_of: date | None = None,
) -> dict[str, Any]:
    return await _service(db, principal).list_standard_costs(
        item_id=item_id, cost_category_id=cost_category_id, as_of=as_of.isoformat() if as_of else None
    )


@router.post("/standard-costs", status_code=201, dependencies=[Depends(require_scope(STANDARD_ACTIVATE))])
async def create_standard_cost(
    body: contracts.StandardCostIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:standard-costs:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/standard-costs", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_standard_cost(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


# ------------------------------------------------------------------------- rollups -----------
@router.post("/cost-rollups", status_code=201, dependencies=[Depends(require_scope(STANDARD_ROLLUP))])
async def create_rollup(
    body: contracts.RollupRequest, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-rollups:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-rollups", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_rollup(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["run_id"])


@router.get("/cost-rollups", dependencies=[Depends(require_scope(COST_READ))])
async def list_rollups(
    db: DbDep, principal: PrincipalDep, status: str | None = None, cost_version_id: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).list_rollups(status=status, cost_version_id=cost_version_id)


@router.get("/cost-rollups/{run_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_rollup(run_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_rollup(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/cost-rollups/{run_id}/lines", dependencies=[Depends(require_scope(COST_READ))])
async def get_rollup_lines(
    run_id: str, db: DbDep, principal: PrincipalDep, item_id: str | None = None,
    cost_category_id: str | None = None, bom_level: int | None = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_rollup_lines(
            run_id, item_id=item_id, cost_category_id=cost_category_id, bom_level=bom_level
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ------------------------------------------------------------------------- revaluations ------
@router.post("/cost-revaluations", status_code=201, dependencies=[Depends(require_scope(REVALUATION_CREATE))])
async def create_revaluation(
    body: contracts.RevaluationCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cost-revaluations:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-revaluations", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_revaluation(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/cost-revaluations", dependencies=[Depends(require_scope(COST_READ))])
async def list_revaluations(db: DbDep, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_revaluations(status=status)


@router.get("/cost-revaluations/{revaluation_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_revaluation(revaluation_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_revaluation(revaluation_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


def _revaluation_transition(target: str) -> Any:
    async def handler(
        revaluation_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        endpoint = f"costing:cost-revaluations:{target}"
        store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                         method="POST", path=f"/api/v1/cost-revaluations/{{id}}/{target}",
                                         body={}, response=response)
        if replay is not None:
            return replay
        try:
            row = await _service(db, principal).transition_revaluation(revaluation_id, target)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=revaluation_id)
    return handler


for _target, _scope in (("submit", REVALUATION_CREATE), ("approve", REVALUATION_APPROVE), ("post", REVALUATION_POST), ("reverse", REVALUATION_POST)):
    router.add_api_route(
        f"/cost-revaluations/{{revaluation_id}}/{_target}",
        _revaluation_transition(_target),
        methods=["POST"],
        status_code=200,
        dependencies=[Depends(require_scope(_scope))],
    )


# ------------------------------------------------------------------------- variances ---------
@router.get("/cost-variances", dependencies=[Depends(require_scope(VARIANCE_VIEW))])
async def list_variances(
    db: DbDep, principal: PrincipalDep, variance_type: str | None = None, item_id: str | None = None,
    fiscal_period_id: str | None = None, work_order_id: str | None = None, status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).list_variances(
        variance_type=variance_type, item_id=item_id, fiscal_period_id=fiscal_period_id,
        work_order_id=work_order_id, status=status, limit=_limit(limit),
    )


# ------------------------------------------------------------------------- landed cost -------
@router.post("/landed-costs", status_code=201, dependencies=[Depends(require_scope(LANDED_CREATE))])
async def create_landed_cost(
    body: contracts.LandedCostCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:landed-costs:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/landed-costs", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_landed_cost(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/landed-costs", dependencies=[Depends(require_scope(COST_READ))])
async def list_landed_costs(db: DbDep, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_landed_costs(status=status)


@router.get("/landed-costs/{landed_cost_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_landed_cost(landed_cost_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_landed_cost(landed_cost_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


def _landed_transition(target: str) -> Any:
    async def handler(
        landed_cost_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        endpoint = f"costing:landed-costs:{target}"
        store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                         method="POST", path=f"/api/v1/landed-costs/{{id}}/{target}", body={}, response=response)
        if replay is not None:
            return replay
        try:
            row = await _service(db, principal).transition_landed_cost(landed_cost_id, target)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=landed_cost_id)
    return handler


for _target, _scope in (("submit", LANDED_CREATE), ("approve", LANDED_POST), ("post", LANDED_POST), ("reverse", LANDED_POST), ("void", LANDED_CREATE)):
    router.add_api_route(
        f"/landed-costs/{{landed_cost_id}}/{_target}",
        _landed_transition(_target),
        methods=["POST"],
        status_code=200,
        dependencies=[Depends(require_scope(_scope))],
    )


@router.get("/landed-costs/{landed_cost_id}/allocations", dependencies=[Depends(require_scope(COST_READ))])
async def get_landed_allocations(
    landed_cost_id: str, db: DbDep, principal: PrincipalDep, line_id: str | None = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).landed_cost_allocations(landed_cost_id, line_id=line_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/landed-costs/{landed_cost_id}/estimated-costs", dependencies=[Depends(require_scope(ELC_MANAGE))])
async def create_estimated_landed_cost(
    landed_cost_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:landed-costs:estimated"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/landed-costs/{landed_cost_id}/estimated-costs",
                                     body=body, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_estimated_landed_cost(landed_cost_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=landed_cost_id)


# ---------------------------------------------------------------- ELC templates -------------
@router.get("/estimated-landed-cost-templates", dependencies=[Depends(require_scope(COST_READ))])
async def list_elc_templates(db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).list_elc_templates()


@router.get("/estimated-landed-cost-templates/{template_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_elc_template(template_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_elc_template(template_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/estimated-landed-cost-templates", status_code=201, dependencies=[Depends(require_scope(ELC_MANAGE))])
async def create_elc_template(
    body: contracts.EstimatedLandedCostTemplateCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:elc-templates:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/estimated-landed-cost-templates", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_elc_template(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.patch("/estimated-landed-cost-templates/{template_id}", dependencies=[Depends(require_scope(ELC_MANAGE))])
async def update_elc_template(
    template_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:elc-templates:update"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="PATCH", path=f"/api/v1/estimated-landed-cost-templates/{template_id}",
                                     body=body, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_elc_template(template_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=template_id)


# ---------------------------------------------------------------- COGS adjustments ----------
@router.post("/cogs-adjustments", status_code=201, dependencies=[Depends(require_scope(COGS_CREATE))])
async def create_cogs_adjustment(
    body: contracts.CogsAdjustmentCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:cogs-adjustments:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cogs-adjustments", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_cogs_adjustment(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/cogs-adjustments", dependencies=[Depends(require_scope(COST_READ))])
async def list_cogs_adjustments(db: DbDep, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_cogs_adjustments(status=status)


@router.get("/cogs-adjustments/{adjustment_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_cogs_adjustment(adjustment_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_cogs_adjustment(adjustment_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


def _cogs_transition(target: str) -> Any:
    async def handler(
        adjustment_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
    ) -> dict[str, Any]:
        endpoint = f"costing:cogs-adjustments:{target}"
        store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                         method="POST", path=f"/api/v1/cogs-adjustments/{{id}}/{target}", body={}, response=response)
        if replay is not None:
            return replay
        try:
            row = await _service(db, principal).transition_cogs_adjustment(adjustment_id, target)
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=adjustment_id)
    return handler


for _target, _scope in (("submit", COGS_CREATE), ("approve", COGS_POST), ("post", COGS_POST), ("reverse", COGS_POST)):
    router.add_api_route(
        f"/cogs-adjustments/{{adjustment_id}}/{_target}",
        _cogs_transition(_target),
        methods=["POST"],
        status_code=200,
        dependencies=[Depends(require_scope(_scope))],
    )


# ---------------------------------------------------------------- groups ---------------------
@router.post("/location-costing-groups", status_code=201, dependencies=[Depends(require_scope(GROUP_MANAGE))])
async def create_group(
    body: contracts.CostingGroupCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:groups:create"
    body_dict = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/location-costing-groups", body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_group(body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/location-costing-groups", dependencies=[Depends(require_scope(COST_READ))])
async def list_groups(db: DbDep, principal: PrincipalDep, active: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_groups(active=active)


@router.get("/location-costing-groups/{group_id}", dependencies=[Depends(require_scope(COST_READ))])
async def get_group(group_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_group(group_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/location-costing-groups/{group_id}", dependencies=[Depends(require_scope(GROUP_MANAGE))])
async def update_group(
    group_id: str, body: contracts.CostingGroupUpdate, db: DbDep, principal: PrincipalDep,
    response: Response, idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:groups:update"
    body_dict = body.model_dump(exclude_unset=True)
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="PATCH", path=f"/api/v1/location-costing-groups/{group_id}",
                                     body=body_dict, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_group(group_id, body_dict)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=group_id)


@router.post("/location-costing-groups/{group_id}/members", dependencies=[Depends(require_scope(GROUP_MANAGE))])
async def add_group_member(
    group_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:groups:add-member"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/location-costing-groups/{group_id}/members",
                                     body=body, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).add_group_member(group_id, str(body["location_id"]))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=group_id)


@router.delete("/location-costing-groups/{group_id}/members/{location_id}", dependencies=[Depends(require_scope(GROUP_MANAGE))])
async def remove_group_member(
    group_id: str, location_id: str, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "costing:groups:remove-member"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="DELETE", path=f"/api/v1/location-costing-groups/{group_id}/members/{location_id}",
                                     body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).remove_group_member(group_id, location_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=group_id)


@router.post("/location-costing-groups/{group_id}/inactivate", dependencies=[Depends(require_scope(GROUP_MANAGE))])
async def inactivate_group(
    group_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "costing:groups:inactivate"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/location-costing-groups/{group_id}/inactivate",
                                     body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_group(group_id, {"is_active": False})
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=group_id)


@router.get("/group-average-balances", dependencies=[Depends(require_scope(COST_READ))])
async def group_average_balances(
    db: DbDep, principal: PrincipalDep, item_id: str | None = None, group_id: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).group_average_balances(item_id=item_id, group_id=group_id)


# ---------------------------------------------------------------- recalculations -------------
@router.post("/cost-recalculations", status_code=202, dependencies=[Depends(require_scope(RECALC_RUN))])
async def request_recalculation(
    body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "costing:recalculations:request"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/cost-recalculations", body=body, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).request_recalculation(body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=202, body=row, resource_id=row.get("job_run_id"))


@router.post("/costs/reconcile", dependencies=[Depends(require_scope(RECALC_RUN))])
async def reconcile(
    db: DbDep, principal: PrincipalDep, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await _service(db, principal).reconciliation(as_of=(body or {}).get("as_of"))


# --------------------------------------------------------------------------- canonical aliases -----
# The FS-05 contract declares resource paths with the canonical ``{id}`` placeholder (and a few legacy
# action spellings) while the implementation historically used resource-specific parameter names
# (``{category_id}``/``{template_id}``/``{landed_cost_id}``/...). These aliases mount the declared
# operations at their exact contract spelling without changing behaviour: the wrapper renames ``id``
# back to the implementation parameter and delegates to the same handler. Five malformed spec-extraction
# paths are intentionally not mounted (documented in the residual report/tests): POST
# ``/cogs-adjustments/{id}`` (read resource with POST), POST ``/cost-revaluations/approve`` (missing
# resource id), and three duplicated-path submit variants under cost-revaluations / landed-costs.
import inspect as _inspect  # noqa: E402

_CANONICAL_ALIASES: tuple[tuple[str, str, str, str | None], ...] = (
    ("GET", "/api/v1/cogs-adjustments/{adjustment_id}", "/cogs-adjustments/{id}", "adjustment_id"),
    ("GET", "/api/v1/cost-revaluations/{revaluation_id}", "/cost-revaluations/{id}", "revaluation_id"),
    ("GET", "/api/v1/cost-rollups/{run_id}", "/cost-rollups/{id}", "run_id"),
    ("GET", "/api/v1/cost-rollups/{run_id}/lines", "/cost-rollups/{id}/lines", "run_id"),
    ("GET", "/api/v1/cost-versions/{version_id}", "/cost-versions/{id}", "version_id"),
    ("GET", "/api/v1/cost-versions/{version_id}/planned-standards", "/cost-versions/{id}/planned-standards", "version_id"),
    ("GET", "/api/v1/costs/item-costs/{item_cost_id}", "/costs/item-costs/{id}", "item_cost_id"),
    ("GET", "/api/v1/costs/item-costs/{item_cost_id}/layers", "/costs/item-costs/{id}/layers", "item_cost_id"),
    ("GET", "/api/v1/landed-costs/{landed_cost_id}", "/landed-costs/{id}", "landed_cost_id"),
    ("GET", "/api/v1/landed-costs/{landed_cost_id}/allocations", "/landed-costs/{id}/allocations", "landed_cost_id"),
    ("GET", "/api/v1/location-costing-groups/{group_id}", "/location-costing-groups/{id}", "group_id"),
    ("PATCH", "/api/v1/cost-categories/{category_id}", "/cost-categories/{id}", "category_id"),
    ("PATCH", "/api/v1/cost-templates/{template_id}", "/cost-templates/{id}", "template_id"),
    ("PATCH", "/api/v1/estimated-landed-cost-templates/{template_id}", "/estimated-landed-cost-templates/{id}", "template_id"),
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/approve", "/cogs-adjustments/{id}/approve", "adjustment_id"),
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/post", "/cogs-adjustments/{id}/post", "adjustment_id"),
    ("POST", "/api/v1/cogs-adjustments/{adjustment_id}/reverse", "/cogs-adjustments/{id}/reverse", "adjustment_id"),
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/approve", "/cost-revaluations/{id}/approve", "revaluation_id"),
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/post", "/cost-revaluations/{id}/post", "revaluation_id"),
    ("POST", "/api/v1/cost-revaluations/{revaluation_id}/reverse", "/cost-revaluations/{id}/reverse", "revaluation_id"),
    ("POST", "/api/v1/cost-versions/{version_id}/activate", "/cost-versions/{id}/activate", "version_id"),
    ("POST", "/api/v1/cost-versions/{version_id}/approve", "/cost-versions/{id}/approve", "version_id"),
    ("POST", "/api/v1/cost-versions/{version_id}/planned-standards", "/cost-versions/{id}/planned-standards", "version_id"),
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/approve", "/landed-costs/{id}/allocations/approve", "landed_cost_id"),
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/post", "/landed-costs/{id}/post", "landed_cost_id"),
    ("POST", "/api/v1/landed-costs/{landed_cost_id}/reverse", "/landed-costs/{id}/reverse", "landed_cost_id"),
    ("POST", "/api/v1/location-costing-groups/{group_id}/members", "/location-costing-groups/{id}/members", "group_id"),
    ("POST", "/api/v1/location-costing-groups/{group_id}/inactivate", "/location-costing-groups/{id}/inactivate", "group_id"),
    ("DELETE", "/api/v1/location-costing-groups/{group_id}/members/{location_id}", "/location-costing-groups/{id}/members/{location_id}", "group_id"),
    ("POST", "/api/v1/cost-recalculations", "/costs/recalculations", None),
)


def _alias_endpoint(endpoint: Any, implementation_param: str | None) -> Any:
    if implementation_param is None:
        return endpoint
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
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            routes_by_path[(method.upper(), getattr(route, "path", ""))] = route
    for method, concrete_path, relative_path, implementation_param in _CANONICAL_ALIASES:
        concrete_route: Any = routes_by_path.get((method, concrete_path))
        if concrete_route is None:  # pragma: no cover - defensive: contract drift detected by tests
            continue
        endpoint = concrete_route.endpoint
        alias = _alias_endpoint(endpoint, implementation_param)
        scope = ROUTE_SCOPES[(method, concrete_path)]
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=int(getattr(concrete_route, "status_code", None) or 200),
            dependencies=[Depends(require_scope(scope))],
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = scope


_register_canonical_aliases()
