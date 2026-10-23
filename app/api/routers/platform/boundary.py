"""FS-00 boundary / finance / reference / internal-outbox / job API (task-53).

This router adds the FS-00 §6 surfaces that were missing from group A and that block C0/0A:

* ``GET /api/v1/order/demand/open`` — paged OPEN demand from the Phase-1 ``StubOrderDemandAdapter``
  (external order boundary; no order tables), scope ``inventory.balance.read``;
* ``PUT /api/v1/internal/order-boundary/demand-doubles`` — replace stub fixtures, scope
  ``platform.internal`` + ``Idempotency-Key``;
* ``GET /api/v1/finance/period-status`` — ``fiscal_period`` projection read, scope
  ``platform.finance.read``;
* ``GET /api/v1/reference/exchange-rates`` — ``exchange_rate`` projection read, scope
  ``platform.reference.read``;
* ``PUT /api/v1/internal/finance/exchange-rates`` — finance→platform FX upsert, scope
  ``platform.finance.sync`` + ``Idempotency-Key``;
* ``POST /api/v1/internal/outbox/replay`` — replay/dry-run outbox events, scope ``platform.internal`` +
  ``Idempotency-Key``;
* job operations — ``POST /api/v1/admin/jobs/{name}/run`` (202 QUEUED, ``run_key`` dedupe),
  ``GET /api/v1/admin/job-runs``, ``GET /api/v1/admin/job-runs/{run_id}``,
  ``POST /api/v1/admin/job-runs/{run_id}/cancel``, scope ``platform.job.manage``.

Router registration is integrator-owned (``app/api/main.py``/``registry.py``): this module only exposes
``router`` (+ ``ROUTE_SCOPES``/``get_adapter`` for the coverage + provider contract tests) and never edits
existing routers/main/registry/deps. Every write uses the durable idempotency guard from
``app.core.idempotency_store``; errors render through the platform problem+json handlers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.core.platform_events import emit_platform_event
from app.core.repositories.boundary import (
    DEFAULT_PAGE_LIMIT,
    BoundaryError,
    StubOrderDemandAdapter,
    cancel_job_run,
    enqueue_job_run,
    get_default_adapter,
    get_job_run,
    get_period_status,
    list_exchange_rates,
    list_job_runs,
    parse_demand_rows,
    query_open_demand,
    replay_outbox,
    upsert_exchange_rates,
)

router = APIRouter(prefix="/api/v1", tags=["platform-boundary"])

# Capability scopes — all catalogued in design/permissions.json (never invent a scope string).
INVENTORY_BALANCE_READ = "inventory.balance.read"
PLATFORM_INTERNAL = "platform.internal"
PLATFORM_FINANCE_READ = "platform.finance.read"
PLATFORM_REFERENCE_READ = "platform.reference.read"
PLATFORM_FINANCE_SYNC = "platform.finance.sync"
PLATFORM_JOB_MANAGE = "platform.job.manage"

#: (method, full path) -> exactly one catalogued scope; asserted by the boundary router contract test.
ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/order/demand/open"): INVENTORY_BALANCE_READ,
    ("PUT", "/api/v1/internal/order-boundary/demand-doubles"): PLATFORM_INTERNAL,
    ("GET", "/api/v1/finance/period-status"): PLATFORM_FINANCE_READ,
    ("GET", "/api/v1/reference/exchange-rates"): PLATFORM_REFERENCE_READ,
    ("PUT", "/api/v1/internal/finance/exchange-rates"): PLATFORM_FINANCE_SYNC,
    ("POST", "/api/v1/internal/outbox/replay"): PLATFORM_INTERNAL,
    ("POST", "/api/v1/admin/jobs/{name}/run"): PLATFORM_JOB_MANAGE,
    ("GET", "/api/v1/admin/job-runs"): PLATFORM_JOB_MANAGE,
    ("GET", "/api/v1/admin/job-runs/{run_id}"): PLATFORM_JOB_MANAGE,
    ("POST", "/api/v1/admin/job-runs/{run_id}/cancel"): PLATFORM_JOB_MANAGE,
}


# Dependency-injection point for tests/production external adapters.
def get_adapter() -> StubOrderDemandAdapter:
    return get_default_adapter()


AdapterDep = Annotated[StubOrderDemandAdapter, Depends(get_adapter)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]

BalanceReader = Annotated[object, Depends(require_scope(INVENTORY_BALANCE_READ))]
InternalService = Annotated[object, Depends(require_scope(PLATFORM_INTERNAL))]
FinanceReader = Annotated[object, Depends(require_scope(PLATFORM_FINANCE_READ))]
ReferenceReader = Annotated[object, Depends(require_scope(PLATFORM_REFERENCE_READ))]
FinanceSyncWriter = Annotated[object, Depends(require_scope(PLATFORM_FINANCE_SYNC))]
JobManager = Annotated[object, Depends(require_scope(PLATFORM_JOB_MANAGE))]


# --------------------------------------------------------------------------- errors -----------
def _http_error(exc: BoundaryError) -> HTTPException:
    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


def _not_found(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message, "details": details})


# --------------------------------------------------------------------------- schemas ----------
class DemandDoublesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_version: str = Field(min_length=1, max_length=100)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


class ExchangeRatesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rates: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class OutboxAggregateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=100)
    id: UUID


class OutboxReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[UUID] | None = None
    aggregate: OutboxAggregateIn | None = None
    from_occurred_at: datetime | None = None
    to_occurred_at: datetime | None = None
    dry_run: bool = False
    limit: int = Field(default=1000, ge=1, le=1000)


class JobRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_key: str | None = Field(default=None, min_length=1, max_length=200)
    params: dict[str, Any] | None = None


class CancelRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


# ------------------------------------------------------------------- idempotent helper --------
async def _run_idempotent(
    *,
    db: DbDep,
    principal: PrincipalDep,
    endpoint_id: str,
    key: str,
    method: str,
    path: str,
    payload: Any,
    response: Response,
) -> tuple[IdempotencyStore, dict[str, Any] | None]:
    """Reserve the key and return a stored replay payload when this request already completed."""
    store = IdempotencyStore(db)
    fingerprint = build_fingerprint(
        method=method,
        path=path,
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        body=payload,
    )
    decision = await store.guard(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=key,
        request_fingerprint_hash=fingerprint,
        actor_id=principal.identity_id,
    )
    decision.raise_for_conflict()
    if decision.replay:
        record = decision.record
        assert record is not None
        response.status_code = int(record.response_status or 200)
        return store, dict(record.response_body or {})
    return store, None


# --------------------------------------------------------------------------- order boundary --
@router.get("/order/demand/open", dependencies=[Depends(require_scope(INVENTORY_BALANCE_READ))])
async def open_order_demand(
    _: BalanceReader,
    adapter: AdapterDep,
    item_id: UUID | None = None,
    location_id: UUID | None = None,
    required_by_from: date | None = None,
    required_by_to: date | None = None,
    status: str | None = None,
    priority: int | None = Query(default=None, ge=0),
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=500),
) -> dict[str, Any]:
    try:
        return query_open_demand(
            adapter,
            item_id=item_id,
            location_id=location_id,
            required_by_from=required_by_from,
            required_by_to=required_by_to,
            status=status,
            priority=priority,
            cursor=cursor,
            limit=limit,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/internal/order-boundary/demand-doubles",
    dependencies=[Depends(require_scope(PLATFORM_INTERNAL))],
)
async def replace_demand_doubles(
    body: DemandDoublesIn,
    db: DbDep,
    principal: PrincipalDep,
    adapter: AdapterDep,
    response: Response,
    _: InternalService,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint_id = "internal:order-boundary:demand-doubles"
    path = "/api/v1/internal/order-boundary/demand-doubles"
    store, replay = await _run_idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        method="PUT",
        path=path,
        payload=body.model_dump(exclude_none=True),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        rows = parse_demand_rows(body.rows)
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    adapter.load(rows, fixture_version=body.fixture_version)
    item_ids = sorted({str(row.get("item_id")) for row in body.rows if row.get("item_id")})
    await emit_platform_event(
        db, "order.demand.changed", tenant_id=principal.tenant_id, aggregate_type="order_demand",
        aggregate_id=str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, body.fixture_version)),
        payload={"fixture_version": body.fixture_version, "item_ids": item_ids,
                 "changed_count": len(rows), "occurred_at": datetime.now(UTC).isoformat()},
    )
    result = {"loaded": len(rows), "fixture_version": body.fixture_version}
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        response_status=200,
        response_body=result,
        resource_id=None,
    )
    return result


# --------------------------------------------------------------------------- finance -----------
@router.get("/finance/period-status", dependencies=[Depends(require_scope(PLATFORM_FINANCE_READ))])
async def finance_period_status(
    db: DbDep,
    principal: PrincipalDep,
    _: FinanceReader,
    on_date: Annotated[date, Query(alias="date")],
) -> dict[str, Any]:
    try:
        status = await get_period_status(db, tenant_id=principal.tenant_id, as_of=on_date)
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    if status is None:
        raise _not_found("PERIOD_NOT_FOUND", "no fiscal period covers the requested date", date=on_date.isoformat())
    return status


@router.get("/reference/exchange-rates", dependencies=[Depends(require_scope(PLATFORM_REFERENCE_READ))])
async def reference_exchange_rates(
    db: DbDep,
    principal: PrincipalDep,
    _: ReferenceReader,
    base: str | None = None,
    quote: str | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    try:
        return await list_exchange_rates(
            db,
            tenant_id=principal.tenant_id,
            base=base,
            quote=quote,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.put("/internal/finance/exchange-rates", dependencies=[Depends(require_scope(PLATFORM_FINANCE_SYNC))])
async def sync_exchange_rates(
    body: ExchangeRatesIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: FinanceSyncWriter,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint_id = "internal:finance:exchange-rates"
    path = "/api/v1/internal/finance/exchange-rates"
    store, replay = await _run_idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        method="PUT",
        path=path,
        payload=body.model_dump(exclude_none=True),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await upsert_exchange_rates(db, tenant_id=principal.tenant_id, rates=body.rates)
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        response_status=200,
        response_body=result,
        resource_id=None,
    )
    return result


# --------------------------------------------------------------------------- outbox -----------
@router.post("/internal/outbox/replay", dependencies=[Depends(require_scope(PLATFORM_INTERNAL))])
async def internal_outbox_replay(
    body: OutboxReplayIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: InternalService,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint_id = "internal:outbox:replay"
    path = "/api/v1/internal/outbox/replay"
    store, replay = await _run_idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        method="POST",
        path=path,
        payload=body.model_dump(mode="json", exclude_none=True),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await replay_outbox(
            db,
            tenant_id=principal.tenant_id,
            event_ids=body.event_ids,
            aggregate=body.aggregate.model_dump() if body.aggregate is not None else None,
            from_occurred_at=body.from_occurred_at,
            to_occurred_at=body.to_occurred_at,
            dry_run=body.dry_run,
            limit=body.limit,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        response_status=200,
        response_body=result,
        resource_id=None,
    )
    return result


# --------------------------------------------------------------------------- jobs --------------
@router.post("/admin/jobs/{name}/run", status_code=202, dependencies=[Depends(require_scope(PLATFORM_JOB_MANAGE))])
async def run_job(
    name: str,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: JobManager,
    idempotency_key: IdempotencyKey,
    body: Annotated[JobRunIn | None, Body()] = None,
) -> dict[str, Any]:
    run_key = (body.run_key if body is not None and body.run_key else None) or idempotency_key
    try:
        result = await enqueue_job_run(
            db,
            tenant_id=principal.tenant_id,
            name=name,
            run_key=run_key,
            params=body.params if body is not None else None,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    response.status_code = 202
    return {"job_run_id": result["job_run_id"], "status": "QUEUED", "run": result}


@router.get("/admin/job-runs", dependencies=[Depends(require_scope(PLATFORM_JOB_MANAGE))])
async def job_runs_list(
    db: DbDep,
    principal: PrincipalDep,
    _: JobManager,
    status: str | None = None,
    job_definition_id: UUID | None = None,
    from_created_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_created_at: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    try:
        return await list_job_runs(
            db,
            tenant_id=principal.tenant_id,
            status=status,
            job_definition_id=job_definition_id,
            from_created_at=from_created_at,
            to_created_at=to_created_at,
            cursor=cursor,
            limit=limit,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.get("/admin/job-runs/{run_id}", dependencies=[Depends(require_scope(PLATFORM_JOB_MANAGE))])
async def job_run_detail(
    run_id: UUID,
    db: DbDep,
    principal: PrincipalDep,
    _: JobManager,
) -> dict[str, Any]:
    try:
        return await get_job_run(db, tenant_id=principal.tenant_id, run_id=run_id)
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/admin/job-runs/{run_id}/cancel",
    dependencies=[Depends(require_scope(PLATFORM_JOB_MANAGE))],
)
async def job_run_cancel(
    run_id: UUID,
    body: CancelRunIn,
    db: DbDep,
    principal: PrincipalDep,
    response: Response,
    _: JobManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint_id = f"admin:job-runs:{run_id}:cancel"
    path = f"/api/v1/admin/job-runs/{run_id}/cancel"
    store, replay = await _run_idempotent(
        db=db,
        principal=principal,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        method="POST",
        path=path,
        payload=body.model_dump(exclude_none=True),
        response=response,
    )
    if replay is not None:
        return replay
    try:
        result = await cancel_job_run(db, tenant_id=principal.tenant_id, run_id=run_id, reason=body.reason)
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    await store.complete(
        tenant_id=principal.tenant_id,
        endpoint_id=endpoint_id,
        key=idempotency_key,
        response_status=200,
        response_body=result,
        resource_id=run_id,
    )
    return result


__all__ = ["ROUTE_SCOPES", "get_adapter", "router"]
