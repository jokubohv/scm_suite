"""FS-00 internal/operations routes declared by the frozen FS-00 §6 surface.

The main application exposes the ``/api/v1/internal/*`` aliases; the contract also declares canonical
un-prefixed ``/internal/*`` paths (L2 gate surfaces + finance/order-boundary sync). This router provides
the canonical un-prefixed operations and the two framework-facing endpoints. Registration is
integrator-owned: the module exports ``router`` + ``ROUTE_SCOPES`` and never edits main/registry.

All writes require the catalogued service scopes and an ``Idempotency-Key``; tenant is always taken
from the authenticated principal, never from the request body.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from app import __version__
from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routers.platform.boundary import (
    DemandDoublesIn,
    ExchangeRatesIn,
    OutboxReplayIn,
    _http_error,
    get_adapter,
)
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import PlatformObject
from app.core.models import platform as p
from app.core.platform_events import emit_platform_event
from app.core.repositories.boundary import (
    BoundaryError,
    StubOrderDemandAdapter,
    parse_demand_rows,
    replay_outbox,
    upsert_exchange_rates,
)

router = APIRouter(responses=PROBLEM_RESPONSES, tags=["platform-internal"], route_class=SCMAPIRoute)

PLATFORM_INTERNAL = "platform.internal"
PLATFORM_FINANCE_SYNC = "platform.finance.sync"
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
InternalService = Annotated[object, Depends(require_scope(PLATFORM_INTERNAL))]
FinanceSyncWriter = Annotated[object, Depends(require_scope(PLATFORM_FINANCE_SYNC))]
AdapterDep = Annotated[StubOrderDemandAdapter, Depends(get_adapter)]

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/internal/deps"): PLATFORM_INTERNAL,
    ("POST", "/internal/outbox/replay"): PLATFORM_INTERNAL,
    ("PUT", "/internal/finance/exchange-rates"): PLATFORM_FINANCE_SYNC,
    ("PUT", "/internal/finance/fiscal-periods"): PLATFORM_FINANCE_SYNC,
    ("PUT", "/internal/order-boundary/demand-doubles"): PLATFORM_INTERNAL,
}


class FiscalPeriodIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_key: str = Field(min_length=1, max_length=40)
    start_date: date
    end_date: date
    status: str = Field(default="OPEN", pattern="^(OPEN|CLOSED|LOCKED)$")
    source_version: int = Field(default=1, ge=1)


class FiscalPeriodsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    periods: list[FiscalPeriodIn] = Field(default_factory=list, max_length=5000)


@router.get("/internal/deps", response_model=PlatformObject)
async def internal_deps(request: Request, _: InternalService) -> dict[str, Any]:
    return {
        "platform_version": __version__,
        "plan_version": "1.5.0",
        "migrations_head": "0031_count_post_txn_idx",
        "outbox": {"depth": None, "oldest_age_s": None},
        "jobs": {"queued": None, "running": None, "oldest_pending_s": None},
        "scheduler": {"leader": "worker", "last_tick": datetime.now(UTC).isoformat()},
    }


@router.get("/openapi.json", include_in_schema=False)
async def root_openapi(request: Request) -> dict[str, Any]:
    return request.app.openapi()


@router.post("/internal/outbox/replay", response_model=PlatformObject, dependencies=[Depends(require_scope(PLATFORM_INTERNAL))])
async def outbox_replay(body: OutboxReplayIn, db: DbDep, principal: PrincipalDep,
                        _: InternalService, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    try:
        return await replay_outbox(
            db, tenant_id=principal.tenant_id, event_ids=body.event_ids,
            aggregate=body.aggregate.model_dump() if body.aggregate is not None else None,
            from_occurred_at=body.from_occurred_at, to_occurred_at=body.to_occurred_at,
            dry_run=body.dry_run, limit=body.limit,
        )
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.put("/internal/finance/exchange-rates", response_model=PlatformObject, dependencies=[Depends(require_scope(PLATFORM_FINANCE_SYNC))])
async def finance_exchange_rates(body: ExchangeRatesIn, db: DbDep, principal: PrincipalDep,
                                 _: FinanceSyncWriter, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    try:
        return await upsert_exchange_rates(db, tenant_id=principal.tenant_id, rates=body.rates)
    except BoundaryError as exc:
        raise _http_error(exc) from exc


@router.put("/internal/finance/fiscal-periods", response_model=PlatformObject, dependencies=[Depends(require_scope(PLATFORM_FINANCE_SYNC))])
async def finance_fiscal_periods(body: FiscalPeriodsIn, db: DbDep, principal: PrincipalDep,
                                 _: FinanceSyncWriter, idempotency_key: IdempotencyKey) -> dict[str, Any]:
    upserted: list[str] = []
    rejected: list[dict[str, Any]] = []
    for entry in body.periods:
        if entry.end_date < entry.start_date:
            rejected.append({"period_key": entry.period_key, "code": "FISCAL_PERIOD_RANGE_INVALID"})
            continue
        existing = (await db.execute(select(p.fiscal_period).where(
            p.fiscal_period.c.tenant_id == principal.tenant_id,
            p.fiscal_period.c.period_key == entry.period_key))).mappings().first()
        try:
            if existing is None:
                await db.execute(p.fiscal_period.insert().values(
                    id=uuid4(), tenant_id=principal.tenant_id, period_key=entry.period_key,
                    start_date=entry.start_date, end_date=entry.end_date, status=entry.status,
                    source_version=entry.source_version, created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC), version=1,
                ))
            else:
                await db.execute(update(p.fiscal_period).where(
                    p.fiscal_period.c.tenant_id == principal.tenant_id,
                    p.fiscal_period.c.id == existing["id"],
                ).values(start_date=entry.start_date, end_date=entry.end_date, status=entry.status,
                         source_version=entry.source_version, updated_at=datetime.now(UTC),
                         version=p.fiscal_period.c.version + 1))
            upserted.append(entry.period_key)
        except Exception as exc:  # noqa: BLE001 - per-row rejection contract
            rejected.append({"period_key": entry.period_key, "code": type(exc).__name__})
    return {"upserted": len(upserted), "period_keys": upserted, "rejected": rejected}


@router.put("/internal/order-boundary/demand-doubles", response_model=PlatformObject, dependencies=[Depends(require_scope(PLATFORM_INTERNAL))])
async def demand_doubles(body: DemandDoublesIn, db: DbDep, principal: PrincipalDep,
                         adapter: AdapterDep, _: InternalService,
                         idempotency_key: IdempotencyKey) -> dict[str, Any]:
    try:
        rows = parse_demand_rows(body.rows)
    except BoundaryError as exc:
        raise _http_error(exc) from exc
    adapter.load(rows, fixture_version=body.fixture_version)
    item_ids = sorted({str(row.get("item_id")) for row in body.rows if row.get("item_id")})
    await emit_platform_event(
        db, "order.demand.changed", tenant_id=principal.tenant_id, aggregate_type="order_demand",
        aggregate_id=str(uuid5(NAMESPACE_URL, body.fixture_version)),
        payload={"fixture_version": body.fixture_version, "item_ids": item_ids,
                 "changed_count": len(rows), "occurred_at": datetime.now(UTC).isoformat()},
    )
    return {"loaded": len(rows), "fixture_version": body.fixture_version}


__all__ = ["ROUTE_SCOPES", "router"]
