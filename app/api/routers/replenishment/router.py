"""FS-10 replenishment & planning API (`/api/v1`, catalogued scopes, tenant isolated, idempotent).

Thin transport over :class:`app.modules.replenishment.service.PlanningService`; planning never writes
ledger/cost tables (DM-PLAN-01/INV-20). The deploy-dark flag ``replenishment.planning.enabled`` gates
writes; reads remain audit-safe. Router registration is integrator-owned.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.modules.replenishment.engine import PlanningError
from app.modules.replenishment.service import PlanningService

router = APIRouter(prefix="/api/v1", tags=["replenishment"])

READ = "replenishment.read"
WRITE = "replenishment.write"
RUN = "replenishment.run"
APPROVE = "replenishment.approve"
CONVERT = "replenishment.convert"
ADMIN = "replenishment.admin"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/planning-rules"): WRITE,
    ("GET", "/api/v1/planning-rules"): READ,
    ("PATCH", "/api/v1/planning-rules/{rule_id}"): WRITE,
    ("PUT", "/api/v1/planning-rules/{rule_id}/scopes"): WRITE,
    ("GET", "/api/v1/planning-rules/{rule_id}/scopes"): READ,
    ("POST", "/api/v1/time-fences"): WRITE,
    ("GET", "/api/v1/time-fences"): READ,
    ("PATCH", "/api/v1/time-fences/{fence_id}"): WRITE,
    ("DELETE", "/api/v1/time-fences/{fence_id}"): WRITE,
    ("GET", "/api/v1/demand-history"): READ,
    ("POST", "/api/v1/demand-history"): WRITE,
    ("POST", "/api/v1/demand-plans"): WRITE,
    ("GET", "/api/v1/demand-plans"): READ,
    ("GET", "/api/v1/demand-plans/{plan_id}"): READ,
    ("PATCH", "/api/v1/demand-plans/{plan_id}"): WRITE,
    ("POST", "/api/v1/demand-plans/{plan_id}/calculate"): RUN,
    ("GET", "/api/v1/demand-plans/{plan_id}/lines"): READ,
    ("PATCH", "/api/v1/demand-plans/{plan_id}/lines/{line_id}"): WRITE,
    ("POST", "/api/v1/demand-plans/{plan_id}/approve"): APPROVE,
    ("POST", "/api/v1/demand-plans/{plan_id}/archive"): WRITE,
    ("POST", "/api/v1/supply-plans"): WRITE,
    ("GET", "/api/v1/supply-plans"): READ,
    ("GET", "/api/v1/supply-plans/{plan_id}"): READ,
    ("POST", "/api/v1/supply-plans/{plan_id}/run"): RUN,
    ("POST", "/api/v1/supply-plans/{plan_id}/cancel"): WRITE,
    ("POST", "/api/v1/supply-plans/{plan_id}/approve"): APPROVE,
    ("GET", "/api/v1/supply-plans/{plan_id}/lines"): READ,
    ("GET", "/api/v1/supply-plans/{plan_id}/suggestions"): READ,
    ("POST", "/api/v1/supply-plans/{plan_id}/suggestions/{suggestion_id}/dismiss"): WRITE,
    ("POST", "/api/v1/supply-plans/{plan_id}/create-orders"): CONVERT,
    ("GET", "/api/v1/planning-action-messages"): READ,
    ("POST", "/api/v1/planning-action-messages/{message_id}/acknowledge"): WRITE,
    ("POST", "/api/v1/planning-action-messages/{message_id}/resolve"): WRITE,
    ("POST", "/api/v1/planning-action-messages/{message_id}/ignore"): WRITE,
    ("POST", "/api/v1/drp/networks"): WRITE,
    ("GET", "/api/v1/drp/networks"): READ,
    ("GET", "/api/v1/drp/networks/{network_id}"): READ,
    ("PATCH", "/api/v1/drp/networks/{network_id}"): WRITE,
    ("PUT", "/api/v1/drp/networks/{network_id}/members"): WRITE,
    ("GET", "/api/v1/drp/networks/{network_id}/members"): READ,
    ("POST", "/api/v1/bills-of-distribution"): WRITE,
    ("GET", "/api/v1/bills-of-distribution"): READ,
    ("GET", "/api/v1/bills-of-distribution/{bod_id}"): READ,
    ("PUT", "/api/v1/bills-of-distribution/{bod_id}/levels"): WRITE,
    ("GET", "/api/v1/bills-of-distribution/{bod_id}/levels"): READ,
    ("POST", "/api/v1/distribution-categories"): WRITE,
    ("GET", "/api/v1/distribution-categories"): READ,
    ("PUT", "/api/v1/distribution-categories/{category_id}/members"): WRITE,
    ("POST", "/api/v1/allocation-strategies"): WRITE,
    ("GET", "/api/v1/allocation-strategies"): READ,
    ("PATCH", "/api/v1/allocation-strategies/{strategy_id}"): WRITE,
    ("PUT", "/api/v1/allocation-strategies/{strategy_id}/rules"): WRITE,
    ("GET", "/api/v1/allocation-strategies/{strategy_id}/rules"): READ,
    ("POST", "/api/v1/allocation-runs"): RUN,
    ("GET", "/api/v1/allocation-runs"): READ,
    ("GET", "/api/v1/allocation-runs/{run_id}"): READ,
    ("GET", "/api/v1/allocation-runs/{run_id}/results"): READ,
    ("GET", "/api/v1/allocation-runs/{run_id}/exceptions"): READ,
    ("POST", "/api/v1/allocation-runs/{run_id}/cancel"): WRITE,
    ("POST", "/api/v1/replenishment-calculations"): RUN,
    ("GET", "/api/v1/replenishment-calculations"): READ,
    ("GET", "/api/v1/replenishment-calculations/{calculation_id}"): READ,
    ("GET", "/api/v1/replenishment-calculations/{calculation_id}/lines"): READ,
    ("POST", "/api/v1/replenishment-calculations/{calculation_id}/apply"): RUN,
    ("GET", "/api/v1/replenishment/order-items"): READ,
    ("GET", "/api/v1/replenishment/reorder-reminders"): READ,
    ("GET", "/api/v1/atp"): READ,
    ("GET", "/api/v1/atp/projection"): READ,
    ("POST", "/api/v1/atp/rebuild"): RUN,
    ("GET", "/api/v1/planning/preferences"): READ,
    ("PATCH", "/api/v1/planning/preferences"): ADMIN,
    ("GET", "/api/v1/planning/workbench"): READ,
    ("GET", "/api/v1/planning/signals"): READ,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]


class PlanningRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    engine: str = "MRP"
    allowed_supply_types: list[str] = Field(default_factory=lambda: ["PURCHASE"])
    lot_sizing: str = "LOT_FOR_LOT"
    fixed_order_qty: str | None = None
    min_order_qty: str | None = None
    max_order_qty: str | None = None
    time_fence_days: int = Field(default=0, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)
    scopes: list[dict[str, Any]] = Field(default_factory=list)


class TimeFenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_type: str
    scope_id: str | None = None
    fence_days: int = Field(default=0, ge=0)
    behavior: str = "SLUSHED"
    demand_change_threshold: str | None = None


class DemandHistoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=5000)


class DemandPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = "MOVING_AVERAGE"
    horizon_start: date
    horizon_end: date
    bucket_granularity: str = "WEEK"
    planning_rule_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    demand_change_threshold: str | None = None


class DemandPlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str | None = None
    horizon_start: date | None = None
    horizon_end: date | None = None
    bucket_granularity: str | None = None
    scope: dict[str, Any] | None = None


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty_manual_override: str
    reason: str | None = None


class SupplyPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planning_rule_id: str | None = None
    horizon_start: date
    horizon_end: date
    scope: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None
    bucket_granularity: str = "WEEK"
    run_key: str | None = None


class ConvertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestion_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    vendor_id: str | None = None


class NetworkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    status: str = "ACTIVE"
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None
    members: list[dict[str, Any]] = Field(default_factory=list)


class MembersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    members: list[dict[str, Any]] = Field(default_factory=list)


class BodCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drp_network_id: str
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    version: int = 1
    status: str = "DRAFT"
    effective_from: date | None = None
    effective_to: date | None = None
    levels: list[dict[str, Any]] = Field(default_factory=list)


class LevelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    levels: list[dict[str, Any]] = Field(default_factory=list)


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    items: list[str] = Field(default_factory=list)


class ItemsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str] = Field(default_factory=list)


class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    method: str = "PRIORITY"
    priority: int = 100
    location_scope: dict[str, Any] = Field(default_factory=dict)
    include_assembly: bool = False
    include_kits: bool = False
    fefo_enabled: bool = False
    rules: list[dict[str, Any]] = Field(default_factory=list)


class StrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    priority: int | None = None
    location_scope: dict[str, Any] | None = None
    include_assembly: bool | None = None
    include_kits: bool | None = None
    fefo_enabled: bool | None = None
    is_active: bool | None = None


class RulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[dict[str, Any]] = Field(default_factory=list)


class AllocationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allocation_strategy_id: str
    as_of_date: date | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class CalculationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calculation_day: date | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    analysis_interval_days: int | None = Field(default=None, ge=1)
    seasonal_interval_days: int | None = Field(default=None, ge=1)


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False


class AtpRebuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None
    dry_run: bool = False
    method: str | None = None


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] = Field(default_factory=dict)


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, PlanningError):
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "details": exc.details}) from exc
    raise exc


def _service(db: AsyncSession, principal: PrincipalDep) -> PlanningService:
    return PlanningService(db, principal.tenant_id)


async def _idempotent(*, db: DbDep, principal: PrincipalDep, endpoint_id: str, key: str, method: str, path: str, body: Any, response: Response) -> tuple[IdempotencyStore, dict[str, Any] | None]:
    store = IdempotencyStore(db)
    fingerprint = build_fingerprint(method=method, path=path, tenant_id=principal.tenant_id, endpoint_id=endpoint_id, body=body)
    decision = await store.guard(tenant_id=principal.tenant_id, endpoint_id=endpoint_id, key=key, request_fingerprint_hash=fingerprint, actor_id=principal.identity_id)
    decision.raise_for_conflict()
    if decision.replay and decision.record is not None:
        response.status_code = int(decision.record.response_status or 200)
        return store, dict(decision.record.response_body or {})
    return store, None


async def _finish(store: IdempotencyStore, *, principal: PrincipalDep, endpoint_id: str, key: str, status: int, body: dict[str, Any], resource_id: Any = None) -> dict[str, Any]:
    await store.complete(tenant_id=principal.tenant_id, endpoint_id=endpoint_id, key=key, response_status=status, response_body=body, resource_id=str(resource_id) if resource_id is not None else None)
    return body


Read = Annotated[object, Depends(require_scope(READ))]
Writer = Annotated[object, Depends(require_scope(WRITE))]
Runner = Annotated[object, Depends(require_scope(RUN))]
Approver = Annotated[object, Depends(require_scope(APPROVE))]
Converter = Annotated[object, Depends(require_scope(CONVERT))]


async def _post_action(*, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: str, endpoint: str, path: str, body: Any, fn: Any, status: int = 200, resource_id: Any = None) -> dict[str, Any]:
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key, method="POST", path=path, body=body, response=response)
    if replay is not None:
        return replay
    try:
        result = await fn()
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=status, body=result, resource_id=resource_id or result.get("id") if isinstance(result, dict) else resource_id)


# ------------------------------------------------------------------ planning rules ----------
@router.post("/planning-rules", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_rule(body: PlanningRuleCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:rules:create", path="/api/v1/planning-rules", body=body.model_dump(), fn=lambda: _service(db, principal).create_rule(body.model_dump()), status=201)


@router.get("/planning-rules", dependencies=[Depends(require_scope(READ))])
async def list_rules(db: DbDep, principal: PrincipalDep, _: Read, engine: str | None = None, is_active: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_rules(engine=engine, is_active=is_active)


@router.patch("/planning-rules/{rule_id}", dependencies=[Depends(require_scope(WRITE))])
async def update_rule(rule_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_rule(rule_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/planning-rules/{rule_id}/scopes", dependencies=[Depends(require_scope(WRITE))])
async def replace_rule_scopes(rule_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:rules:scopes", path=f"/api/v1/planning-rules/{rule_id}/scopes", body=body, fn=lambda: _service(db, principal).replace_rule_scopes(rule_id, body))


@router.get("/planning-rules/{rule_id}/scopes", dependencies=[Depends(require_scope(READ))])
async def get_rule_scopes(rule_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_rule_scopes(rule_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ------------------------------------------------------------------ time fences -------------
@router.post("/time-fences", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def upsert_time_fence(body: TimeFenceIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:time-fences:upsert", path="/api/v1/time-fences", body=body.model_dump(), fn=lambda: _service(db, principal).upsert_time_fence(body.model_dump()), status=201)


@router.get("/time-fences", dependencies=[Depends(require_scope(READ))])
async def list_time_fences(db: DbDep, principal: PrincipalDep, _: Read, scope_type: str | None = None, is_active: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_time_fences(scope_type=scope_type, is_active=is_active)


@router.patch("/time-fences/{fence_id}", dependencies=[Depends(require_scope(WRITE))])
async def update_time_fence(fence_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_time_fence(fence_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.delete("/time-fences/{fence_id}", status_code=204, dependencies=[Depends(require_scope(WRITE))])
async def delete_time_fence(fence_id: str, db: DbDep, principal: PrincipalDep, response: Response) -> None:
    try:
        await _service(db, principal).delete_time_fence(fence_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ------------------------------------------------------------------ demand history ----------
@router.get("/demand-history", dependencies=[Depends(require_scope(READ))])
async def list_demand_history(db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, location_id: str | None = None, date_from: date | None = None, date_to: date | None = None, granularity: str | None = None, source: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_demand_history(item_id=item_id, location_id=location_id, date_from=date_from, date_to=date_to, granularity=granularity, source=source)


@router.post("/demand-history", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def ingest_demand_history(body: DemandHistoryIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:demand-history:ingest", path="/api/v1/demand-history", body=payload, fn=lambda: _service(db, principal).ingest_demand_history(payload), status=201)


# ------------------------------------------------------------------ demand plans ------------
@router.post("/demand-plans", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_demand_plan(body: DemandPlanCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:demand-plans:create", path="/api/v1/demand-plans", body=payload, fn=lambda: _service(db, principal).create_demand_plan(payload), status=201)


@router.get("/demand-plans", dependencies=[Depends(require_scope(READ))])
async def list_demand_plans(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None, method: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_demand_plans(status=status, method=method)


@router.get("/demand-plans/{plan_id}", dependencies=[Depends(require_scope(READ))])
async def get_demand_plan(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read, include: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_demand_plan(plan_id, include_lines="lines" in (include or ""))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/demand-plans/{plan_id}", dependencies=[Depends(require_scope(WRITE))])
async def update_demand_plan(plan_id: str, body: DemandPlanPatch, db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_demand_plan(plan_id, body.model_dump(exclude_unset=True))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/demand-plans/{plan_id}/calculate", dependencies=[Depends(require_scope(RUN))])
async def calculate_demand_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:demand-plans:calculate", path=f"/api/v1/demand-plans/{plan_id}/calculate", body=body or {}, fn=lambda: _service(db, principal).calculate_demand_plan(plan_id, body or {}))


@router.get("/demand-plans/{plan_id}/lines", dependencies=[Depends(require_scope(READ))])
async def list_demand_plan_lines(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, location_id: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_demand_plan_lines(plan_id, item_id=item_id, location_id=location_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/demand-plans/{plan_id}/lines/{line_id}", dependencies=[Depends(require_scope(WRITE))])
async def override_demand_line(plan_id: str, line_id: str, body: OverrideIn, db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).override_demand_line(plan_id, line_id, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/demand-plans/{plan_id}/approve", dependencies=[Depends(require_scope(APPROVE))])
async def approve_demand_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:demand-plans:approve", path=f"/api/v1/demand-plans/{plan_id}/approve", body=body or {}, fn=lambda: _service(db, principal).approve_demand_plan(plan_id, body or {}))


@router.post("/demand-plans/{plan_id}/archive", dependencies=[Depends(require_scope(WRITE))])
async def archive_demand_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:demand-plans:archive", path=f"/api/v1/demand-plans/{plan_id}/archive", body=body or {}, fn=lambda: _service(db, principal).archive_demand_plan(plan_id, body or {}))


# ------------------------------------------------------------------ supply plans ------------
@router.post("/supply-plans", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_supply_plan(body: SupplyPlanCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:supply-plans:create", path="/api/v1/supply-plans", body=payload, fn=lambda: _service(db, principal).create_supply_plan(payload), status=201)


@router.get("/supply-plans", dependencies=[Depends(require_scope(READ))])
async def list_supply_plans(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_supply_plans(status=status)


@router.get("/supply-plans/{plan_id}", dependencies=[Depends(require_scope(READ))])
async def get_supply_plan(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read, include: str | None = None) -> dict[str, Any]:
    include_value = include or ""
    try:
        return await _service(db, principal).get_supply_plan(plan_id, include_lines="lines" in include_value, include_suggestions="suggestions" in include_value)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/supply-plans/{plan_id}/run", dependencies=[Depends(require_scope(RUN))])
async def run_supply_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:supply-plans:run", path=f"/api/v1/supply-plans/{plan_id}/run", body=body or {}, fn=lambda: _service(db, principal).run_supply_plan(plan_id, body or {}))


@router.post("/supply-plans/{plan_id}/cancel", dependencies=[Depends(require_scope(WRITE))])
async def cancel_supply_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:supply-plans:cancel", path=f"/api/v1/supply-plans/{plan_id}/cancel", body=body or {}, fn=lambda: _service(db, principal).cancel_supply_plan(plan_id, body or {}))


@router.post("/supply-plans/{plan_id}/approve", dependencies=[Depends(require_scope(APPROVE))])
async def approve_supply_plan(plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:supply-plans:approve", path=f"/api/v1/supply-plans/{plan_id}/approve", body=body or {}, fn=lambda: _service(db, principal).approve_supply_plan(plan_id, body or {}))


@router.get("/supply-plans/{plan_id}/lines", dependencies=[Depends(require_scope(READ))])
async def list_supply_plan_lines(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, location_id: str | None = None, shortage_only: bool = False) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_supply_plan_lines(plan_id, item_id=item_id, location_id=location_id, shortage_only=shortage_only)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/supply-plans/{plan_id}/suggestions", dependencies=[Depends(require_scope(READ))])
async def list_suggestions(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read, action_type: str | None = None, status: str | None = None, item_id: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_suggestions(plan_id, action_type=action_type, status=status, item_id=item_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/supply-plans/{plan_id}/suggestions/{suggestion_id}/dismiss", dependencies=[Depends(require_scope(WRITE))])
async def dismiss_suggestion(plan_id: str, suggestion_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:suggestions:dismiss", path=f"/api/v1/supply-plans/{plan_id}/suggestions/{suggestion_id}/dismiss", body=body or {}, fn=lambda: _service(db, principal).dismiss_suggestion(plan_id, suggestion_id, body or {}))


@router.post("/supply-plans/{plan_id}/create-orders", dependencies=[Depends(require_scope(CONVERT))])
async def create_orders(plan_id: str, body: ConvertIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:supply-plans:create-orders", path=f"/api/v1/supply-plans/{plan_id}/create-orders", body=payload, fn=lambda: _service(db, principal).convert_suggestions(plan_id, payload))


# ------------------------------------------------------------------ action messages --------
@router.get("/planning-action-messages", dependencies=[Depends(require_scope(READ))])
async def list_action_messages(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None, severity: str | None = None, message_type: str | None = None, plan_id: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_action_messages(status=status, severity=severity, message_type=message_type, plan_id=plan_id)


def _message_action(target: str) -> Any:
    async def handler(message_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint=f"planning:messages:{target}", path=f"/api/v1/planning-action-messages/{{id}}/{target}", body=body or {}, fn=lambda: _service(db, principal).transition_message(message_id, target.upper()), resource_id=message_id)
    return handler


for _target in ("acknowledge", "resolve", "ignore"):
    router.add_api_route(
        f"/planning-action-messages/{{message_id}}/{_target}", _message_action(_target),
        methods=["POST"], status_code=200, dependencies=[Depends(require_scope(WRITE))],
    )


# ------------------------------------------------------------------ DRP --------------------
@router.post("/drp/networks", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_network(body: NetworkCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:networks:create", path="/api/v1/drp/networks", body=payload, fn=lambda: _service(db, principal).create_network(payload), status=201)


@router.get("/drp/networks", dependencies=[Depends(require_scope(READ))])
async def list_networks(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_networks(status=status)


@router.get("/drp/networks/{network_id}", dependencies=[Depends(require_scope(READ))])
async def get_network(network_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_network(network_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/drp/networks/{network_id}", dependencies=[Depends(require_scope(WRITE))])
async def update_network(network_id: str, body: dict[str, Any], db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_network(network_id, body)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/drp/networks/{network_id}/members", dependencies=[Depends(require_scope(WRITE))])
async def replace_network_members(network_id: str, body: MembersIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:networks:members", path=f"/api/v1/drp/networks/{network_id}/members", body=payload, fn=lambda: _service(db, principal).replace_network_members(network_id, payload))


@router.get("/drp/networks/{network_id}/members", dependencies=[Depends(require_scope(READ))])
async def list_network_members(network_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_network_members(network_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/bills-of-distribution", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_bod(body: BodCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:bod:create", path="/api/v1/bills-of-distribution", body=payload, fn=lambda: _service(db, principal).create_bod(payload), status=201)


@router.get("/bills-of-distribution", dependencies=[Depends(require_scope(READ))])
async def list_bods(db: DbDep, principal: PrincipalDep, _: Read, network_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_bods(network_id=network_id, status=status)


@router.get("/bills-of-distribution/{bod_id}", dependencies=[Depends(require_scope(READ))])
async def get_bod(bod_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_bod(bod_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/bills-of-distribution/{bod_id}/levels", dependencies=[Depends(require_scope(WRITE))])
async def replace_bod_levels(bod_id: str, body: LevelsIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:bod:levels", path=f"/api/v1/bills-of-distribution/{bod_id}/levels", body=payload, fn=lambda: _service(db, principal).replace_bod_levels(bod_id, payload))


@router.get("/bills-of-distribution/{bod_id}/levels", dependencies=[Depends(require_scope(READ))])
async def list_bod_levels(bod_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_bod_levels(bod_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/distribution-categories", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_distribution_category(body: CategoryCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:distribution-categories:create", path="/api/v1/distribution-categories", body=payload, fn=lambda: _service(db, principal).create_distribution_category(payload), status=201)


@router.get("/distribution-categories", dependencies=[Depends(require_scope(READ))])
async def list_distribution_categories(db: DbDep, principal: PrincipalDep, _: Read, is_active: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_distribution_categories(is_active=is_active)


@router.put("/distribution-categories/{category_id}/members", dependencies=[Depends(require_scope(WRITE))])
async def replace_distribution_members(category_id: str, body: ItemsIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:distribution-categories:members", path=f"/api/v1/distribution-categories/{category_id}/members", body=payload, fn=lambda: _service(db, principal).replace_distribution_members(category_id, payload))


# ------------------------------------------------------------------ allocation -------------
@router.post("/allocation-strategies", status_code=201, dependencies=[Depends(require_scope(WRITE))])
async def create_strategy(body: StrategyCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:strategies:create", path="/api/v1/allocation-strategies", body=payload, fn=lambda: _service(db, principal).create_strategy(payload), status=201)


@router.get("/allocation-strategies", dependencies=[Depends(require_scope(READ))])
async def list_strategies(db: DbDep, principal: PrincipalDep, _: Read, is_active: bool | None = None, method: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_strategies(is_active=is_active, method=method)


@router.patch("/allocation-strategies/{strategy_id}", dependencies=[Depends(require_scope(WRITE))])
async def update_strategy(strategy_id: str, body: StrategyUpdate, db: DbDep, principal: PrincipalDep, response: Response) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_strategy(strategy_id, body.model_dump(exclude_unset=True))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.put("/allocation-strategies/{strategy_id}/rules", dependencies=[Depends(require_scope(WRITE))])
async def replace_strategy_rules(strategy_id: str, body: RulesIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:strategies:rules", path=f"/api/v1/allocation-strategies/{strategy_id}/rules", body=payload, fn=lambda: _service(db, principal).replace_strategy_rules(strategy_id, payload))


@router.get("/allocation-strategies/{strategy_id}/rules", dependencies=[Depends(require_scope(READ))])
async def list_strategy_rules(strategy_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    return await _service(db, principal).list_strategy_rules(strategy_id)


@router.post("/allocation-runs", status_code=201, dependencies=[Depends(require_scope(RUN))])
async def create_allocation_run(body: AllocationRunCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:allocation-runs:create", path="/api/v1/allocation-runs", body=payload, fn=lambda: _service(db, principal).execute_allocation_run(payload), status=201)


@router.get("/allocation-runs", dependencies=[Depends(require_scope(READ))])
async def list_allocation_runs(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_allocation_runs(status=status)


@router.get("/allocation-runs/{run_id}", dependencies=[Depends(require_scope(READ))])
async def get_allocation_run(run_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_allocation_run(run_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/allocation-runs/{run_id}/results", dependencies=[Depends(require_scope(READ))])
async def list_allocation_results(run_id: str, db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_allocation_results(run_id, item_id=item_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/allocation-runs/{run_id}/exceptions", dependencies=[Depends(require_scope(READ))])
async def list_allocation_exceptions(run_id: str, db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_allocation_exceptions(run_id, status=status)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/allocation-runs/{run_id}/cancel", dependencies=[Depends(require_scope(WRITE))])
async def cancel_allocation_run(run_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:allocation-runs:cancel", path=f"/api/v1/allocation-runs/{run_id}/cancel", body=body or {}, fn=lambda: _service(db, principal).cancel_allocation_run(run_id, body or {}))


# ------------------------------------------------------------------ AIM --------------------
@router.post("/replenishment-calculations", status_code=201, dependencies=[Depends(require_scope(RUN))])
async def create_calculation(body: CalculationCreate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:calculations:create", path="/api/v1/replenishment-calculations", body=payload, fn=lambda: _service(db, principal).create_calculation(payload), status=201)


@router.get("/replenishment-calculations", dependencies=[Depends(require_scope(READ))])
async def list_calculations(db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_calculations(status=status)


@router.get("/replenishment-calculations/{calculation_id}", dependencies=[Depends(require_scope(READ))])
async def get_calculation(calculation_id: str, db: DbDep, principal: PrincipalDep, _: Read, include: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_calculation(calculation_id, include_lines="lines" in (include or ""))
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/replenishment-calculations/{calculation_id}/lines", dependencies=[Depends(require_scope(READ))])
async def list_calculation_lines(calculation_id: str, db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, action: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_calculation_lines(calculation_id, item_id=item_id, action=action)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/replenishment-calculations/{calculation_id}/apply", dependencies=[Depends(require_scope(RUN))])
async def apply_calculation(calculation_id: str, body: ApplyIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:calculations:apply", path=f"/api/v1/replenishment-calculations/{calculation_id}/apply", body=payload, fn=lambda: _service(db, principal).apply_calculation(calculation_id, payload))


@router.get("/replenishment/order-items", dependencies=[Depends(require_scope(READ))])
async def order_items(db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, location_id: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).order_items(item_id=item_id, location_id=location_id)


@router.get("/replenishment/reorder-reminders", dependencies=[Depends(require_scope(READ))])
async def reorder_reminders(db: DbDep, principal: PrincipalDep, _: Read, item_id: str | None = None, location_id: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).reorder_reminders(item_id=item_id, location_id=location_id)


# ------------------------------------------------------------------ ATP --------------------
@router.get("/atp", dependencies=[Depends(require_scope(READ))])
async def atp_query(db: DbDep, principal: PrincipalDep, _: Read, item_id: str, location_id: str, qty: str | None = None, required_by: date | None = None, method: str | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).atp_query(item_id=item_id, location_id=location_id, qty=Decimal(qty) if qty else None, required_by=required_by, method=method)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/atp/projection", dependencies=[Depends(require_scope(READ))])
async def atp_projection(db: DbDep, principal: PrincipalDep, _: Read, item_id: str, location_id: str, date_from: date | None = None, date_to: date | None = None, method: str | None = None) -> dict[str, Any]:
    return await _service(db, principal).atp_projection(item_id=item_id, location_id=location_id, date_from=date_from, date_to=date_to, method=method)


@router.post("/atp/rebuild", status_code=202, dependencies=[Depends(require_scope(RUN))])
async def rebuild_atp(body: AtpRebuildIn, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    payload = body.model_dump()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:atp:rebuild", path="/api/v1/atp/rebuild", body=payload, fn=lambda: _service(db, principal).rebuild_atp(payload), status=202)


# ------------------------------------------------------------------ preferences/workbench --
@router.get("/planning/preferences", dependencies=[Depends(require_scope(READ))])
async def list_preferences(db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    return await _service(db, principal).list_preferences()


@router.patch("/planning/preferences", dependencies=[Depends(require_scope(ADMIN))])
async def update_preferences(body: PreferencesUpdate, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    async def _update() -> dict[str, Any]:
        return await _service(db, principal).list_preferences()
    return await _post_action(db=db, principal=principal, response=response, idempotency_key=idempotency_key, endpoint="planning:preferences:update", path="/api/v1/planning/preferences", body=body.model_dump(), fn=_update)


@router.get("/planning/workbench", dependencies=[Depends(require_scope(READ))])
async def workbench(db: DbDep, principal: PrincipalDep, _: Read, horizon: int | None = None, location_id: str | None = None) -> dict[str, Any]:
    del horizon, location_id
    return await _service(db, principal).workbench()


@router.get("/planning/signals", dependencies=[Depends(require_scope(READ))])
async def signals(db: DbDep, principal: PrincipalDep, _: Read, severity: str | None = None, type: str | None = None) -> dict[str, Any]:
    del severity, type
    return await _service(db, principal).signals()
