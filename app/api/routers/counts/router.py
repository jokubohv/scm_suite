"""FS-07 inventory counts & cycle counts API (`/api/v1`, catalogued scopes, tenant isolated).

Thin transport over :class:`app.modules.counts.service.CountsService`. All POST routes require an
``Idempotency-Key``; writes require one catalogued ``inventory.count.*`` scope. The deploy-dark flag
``inventory.counts.enabled`` gates writes (disabled → 409 DOMAIN_DISABLED) while reads stay audit-safe.

Router registration is integrator-owned; this module exposes ``router`` and ``ROUTE_SCOPES`` only.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.contracts.counts import schemas
from app.core.idempotency_store import IdempotencyStore, build_fingerprint
from app.modules.counts.errors import CountsError
from app.modules.counts.service import CountsService

router = APIRouter(prefix="/api/v1", tags=["counts"])

COUNT_READ = "inventory.count.read"
COUNT_CREATE = "inventory.count.create"
COUNT_CAPTURE = "inventory.count.capture"
COUNT_APPROVE = "inventory.count.approve"
COUNT_POST = "inventory.count.post"
COUNT_PLAN = "inventory.count.plan.manage"
COUNT_RECOUNT = "inventory.count.recount"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/count-plans"): COUNT_READ,
    ("POST", "/api/v1/count-plans"): COUNT_PLAN,
    ("GET", "/api/v1/count-plans/{plan_id}"): COUNT_READ,
    ("PATCH", "/api/v1/count-plans/{plan_id}"): COUNT_PLAN,
    ("POST", "/api/v1/count-plans/{plan_id}/pause"): COUNT_PLAN,
    ("POST", "/api/v1/count-plans/{plan_id}/resume"): COUNT_PLAN,
    ("POST", "/api/v1/count-plans/{plan_id}/archive"): COUNT_PLAN,
    ("POST", "/api/v1/count-plans/{plan_id}/generate"): COUNT_CREATE,
    ("GET", "/api/v1/inventory-counts"): COUNT_READ,
    ("POST", "/api/v1/inventory-counts"): COUNT_CREATE,
    ("GET", "/api/v1/inventory-counts/{count_id}"): COUNT_READ,
    ("POST", "/api/v1/inventory-counts/{count_id}/start"): COUNT_CAPTURE,
    ("GET", "/api/v1/inventory-counts/{count_id}/lines"): COUNT_READ,
    ("POST", "/api/v1/inventory-counts/{count_id}/lines"): COUNT_CAPTURE,
    ("POST", "/api/v1/inventory-counts/{count_id}/recount"): COUNT_RECOUNT,
    ("POST", "/api/v1/inventory-counts/{count_id}/submit"): COUNT_CAPTURE,
    ("POST", "/api/v1/inventory-counts/{count_id}/approve"): COUNT_APPROVE,
    ("POST", "/api/v1/inventory-counts/{count_id}/reject"): COUNT_APPROVE,
    ("POST", "/api/v1/inventory-counts/{count_id}/recalculate-snapshot"): COUNT_APPROVE,
    ("POST", "/api/v1/inventory-counts/{count_id}/post"): COUNT_POST,
    ("GET", "/api/v1/inventory-counts/{count_id}/snapshot"): COUNT_READ,
    ("GET", "/api/v1/inventory-counts/{count_id}/variance"): COUNT_READ,
    ("GET", "/api/v1/items/{item_id}/next-count"): COUNT_READ,
}

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, CountsError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _service(db: AsyncSession, principal: PrincipalDep) -> CountsService:
    return CountsService(db, principal.tenant_id)


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


Read = Annotated[object, Depends(require_scope(COUNT_READ))]
Creator = Annotated[object, Depends(require_scope(COUNT_CREATE))]
Capturer = Annotated[object, Depends(require_scope(COUNT_CAPTURE))]
Approver = Annotated[object, Depends(require_scope(COUNT_APPROVE))]
Poster = Annotated[object, Depends(require_scope(COUNT_POST))]
PlanManager = Annotated[object, Depends(require_scope(COUNT_PLAN))]
Recounter = Annotated[object, Depends(require_scope(COUNT_RECOUNT))]


# ---------------------------------------------------------------------------- plans ----------
@router.get("/count-plans", dependencies=[Depends(require_scope(COUNT_READ))])
async def list_plans(
    db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None, frequency: str | None = None,
    next_run_before: date | None = None, limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).list_plans(
        status=status, frequency=frequency,
        next_run_before=next_run_before.isoformat() if next_run_before else None, limit=_limit(limit),
    )


@router.post("/count-plans", status_code=201, dependencies=[Depends(require_scope(COUNT_PLAN))])
async def create_plan(
    body: schemas.CountPlanCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:count-plans:create"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/count-plans", body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_plan(payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["id"])


@router.get("/count-plans/{plan_id}", dependencies=[Depends(require_scope(COUNT_READ))])
async def get_plan(plan_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/count-plans/{plan_id}", dependencies=[Depends(require_scope(COUNT_PLAN))])
async def update_plan(
    plan_id: str, body: schemas.CountPlanUpdate, db: DbDep, principal: PrincipalDep,
    response: Response, idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:count-plans:update"
    payload = body.model_dump(exclude_unset=True)
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="PATCH", path=f"/api/v1/count-plans/{plan_id}", body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).update_plan(plan_id, payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=plan_id)


def _plan_action(action: str) -> Any:
    async def handler(
        plan_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey,
        body: schemas.ReasonIn | None = None,
    ) -> dict[str, Any]:
        endpoint = f"counts:count-plans:{action}"
        payload = body.model_dump() if body else {}
        store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                         method="POST", path=f"/api/v1/count-plans/{plan_id}/{action}",
                                         body=payload, response=response)
        if replay is not None:
            return replay
        service = _service(db, principal)
        try:
            if action == "pause":
                row = await service.pause_plan(plan_id, payload.get("reason"))
            elif action == "resume":
                row = await service.resume_plan(plan_id)
            else:
                row = await service.archive_plan(plan_id, payload.get("reason"))
        except Exception as exc:  # noqa: BLE001
            _raise(exc)
        return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=plan_id)
    return handler


for _action, _scope in (("pause", COUNT_PLAN), ("resume", COUNT_PLAN), ("archive", COUNT_PLAN)):
    router.add_api_route(
        f"/count-plans/{{plan_id}}/{_action}", _plan_action(_action), methods=["POST"], status_code=200,
        dependencies=[Depends(require_scope(_scope))],
    )


@router.post("/count-plans/{plan_id}/generate", status_code=201, dependencies=[Depends(require_scope(COUNT_CREATE))])
async def generate_from_plan(
    plan_id: str, body: schemas.GenerateCountIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:count-plans:generate"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/count-plans/{plan_id}/generate",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).generate_from_plan(plan_id, payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["count_id"])


# ---------------------------------------------------------------------------- counts ---------
@router.get("/inventory-counts", dependencies=[Depends(require_scope(COUNT_READ))])
async def list_counts(
    db: DbDep, principal: PrincipalDep, _: Read, status: str | None = None, count_type: str | None = None,
    plan_id: str | None = None, count_date_from: date | None = None, count_date_to: date | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).list_counts(
        status=status, count_type=count_type, plan_id=plan_id,
        count_date_from=count_date_from, count_date_to=count_date_to, limit=_limit(limit),
    )


@router.post("/inventory-counts", status_code=201, dependencies=[Depends(require_scope(COUNT_CREATE))])
async def create_count(
    body: schemas.CountCreate, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:create"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path="/api/v1/inventory-counts", body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).create_count(payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=201, body=row, resource_id=row["count_id"])


@router.get("/inventory-counts/{count_id}", dependencies=[Depends(require_scope(COUNT_READ))])
async def get_count(count_id: str, db: DbDep, principal: PrincipalDep, _: Read, include_lines: bool = False) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_count(count_id, include_lines=include_lines)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/inventory-counts/{count_id}/start", dependencies=[Depends(require_scope(COUNT_CAPTURE))])
async def start_count(
    count_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:start"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/start", body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).start_count(count_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.get("/inventory-counts/{count_id}/lines", dependencies=[Depends(require_scope(COUNT_READ))])
async def list_lines(
    count_id: str, db: DbDep, principal: PrincipalDep, _: Read, count_status: str | None = None,
    item_id: str | None = None, bin_id: str | None = None, limit: int = 500,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_lines(
            count_id, count_status=count_status, item_id=item_id, bin_id=bin_id, limit=_limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/inventory-counts/{count_id}/lines", dependencies=[Depends(require_scope(COUNT_CAPTURE))])
async def capture_lines(
    count_id: str, body: schemas.CaptureLinesIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:capture"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/lines",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).capture_lines(count_id, payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/recount", dependencies=[Depends(require_scope(COUNT_RECOUNT))])
async def recount(
    count_id: str, body: schemas.RecountIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:recount"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/recount",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).recount(count_id, payload)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/submit", dependencies=[Depends(require_scope(COUNT_CAPTURE))])
async def submit(
    count_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:submit"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/submit", body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).submit(count_id, requested_by=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/approve", dependencies=[Depends(require_scope(COUNT_APPROVE))])
async def approve(
    count_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey,
    body: schemas.ApproveIn | None = None,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:approve"
    payload = body.model_dump() if body else {}
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/approve",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).approve(count_id, payload, actor_id=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/reject", dependencies=[Depends(require_scope(COUNT_APPROVE))])
async def reject(
    count_id: str, body: schemas.RejectIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:reject"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/reject",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).reject(count_id, payload, actor_id=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/recalculate-snapshot", dependencies=[Depends(require_scope(COUNT_APPROVE))])
async def recalculate_snapshot(
    count_id: str, body: schemas.SnapshotRecalcIn, db: DbDep, principal: PrincipalDep, response: Response,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:recalculate-snapshot"
    payload = body.model_dump()
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/recalculate-snapshot",
                                     body=payload, response=response)
    if replay is not None:
        return replay
    try:
        revision = await _service(db, principal).recalculate_snapshot(count_id, payload)
        row = {"snapshot_revision": revision}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.post("/inventory-counts/{count_id}/post", dependencies=[Depends(require_scope(COUNT_POST))])
async def post_variance(
    count_id: str, db: DbDep, principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKey
) -> dict[str, Any]:
    endpoint = "counts:inventory-counts:post"
    store, replay = await _idempotent(db=db, principal=principal, endpoint_id=endpoint, key=idempotency_key,
                                     method="POST", path=f"/api/v1/inventory-counts/{count_id}/post", body={}, response=response)
    if replay is not None:
        return replay
    try:
        row = await _service(db, principal).post_variance(count_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    return await _finish(store, principal=principal, endpoint_id=endpoint, key=idempotency_key, status=200, body=row, resource_id=count_id)


@router.get("/inventory-counts/{count_id}/snapshot", dependencies=[Depends(require_scope(COUNT_READ))])
async def snapshot(count_id: str, db: DbDep, principal: PrincipalDep, _: Read, revision: int | None = None) -> dict[str, Any]:
    try:
        return await _service(db, principal).snapshot(count_id, revision=revision)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/inventory-counts/{count_id}/variance", dependencies=[Depends(require_scope(COUNT_READ))])
async def variance(count_id: str, db: DbDep, principal: PrincipalDep, _: Read) -> dict[str, Any]:
    try:
        return await _service(db, principal).variance_preview(count_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/items/{item_id}/next-count", dependencies=[Depends(require_scope(COUNT_READ))])
async def next_count(
    item_id: str, db: DbDep, principal: PrincipalDep, _: Read, location_id: str | None = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).next_count(item_id, location_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ---------------------------------------------------------------- canonical contract aliases -----
# Residual route closure: the contract declares canonical ``{id}`` operations while handlers use
# resource-specific path parameters. These behaviour-preserving aliases delegate to the same handler
# (same signature/deps/scope/status) and only rebind ``id`` -> implementation parameter.
import inspect as _inspect  # noqa: E402
import re as _re  # noqa: E402

_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/count-plans/{id}", "/count-plans/{id}", "plan_id"),
    ("PATCH", "/api/v1/count-plans/{id}", "/count-plans/{id}", "plan_id"),
    ("POST", "/api/v1/count-plans/{id}/archive", "/count-plans/{id}/archive", "plan_id"),
    ("POST", "/api/v1/count-plans/{id}/generate", "/count-plans/{id}/generate", "plan_id"),
    ("POST", "/api/v1/count-plans/{id}/pause", "/count-plans/{id}/pause", "plan_id"),
    ("POST", "/api/v1/count-plans/{id}/resume", "/count-plans/{id}/resume", "plan_id"),
    ("GET", "/api/v1/inventory-counts/{id}", "/inventory-counts/{id}", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/approve", "/inventory-counts/{id}/approve", "count_id"),
    ("GET", "/api/v1/inventory-counts/{id}/lines", "/inventory-counts/{id}/lines", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/lines", "/inventory-counts/{id}/lines", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/post", "/inventory-counts/{id}/post", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/recalculate-snapshot", "/inventory-counts/{id}/recalculate-snapshot", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/recount", "/inventory-counts/{id}/recount", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/reject", "/inventory-counts/{id}/reject", "count_id"),
    ("GET", "/api/v1/inventory-counts/{id}/snapshot", "/inventory-counts/{id}/snapshot", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/start", "/inventory-counts/{id}/start", "count_id"),
    ("POST", "/api/v1/inventory-counts/{id}/submit", "/inventory-counts/{id}/submit", "count_id"),
    ("GET", "/api/v1/inventory-counts/{id}/variance", "/inventory-counts/{id}/variance", "count_id"),
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
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=int(getattr(alias_route, "status_code", None) or 200),
            dependencies=[Depends(require_scope(ROUTE_SCOPES[(method, implementation_path)]))],
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = ROUTE_SCOPES[(method, implementation_path)]


_register_canonical_aliases()
