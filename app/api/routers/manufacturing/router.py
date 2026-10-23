"""FS-09 manufacturing REST API (``manufacturing.execution@1.1.0``).

Endpoints are declared from `feature-sets/FS-09.md` §6 (master data, work orders, WIP, build/unbuild
with Δ-REUSE dispositions, outsourced manufacturing, ECOs, scheduling/capacity, available-to-build,
compatibility engine, condition codes and inquiries). Every route declares exactly one catalogued scope
via ``require_scope``; errors render as RFC 9457 problem+json. The integrator registers ``router``.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.modules.manufacturing.errors import ManufacturingError
from app.modules.manufacturing.service import ManufacturingService

router = APIRouter(prefix="/api/v1", tags=["manufacturing"])

MFG_READ = "manufacturing.read"
MFG_WRITE = "manufacturing.write"
MFG_RELEASE = "manufacturing.release"
MFG_POST = "manufacturing.post"
MFG_APPROVE = "manufacturing.approve"
MFG_COST_VIEW = "manufacturing.cost.view"
MFG_ADMIN = "manufacturing.admin"
COMPAT_MANAGE = "manufacturing.compat.manage"
COMPAT_VALIDATE = "manufacturing.compat.validate"
COMPAT_OVERRIDE = "manufacturing.compat.override"
UNBUILD_DISPOSE = "manufacturing.unbuild.dispose"

#: (method, full path) -> exactly one catalogued scope; asserted by the router contract/coverage checks.
ROUTE_SCOPES: dict[tuple[str, str], str] = {}


def _scoped(method: str, path: str, scope: str):
    ROUTE_SCOPES[(method, path)] = scope
    return Depends(require_scope(scope))


ReadDep = Annotated[object, Depends(require_scope(MFG_READ))]
WriteDep = Annotated[object, Depends(require_scope(MFG_WRITE))]
ReleaseDep = Annotated[object, Depends(require_scope(MFG_RELEASE))]
PostDep = Annotated[object, Depends(require_scope(MFG_POST))]
ApproveDep = Annotated[object, Depends(require_scope(MFG_APPROVE))]
CostDep = Annotated[object, Depends(require_scope(MFG_COST_VIEW))]
AdminDep = Annotated[object, Depends(require_scope(MFG_ADMIN))]
CompatManageDep = Annotated[object, Depends(require_scope(COMPAT_MANAGE))]
CompatValidateDep = Annotated[object, Depends(require_scope(COMPAT_VALIDATE))]
CompatOverrideDep = Annotated[object, Depends(require_scope(COMPAT_OVERRIDE))]
DisposeDep = Annotated[object, Depends(require_scope(UNBUILD_DISPOSE))]

# Register all scopes in ROUTE_SCOPES up-front (decorator registration is below; contract test compares sets).
_SCOPE_DECLARATIONS: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/v1/assemblies", MFG_WRITE),
    ("GET", "/api/v1/assemblies", MFG_READ),
    ("GET", "/api/v1/assemblies/{assembly_id}", MFG_READ),
    ("PATCH", "/api/v1/assemblies/{assembly_id}", MFG_WRITE),
    ("POST", "/api/v1/boms", MFG_WRITE),
    ("GET", "/api/v1/boms", MFG_READ),
    ("GET", "/api/v1/boms/{bom_id}", MFG_READ),
    ("POST", "/api/v1/boms/{bom_id}/activate", MFG_WRITE),
    ("POST", "/api/v1/boms/{bom_id}/revisions", MFG_WRITE),
    ("GET", "/api/v1/boms/{bom_id}/revisions", MFG_READ),
    ("GET", "/api/v1/bom-revisions/{revision_id}", MFG_READ),
    ("PATCH", "/api/v1/bom-revisions/{revision_id}", MFG_WRITE),
    ("POST", "/api/v1/bom-revisions/{revision_id}/activate", MFG_WRITE),
    ("GET", "/api/v1/bom-revisions/{revision_id}/components", MFG_READ),
    ("POST", "/api/v1/bom-revisions/{revision_id}/components", MFG_WRITE),
    ("PATCH", "/api/v1/bom-components/{component_id}", MFG_WRITE),
    ("DELETE", "/api/v1/bom-components/{component_id}", MFG_WRITE),
    ("PUT", "/api/v1/bom-components/{component_id}/control", MFG_WRITE),
    ("GET", "/api/v1/bom-components/{component_id}/control", MFG_READ),
    ("GET", "/api/v1/assemblies/{assembly_id}/bom-inquiry", MFG_READ),
    ("GET", "/api/v1/assemblies/{assembly_id}/costed-bom-inquiry", MFG_COST_VIEW),
    ("GET", "/api/v1/component-usage/where-used", MFG_READ),
    ("POST", "/api/v1/assemblies/{assembly_id}/explode", MFG_READ),
    ("POST", "/api/v1/routings", MFG_WRITE),
    ("GET", "/api/v1/routings", MFG_READ),
    ("GET", "/api/v1/routings/{routing_id}", MFG_READ),
    ("PATCH", "/api/v1/routings/{routing_id}", MFG_WRITE),
    ("POST", "/api/v1/routings/{routing_id}/activate", MFG_WRITE),
    ("POST", "/api/v1/routings/{routing_id}/steps", MFG_WRITE),
    ("PATCH", "/api/v1/routing-steps/{step_id}", MFG_WRITE),
    ("POST", "/api/v1/routing-steps/{step_id}/charges", MFG_WRITE),
    ("POST", "/api/v1/work-centers", MFG_WRITE),
    ("GET", "/api/v1/work-centers", MFG_READ),
    ("PATCH", "/api/v1/work-centers/{work_center_id}", MFG_WRITE),
    ("POST", "/api/v1/work-center-groups", MFG_WRITE),
    ("GET", "/api/v1/work-center-groups", MFG_READ),
    ("POST", "/api/v1/work-center-calendars", MFG_WRITE),
    ("GET", "/api/v1/work-center-calendars", MFG_READ),
    ("PUT", "/api/v1/work-center-calendars/{calendar_id}/days", MFG_WRITE),
    ("POST", "/api/v1/manufacturing-cost-templates", MFG_WRITE),
    ("GET", "/api/v1/manufacturing-cost-templates", MFG_READ),
    ("POST", "/api/v1/manufacturing-cost-templates/{template_id}/lines", MFG_WRITE),
    ("GET", "/api/v1/manufacturing-preferences", MFG_READ),
    ("PATCH", "/api/v1/manufacturing-preferences", MFG_ADMIN),
    ("POST", "/api/v1/work-orders", MFG_WRITE),
    ("GET", "/api/v1/work-orders", MFG_READ),
    ("GET", "/api/v1/work-orders/{work_order_id}", MFG_READ),
    ("PATCH", "/api/v1/work-orders/{work_order_id}", MFG_WRITE),
    ("POST", "/api/v1/work-orders/{work_order_id}/firm", MFG_WRITE),
    ("POST", "/api/v1/work-orders/{work_order_id}/release", MFG_RELEASE),
    ("POST", "/api/v1/work-orders/{work_order_id}/issue", MFG_POST),
    ("POST", "/api/v1/work-orders/{work_order_id}/complete", MFG_POST),
    ("POST", "/api/v1/work-orders/{work_order_id}/backflush", MFG_POST),
    ("POST", "/api/v1/work-orders/{work_order_id}/close", MFG_POST),
    ("POST", "/api/v1/work-orders/{work_order_id}/void", MFG_WRITE),
    ("POST", "/api/v1/work-orders/{work_order_id}/reopen", MFG_WRITE),
    ("GET", "/api/v1/work-orders/{work_order_id}/components", MFG_READ),
    ("GET", "/api/v1/work-orders/{work_order_id}/components/pick-requirements", MFG_READ),
    ("GET", "/api/v1/work-orders/{work_order_id}/operations", MFG_READ),
    ("POST", "/api/v1/work-order-operations/{operation_id}/complete", MFG_POST),
    ("GET", "/api/v1/work-orders/{work_order_id}/wip-balance", MFG_READ),
    ("GET", "/api/v1/wip-transactions/{wip_transaction_id}", MFG_READ),
    ("POST", "/api/v1/wip-transactions/{wip_transaction_id}/reverse", MFG_POST),
    ("POST", "/api/v1/assembly-builds", MFG_WRITE),
    ("GET", "/api/v1/assembly-builds", MFG_READ),
    ("GET", "/api/v1/assembly-builds/{build_id}", MFG_READ),
    ("POST", "/api/v1/assembly-builds/{build_id}/post", MFG_POST),
    ("POST", "/api/v1/assembly-builds/{build_id}/reverse", MFG_POST),
    ("POST", "/api/v1/assembly-unbuilds", MFG_WRITE),
    ("GET", "/api/v1/assembly-unbuilds", MFG_READ),
    ("GET", "/api/v1/assembly-unbuilds/{unbuild_id}", MFG_READ),
    ("POST", "/api/v1/assembly-unbuilds/{unbuild_id}/post", MFG_POST),
    ("POST", "/api/v1/assembly-unbuilds/{unbuild_id}/reverse", UNBUILD_DISPOSE),
    ("POST", "/api/v1/outsourced-operations", MFG_WRITE),
    ("GET", "/api/v1/outsourced-operations", MFG_READ),
    ("POST", "/api/v1/outsourced-operations/{operation_id}/create-po", MFG_WRITE),
    ("POST", "/api/v1/outsourced-operations/{operation_id}/receive", MFG_POST),
    ("POST", "/api/v1/outsourced-operations/{operation_id}/close", MFG_WRITE),
    ("POST", "/api/v1/outsourced-operations/consolidate", MFG_WRITE),
    ("POST", "/api/v1/ecos", MFG_WRITE),
    ("GET", "/api/v1/ecos", MFG_READ),
    ("GET", "/api/v1/ecos/{eco_id}", MFG_READ),
    ("POST", "/api/v1/ecos/{eco_id}/submit", MFG_WRITE),
    ("POST", "/api/v1/ecos/{eco_id}/approve", MFG_APPROVE),
    ("POST", "/api/v1/ecos/{eco_id}/implement", MFG_APPROVE),
    ("POST", "/api/v1/scheduling-runs", MFG_WRITE),
    ("GET", "/api/v1/scheduling-runs/{run_id}", MFG_READ),
    ("GET", "/api/v1/scheduling-runs/{run_id}/scheduled-operations", MFG_READ),
    ("POST", "/api/v1/scheduling-runs/{run_id}/apply", MFG_WRITE),
    ("GET", "/api/v1/capacity-plans", MFG_READ),
    ("GET", "/api/v1/work-center-loads", MFG_READ),
    ("GET", "/api/v1/available-to-build", MFG_READ),
    ("POST", "/api/v1/available-to-build/calculate", MFG_READ),
    ("GET", "/api/v1/manufacturing/condition-codes", MFG_READ),
    ("POST", "/api/v1/manufacturing/condition-codes", MFG_ADMIN),
    ("PATCH", "/api/v1/manufacturing/condition-codes/{condition_id}", MFG_ADMIN),
    ("GET", "/api/v1/manufacturing/compat/attributes", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/attributes", COMPAT_MANAGE),
    ("PATCH", "/api/v1/manufacturing/compat/attributes/{attribute_id}", COMPAT_MANAGE),
    ("GET", "/api/v1/items/{item_id}/compat-attributes", COMPAT_MANAGE),
    ("PUT", "/api/v1/items/{item_id}/compat-attributes", COMPAT_MANAGE),
    ("GET", "/api/v1/manufacturing/compat/rule-sets", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/rule-sets", COMPAT_MANAGE),
    ("GET", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", COMPAT_MANAGE),
    ("PATCH", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/activate", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/retire", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules", COMPAT_MANAGE),
    ("PATCH", "/api/v1/manufacturing/compat/rules/{rule_id}", COMPAT_MANAGE),
    ("GET", "/api/v1/manufacturing/compat/slots", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/compat/slots", COMPAT_MANAGE),
    ("PATCH", "/api/v1/manufacturing/compat/slots/{slot_id}", COMPAT_MANAGE),
    ("POST", "/api/v1/manufacturing/configurations/validate", COMPAT_VALIDATE),
    ("POST", "/api/v1/manufacturing/configurations/simulate", COMPAT_VALIDATE),
    ("GET", "/api/v1/manufacturing/configurations/{configuration_id}", COMPAT_VALIDATE),
    ("GET", "/api/v1/manufacturing/configurations", COMPAT_VALIDATE),
    ("GET", "/api/v1/manufacturing/compat/report", COMPAT_VALIDATE),
)
ROUTE_SCOPES.update({(method, path): scope for method, path, scope in _SCOPE_DECLARATIONS})


def _svc(db: DbDep, principal: PrincipalDep) -> ManufacturingService:
    return ManufacturingService(db, principal.tenant_id, actor_id=principal.identity_id)


ServiceDep = Annotated[ManufacturingService, Depends(_svc)]


def _raise(exc: ManufacturingError) -> NoReturn:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


# =================================================================== schemas ==================
class Body(BaseModel):
    model_config = ConfigDict(extra="allow")


class AssemblyCreate(Body):
    item_id: str
    assembly_type: str = "STANDARD"


class BomCreate(Body):
    assembly_item_id: str
    bom_code: str
    name: str


class RevisionCreate(Body):
    revision_code: str
    effective_from: Any
    effective_to: Any | None = None


class ComponentCreate(Body):
    component_item_id: str
    qty_per: Any
    uom_id: str
    line_number: int | None = None


class WorkOrderCreate(Body):
    assembly_item_id: str
    qty_ordered: Any
    start_date: Any | None = None
    end_date: Any | None = None
    uom_id: str | None = None
    wo_type: str | None = None
    bom_revision_id: str | None = None
    routing_id: str | None = None
    wip_location_id: str | None = None
    external_sales_ref: str | None = None
    memo: str | None = None


class BuildCreate(Body):
    assembly_item_id: str
    qty_built: Any
    location_id: str | None = None
    uom_id: str | None = None


class UnbuildLine(Body):
    component_item_id: str
    disposition: str = "RETURN_TO_STOCK"
    is_used: bool = False
    condition_code_id: str | None = None
    return_status_id: str | None = None
    target_location_id: str | None = None
    target_bin_id: str | None = None
    allow_resale: bool = False
    value_policy: str = "ORIGINAL_COST"
    unit_value: Any | None = None
    unit_cost: Any | None = None


class UnbuildCreate(Body):
    assembly_item_id: str
    qty_unbuilt: Any
    location_id: str | None = None
    uom_id: str | None = None
    original_assembly_build_id: str | None = None
    reason_code_id: str | None = None
    lines: list[UnbuildLine] = Field(default_factory=list)


class ConfigurationValidate(Body):
    assembly_item_id: str
    context: str = "ATB"
    components: list[dict[str, Any]] | None = None
    context_refs: dict[str, Any] = Field(default_factory=dict)
    override: bool = False
    override_reason: str | None = None


# =================================================================== assemblies / BOM ========
@router.post("/assemblies", status_code=201, dependencies=[_scoped("POST", "/api/v1/assemblies", MFG_WRITE)])
async def create_assembly(payload: AssemblyCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.create_assembly_item(payload.model_dump())
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/assemblies", dependencies=[_scoped("GET", "/api/v1/assemblies", MFG_READ)])
async def list_assemblies(
    svc: ServiceDep, assembly_type: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> dict[str, Any]:
    return {"items": await svc.list_assembly_items(assembly_type=assembly_type, limit=limit)}


@router.get("/assemblies/{assembly_id}", dependencies=[_scoped("GET", "/api/v1/assemblies/{assembly_id}", MFG_READ)])
async def get_assembly(assembly_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"assembly": await svc.get_assembly_item(assembly_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch(
    "/assemblies/{assembly_id}", dependencies=[_scoped("PATCH", "/api/v1/assemblies/{assembly_id}", MFG_WRITE)]
)
async def patch_assembly(assembly_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"assembly": await svc.update_assembly_item(assembly_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/boms", status_code=201, dependencies=[_scoped("POST", "/api/v1/boms", MFG_WRITE)])
async def create_bom(payload: BomCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"bom": await svc.create_bom(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/boms", dependencies=[_scoped("GET", "/api/v1/boms", MFG_READ)])
async def list_boms(svc: ServiceDep, assembly_item_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_boms(assembly_item_id=assembly_item_id, status=status)}


@router.get("/boms/{bom_id}", dependencies=[_scoped("GET", "/api/v1/boms/{bom_id}", MFG_READ)])
async def get_bom(bom_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"bom": await svc.get_bom(bom_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/boms/{bom_id}/activate", dependencies=[_scoped("POST", "/api/v1/boms/{bom_id}/activate", MFG_WRITE)])
async def activate_bom(bom_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"bom": await svc.activate_bom(bom_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/boms/{bom_id}/revisions",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/boms/{bom_id}/revisions", MFG_WRITE)],
)
async def create_revision(bom_id: str, payload: RevisionCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"revision": await svc.create_bom_revision(bom_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/boms/{bom_id}/revisions", dependencies=[_scoped("GET", "/api/v1/boms/{bom_id}/revisions", MFG_READ)])
async def list_revisions(bom_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_bom_revisions(bom_id)}


@router.get(
    "/bom-revisions/{revision_id}", dependencies=[_scoped("GET", "/api/v1/bom-revisions/{revision_id}", MFG_READ)]
)
async def get_revision(revision_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"revision": await svc.get_bom_revision(revision_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch(
    "/bom-revisions/{revision_id}", dependencies=[_scoped("PATCH", "/api/v1/bom-revisions/{revision_id}", MFG_WRITE)]
)
async def patch_revision(revision_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"revision": await svc.update_bom_revision(revision_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/bom-revisions/{revision_id}/activate",
    dependencies=[_scoped("POST", "/api/v1/bom-revisions/{revision_id}/activate", MFG_WRITE)],
)
async def activate_revision(revision_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"revision": await svc.activate_bom_revision(revision_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/bom-revisions/{revision_id}/components",
    dependencies=[_scoped("GET", "/api/v1/bom-revisions/{revision_id}/components", MFG_READ)],
)
async def list_components(revision_id: str, svc: ServiceDep, as_of: date | None = None) -> dict[str, Any]:
    return {"items": await svc.list_bom_components(revision_id, as_of=as_of)}


@router.post(
    "/bom-revisions/{revision_id}/components",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/bom-revisions/{revision_id}/components", MFG_WRITE)],
)
async def add_component(revision_id: str, payload: ComponentCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"component": await svc.add_bom_component(revision_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch(
    "/bom-components/{component_id}",
    dependencies=[_scoped("PATCH", "/api/v1/bom-components/{component_id}", MFG_WRITE)],
)
async def patch_component(component_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"component": await svc.update_bom_component(component_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.delete(
    "/bom-components/{component_id}",
    status_code=204,
    dependencies=[_scoped("DELETE", "/api/v1/bom-components/{component_id}", MFG_WRITE)],
)
async def delete_component(component_id: str, svc: ServiceDep) -> None:
    try:
        await svc.delete_bom_component(component_id)
    except ManufacturingError as exc:
        _raise(exc)


@router.put(
    "/bom-components/{component_id}/control",
    dependencies=[_scoped("PUT", "/api/v1/bom-components/{component_id}/control", MFG_WRITE)],
)
async def put_control(component_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"control": await svc.upsert_component_control(component_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/bom-components/{component_id}/control",
    dependencies=[_scoped("GET", "/api/v1/bom-components/{component_id}/control", MFG_READ)],
)
async def get_control(component_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_component_controls(component_id)}


@router.get(
    "/assemblies/{assembly_id}/bom-inquiry",
    dependencies=[_scoped("GET", "/api/v1/assemblies/{assembly_id}/bom-inquiry", MFG_READ)],
)
async def bom_inquiry(assembly_id: str, svc: ServiceDep, qty: str = "1", as_of: date | None = None) -> dict[str, Any]:
    try:
        return await svc.bom_inquiry(assembly_id, qty=qty, as_of=as_of)
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/assemblies/{assembly_id}/costed-bom-inquiry",
    dependencies=[_scoped("GET", "/api/v1/assemblies/{assembly_id}/costed-bom-inquiry", MFG_COST_VIEW)],
)
async def costed_bom_inquiry(
    assembly_id: str, svc: ServiceDep, qty: str = "1", as_of: date | None = None
) -> dict[str, Any]:
    try:
        return await svc.costed_bom_inquiry(assembly_id, qty=qty, as_of=as_of)
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/component-usage/where-used", dependencies=[_scoped("GET", "/api/v1/component-usage/where-used", MFG_READ)]
)
async def where_used(svc: ServiceDep, component_item_id: str = Query(...)) -> dict[str, Any]:
    try:
        return await svc.where_used(component_item_id)
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assemblies/{assembly_id}/explode",
    dependencies=[_scoped("POST", "/api/v1/assemblies/{assembly_id}/explode", MFG_READ)],
)
async def explode(assembly_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        lines = await svc.resolve_bom(
            assembly_id, qty_ordered=payload.model_dump().get("qty") or 1, as_of=payload.model_dump().get("as_of")
        )
        return {"lines": lines, "count": len(lines)}
    except ManufacturingError as exc:
        _raise(exc)


# =================================================================== routing / work centres ==
@router.post("/routings", status_code=201, dependencies=[_scoped("POST", "/api/v1/routings", MFG_WRITE)])
async def create_routing(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"routing": await svc.create_routing(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/routings", dependencies=[_scoped("GET", "/api/v1/routings", MFG_READ)])
async def list_routings(svc: ServiceDep, item_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_routings(item_id=item_id, status=status)}


@router.get("/routings/{routing_id}", dependencies=[_scoped("GET", "/api/v1/routings/{routing_id}", MFG_READ)])
async def get_routing(routing_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"routing": await svc.get_routing(routing_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch("/routings/{routing_id}", dependencies=[_scoped("PATCH", "/api/v1/routings/{routing_id}", MFG_WRITE)])
async def patch_routing(routing_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"routing": await svc.update_routing(routing_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/routings/{routing_id}/activate",
    dependencies=[_scoped("POST", "/api/v1/routings/{routing_id}/activate", MFG_WRITE)],
)
async def activate_routing(routing_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"routing": await svc.activate_routing(routing_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/routings/{routing_id}/steps",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/routings/{routing_id}/steps", MFG_WRITE)],
)
async def add_routing_step(routing_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"step": await svc.add_routing_step(routing_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch("/routing-steps/{step_id}", dependencies=[_scoped("PATCH", "/api/v1/routing-steps/{step_id}", MFG_WRITE)])
async def patch_routing_step(step_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"step": await svc.update_routing_step(step_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/routing-steps/{step_id}/charges",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/routing-steps/{step_id}/charges", MFG_WRITE)],
)
async def add_step_charge(step_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"charge": await svc.add_routing_step_charge(step_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/work-centers", status_code=201, dependencies=[_scoped("POST", "/api/v1/work-centers", MFG_WRITE)])
async def create_work_center(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"work_center": await svc.create_work_center(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/work-centers", dependencies=[_scoped("GET", "/api/v1/work-centers", MFG_READ)])
async def list_work_centers(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_work_centers()}


@router.patch(
    "/work-centers/{work_center_id}",
    dependencies=[_scoped("PATCH", "/api/v1/work-centers/{work_center_id}", MFG_WRITE)],
)
async def patch_work_center(work_center_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"work_center": await svc.update_work_center(work_center_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-center-groups", status_code=201, dependencies=[_scoped("POST", "/api/v1/work-center-groups", MFG_WRITE)]
)
async def create_work_center_group(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"group": await svc.create_work_center_group(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/work-center-groups", dependencies=[_scoped("GET", "/api/v1/work-center-groups", MFG_READ)])
async def list_work_center_groups(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_work_center_groups()}


@router.post(
    "/work-center-calendars",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/work-center-calendars", MFG_WRITE)],
)
async def create_calendar(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"calendar": await svc.create_work_center_calendar(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/work-center-calendars", dependencies=[_scoped("GET", "/api/v1/work-center-calendars", MFG_READ)])
async def list_calendars(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_work_center_calendars()}


@router.put(
    "/work-center-calendars/{calendar_id}/days",
    dependencies=[_scoped("PUT", "/api/v1/work-center-calendars/{calendar_id}/days", MFG_WRITE)],
)
async def put_calendar_days(calendar_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"days": await svc.upsert_calendar_days(calendar_id, payload.model_dump().get("days") or [])}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/manufacturing-cost-templates",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing-cost-templates", MFG_WRITE)],
)
async def create_cost_template(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"template": await svc.create_cost_template(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/manufacturing-cost-templates", dependencies=[_scoped("GET", "/api/v1/manufacturing-cost-templates", MFG_READ)]
)
async def list_cost_templates(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_cost_templates()}


@router.post(
    "/manufacturing-cost-templates/{template_id}/lines",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing-cost-templates/{template_id}/lines", MFG_WRITE)],
)
async def add_cost_template_line(template_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"line": await svc.add_cost_template_line(template_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/manufacturing-preferences", dependencies=[_scoped("GET", "/api/v1/manufacturing-preferences", MFG_READ)])
async def get_preferences(svc: ServiceDep) -> dict[str, Any]:
    return await svc.get_preferences()


@router.patch(
    "/manufacturing-preferences", dependencies=[_scoped("PATCH", "/api/v1/manufacturing-preferences", MFG_ADMIN)]
)
async def patch_preferences(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return await svc.update_preferences(payload.model_dump())


# =================================================================== work orders ============
@router.post("/work-orders", status_code=201, dependencies=[_scoped("POST", "/api/v1/work-orders", MFG_WRITE)])
async def create_work_order(payload: WorkOrderCreate, svc: ServiceDep, principal: PrincipalDep) -> dict[str, Any]:
    if payload.model_dump().get("compat_override") and COMPAT_OVERRIDE not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN_SCOPE",
                "message": "compatibility override requires manufacturing.compat.override",
            },
        )
    try:
        return {"work_order": await svc.create_work_order(payload.model_dump(exclude_none=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/work-orders", dependencies=[_scoped("GET", "/api/v1/work-orders", MFG_READ)])
async def list_work_orders(
    svc: ServiceDep,
    status: str | None = None,
    assembly_item_id: str | None = None,
    external_sales_ref: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return {
        "items": await svc.list_work_orders(
            status=status,
            assembly_item_id=assembly_item_id,
            external_sales_ref=external_sales_ref,
            limit=limit,
            offset=offset,
        )
    }


@router.get(
    "/work-orders/{work_order_id}", dependencies=[_scoped("GET", "/api/v1/work-orders/{work_order_id}", MFG_READ)]
)
async def get_work_order(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"work_order": await svc.get_work_order(work_order_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.patch(
    "/work-orders/{work_order_id}", dependencies=[_scoped("PATCH", "/api/v1/work-orders/{work_order_id}", MFG_WRITE)]
)
async def patch_work_order(work_order_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"work_order": await svc.update_work_order(work_order_id, payload.model_dump(exclude_unset=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/firm",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/firm", MFG_WRITE)],
)
async def firm_work_order(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"work_order": await svc.firm_work_order(work_order_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/release",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/release", MFG_RELEASE)],
)
async def release_work_order(work_order_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    body = payload.model_dump(exclude_none=True) if payload is not None else {}
    try:
        return {"work_order": await svc.release_work_order(work_order_id, wip_location_id=body.get("wip_location_id"))}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/issue",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/issue", MFG_POST)],
)
async def issue_components(work_order_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"wip_transaction": await svc.issue_components(work_order_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/complete",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/complete", MFG_POST)],
)
async def complete_work_order(work_order_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"wip_transaction": await svc.complete_work_order(work_order_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/backflush",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/backflush", MFG_POST)],
)
async def backflush_work_order(work_order_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return await svc.backflush_work_order(work_order_id, payload.model_dump() if payload else {})
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/close",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/close", MFG_POST)],
)
async def close_work_order(work_order_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"work_order": await svc.close_work_order(work_order_id, payload.model_dump() if payload else {})}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/void",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/void", MFG_WRITE)],
)
async def void_work_order(work_order_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"work_order": await svc.void_work_order(work_order_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/work-orders/{work_order_id}/reopen",
    dependencies=[_scoped("POST", "/api/v1/work-orders/{work_order_id}/reopen", MFG_WRITE)],
)
async def reopen_work_order(work_order_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"work_order": await svc.reopen_work_order(work_order_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/work-orders/{work_order_id}/components",
    dependencies=[_scoped("GET", "/api/v1/work-orders/{work_order_id}/components", MFG_READ)],
)
async def work_order_components(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        work_order = await svc.get_work_order(work_order_id)
    except ManufacturingError as exc:
        _raise(exc)
    return {"items": work_order["components"]}


@router.get(
    "/work-orders/{work_order_id}/components/pick-requirements",
    dependencies=[_scoped("GET", "/api/v1/work-orders/{work_order_id}/components/pick-requirements", MFG_READ)],
)
async def pick_requirements(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.pick_requirements(work_order_id)
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/work-orders/{work_order_id}/operations",
    dependencies=[_scoped("GET", "/api/v1/work-orders/{work_order_id}/operations", MFG_READ)],
)
async def work_order_operations(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        work_order = await svc.get_work_order(work_order_id)
    except ManufacturingError as exc:
        _raise(exc)
    return {"items": work_order["operations"]}


@router.post(
    "/work-order-operations/{operation_id}/complete",
    dependencies=[_scoped("POST", "/api/v1/work-order-operations/{operation_id}/complete", MFG_POST)],
)
async def complete_operation(operation_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"completion": await svc.add_operation_completion(operation_id, payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/work-orders/{work_order_id}/wip-balance",
    dependencies=[_scoped("GET", "/api/v1/work-orders/{work_order_id}/wip-balance", MFG_READ)],
)
async def wip_balance(work_order_id: str, svc: ServiceDep) -> dict[str, Any]:
    return await svc.get_wip_balance(work_order_id)


# =================================================================== WIP / build / unbuild ====
@router.get(
    "/wip-transactions/{wip_transaction_id}",
    dependencies=[_scoped("GET", "/api/v1/wip-transactions/{wip_transaction_id}", MFG_READ)],
)
async def get_wip_transaction(wip_transaction_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"wip_transaction": await svc.get_wip_transaction(wip_transaction_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/wip-transactions/{wip_transaction_id}/reverse",
    dependencies=[_scoped("POST", "/api/v1/wip-transactions/{wip_transaction_id}/reverse", MFG_POST)],
)
async def reverse_wip_transaction(
    wip_transaction_id: str, svc: ServiceDep, payload: Body | None = None
) -> dict[str, Any]:
    try:
        return {
            "wip_transaction": await svc.reverse_wip_transaction(
                wip_transaction_id, payload.model_dump() if payload else {}
            )
        }
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/assembly-builds", status_code=201, dependencies=[_scoped("POST", "/api/v1/assembly-builds", MFG_WRITE)])
async def create_build(payload: BuildCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"build": await svc.create_build(payload.model_dump(exclude_none=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/assembly-builds", dependencies=[_scoped("GET", "/api/v1/assembly-builds", MFG_READ)])
async def list_builds(svc: ServiceDep, assembly_item_id: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_builds(assembly_item_id=assembly_item_id)}


@router.get(
    "/assembly-builds/{build_id}", dependencies=[_scoped("GET", "/api/v1/assembly-builds/{build_id}", MFG_READ)]
)
async def get_build(build_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"build": await svc.get_build(build_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assembly-builds/{build_id}/post",
    dependencies=[_scoped("POST", "/api/v1/assembly-builds/{build_id}/post", MFG_POST)],
)
async def post_build(build_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"build": await svc.post_build(build_id, payload.model_dump() if payload else {})}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assembly-builds/{build_id}/reverse",
    dependencies=[_scoped("POST", "/api/v1/assembly-builds/{build_id}/reverse", MFG_POST)],
)
async def reverse_build(build_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"build": await svc.reverse_build(build_id, payload.model_dump() if payload else {})}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assembly-unbuilds", status_code=201, dependencies=[_scoped("POST", "/api/v1/assembly-unbuilds", MFG_WRITE)]
)
async def create_unbuild(payload: UnbuildCreate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"unbuild": await svc.create_unbuild(payload.model_dump(exclude_none=True))}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/assembly-unbuilds", dependencies=[_scoped("GET", "/api/v1/assembly-unbuilds", MFG_READ)])
async def list_unbuilds(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_unbuilds()}


@router.get(
    "/assembly-unbuilds/{unbuild_id}", dependencies=[_scoped("GET", "/api/v1/assembly-unbuilds/{unbuild_id}", MFG_READ)]
)
async def get_unbuild(unbuild_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"unbuild": await svc.get_unbuild(unbuild_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assembly-unbuilds/{unbuild_id}/post",
    dependencies=[_scoped("POST", "/api/v1/assembly-unbuilds/{unbuild_id}/post", MFG_POST)],
)
async def post_unbuild(unbuild_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"unbuild": await svc.post_unbuild(unbuild_id, payload.model_dump() if payload else {})}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/assembly-unbuilds/{unbuild_id}/reverse",
    dependencies=[_scoped("POST", "/api/v1/assembly-unbuilds/{unbuild_id}/reverse", UNBUILD_DISPOSE)],
)
async def reverse_unbuild(unbuild_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return {"unbuild": await svc.reverse_unbuild(unbuild_id, payload.model_dump() if payload else {})}
    except ManufacturingError as exc:
        _raise(exc)


# =================================================================== outsourced =============
@router.post(
    "/outsourced-operations",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/outsourced-operations", MFG_WRITE)],
)
async def create_outsourced_operation(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"operation": await svc.create_outsourced_operation(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/outsourced-operations", dependencies=[_scoped("GET", "/api/v1/outsourced-operations", MFG_READ)])
async def list_outsourced_operations(svc: ServiceDep, status: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_outsourced_operations(status=status)}


@router.post(
    "/outsourced-operations/{operation_id}/create-po",
    dependencies=[_scoped("POST", "/api/v1/outsourced-operations/{operation_id}/create-po", MFG_WRITE)],
)
async def create_outsourced_po(operation_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.create_outsourced_po(operation_id, payload.model_dump())
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/outsourced-operations/{operation_id}/receive",
    dependencies=[_scoped("POST", "/api/v1/outsourced-operations/{operation_id}/receive", MFG_POST)],
)
async def receive_outsourced_operation(operation_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.receive_outsourced_operation(operation_id, payload.model_dump())
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/outsourced-operations/{operation_id}/close",
    dependencies=[_scoped("POST", "/api/v1/outsourced-operations/{operation_id}/close", MFG_WRITE)],
)
async def close_outsourced_operation(operation_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"operation": await svc.close_outsourced_operation(operation_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/outsourced-operations/consolidate",
    dependencies=[_scoped("POST", "/api/v1/outsourced-operations/consolidate", MFG_WRITE)],
)
async def consolidate_outsourced(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.consolidate_outsourced_operations(payload.model_dump())
    except ManufacturingError as exc:
        _raise(exc)


# =================================================================== ECO ====================
@router.post("/ecos", status_code=201, dependencies=[_scoped("POST", "/api/v1/ecos", MFG_WRITE)])
async def create_eco(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"eco": await svc.create_eco(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/ecos", dependencies=[_scoped("GET", "/api/v1/ecos", MFG_READ)])
async def list_ecos(svc: ServiceDep, status: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_ecos(status=status)}


@router.get("/ecos/{eco_id}", dependencies=[_scoped("GET", "/api/v1/ecos/{eco_id}", MFG_READ)])
async def get_eco(eco_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"eco": await svc.get_eco(eco_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/ecos/{eco_id}/submit", dependencies=[_scoped("POST", "/api/v1/ecos/{eco_id}/submit", MFG_WRITE)])
async def submit_eco(eco_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"eco": await svc.submit_eco(eco_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/ecos/{eco_id}/approve", dependencies=[_scoped("POST", "/api/v1/ecos/{eco_id}/approve", MFG_APPROVE)])
async def approve_eco(eco_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"eco": await svc.approve_eco(eco_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.post("/ecos/{eco_id}/implement", dependencies=[_scoped("POST", "/api/v1/ecos/{eco_id}/implement", MFG_APPROVE)])
async def implement_eco(eco_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"eco": await svc.implement_eco(eco_id)}
    except ManufacturingError as exc:
        _raise(exc)


# =================================================================== scheduling / capacity ==
@router.post("/scheduling-runs", status_code=201, dependencies=[_scoped("POST", "/api/v1/scheduling-runs", MFG_WRITE)])
async def create_scheduling_run(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"scheduling_run": await svc.create_scheduling_run(payload.model_dump())}
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/scheduling-runs/{run_id}", dependencies=[_scoped("GET", "/api/v1/scheduling-runs/{run_id}", MFG_READ)])
async def get_scheduling_run(run_id: str, svc: ServiceDep) -> dict[str, Any]:
    try:
        return {"scheduling_run": await svc.get_scheduling_run(run_id)}
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/scheduling-runs/{run_id}/scheduled-operations",
    dependencies=[_scoped("GET", "/api/v1/scheduling-runs/{run_id}/scheduled-operations", MFG_READ)],
)
async def list_scheduled_operations(run_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_scheduled_operations(run_id)}


@router.post(
    "/scheduling-runs/{run_id}/apply",
    dependencies=[_scoped("POST", "/api/v1/scheduling-runs/{run_id}/apply", MFG_WRITE)],
)
async def apply_scheduling_run(run_id: str, svc: ServiceDep, payload: Body | None = None) -> dict[str, Any]:
    try:
        return await svc.apply_scheduling_run(run_id, payload.model_dump() if payload else {})
    except ManufacturingError as exc:
        _raise(exc)


@router.get("/capacity-plans", dependencies=[_scoped("GET", "/api/v1/capacity-plans", MFG_READ)])
async def capacity_plans(svc: ServiceDep, work_center_id: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_capacity_plans(work_center_id=work_center_id)}


@router.get("/work-center-loads", dependencies=[_scoped("GET", "/api/v1/work-center-loads", MFG_READ)])
async def work_center_loads(svc: ServiceDep) -> dict[str, Any]:
    return {
        "items": await svc._all(
            "SELECT * FROM manufacturing.work_center_load WHERE tenant_id = :tenant_id ORDER BY load_date LIMIT 1000"
        )
    }


# =================================================================== available-to-build ====
@router.get("/available-to-build", dependencies=[_scoped("GET", "/api/v1/available-to-build", MFG_READ)])
async def available_to_build(
    svc: ServiceDep,
    item_id: str = Query(...),
    location_id: str | None = None,
    as_of: date | None = None,
    level_mode: str | None = None,
) -> dict[str, Any]:
    try:
        return await svc.available_to_build(item_id, location_id=location_id, as_of=as_of, level_mode=level_mode)
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/available-to-build/calculate", dependencies=[_scoped("POST", "/api/v1/available-to-build/calculate", MFG_READ)]
)
async def calculate_available_to_build(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    body = payload.model_dump()
    items = body.get("items") or [{"item_id": body.get("item_id")}]
    results = []
    for item in items:
        try:
            results.append(
                await svc.available_to_build(
                    item.get("item_id"), location_id=body.get("location_id"), as_of=body.get("as_of")
                )
            )
        except ManufacturingError as exc:
            results.append({"item_id": item.get("item_id"), "error": exc.message})
    return {"results": results}


# =================================================================== compatibility ==========
@router.get(
    "/manufacturing/condition-codes", dependencies=[_scoped("GET", "/api/v1/manufacturing/condition-codes", MFG_READ)]
)
async def list_condition_codes(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_condition_codes()}


@router.post(
    "/manufacturing/condition-codes",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing/condition-codes", MFG_ADMIN)],
)
async def create_condition_code(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"condition": await svc.create_condition_code(payload.model_dump())}


@router.patch(
    "/manufacturing/condition-codes/{condition_id}",
    dependencies=[_scoped("PATCH", "/api/v1/manufacturing/condition-codes/{condition_id}", MFG_ADMIN)],
)
async def patch_condition_code(condition_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"condition": await svc.update_condition_code(condition_id, payload.model_dump(exclude_unset=True))}


@router.get(
    "/manufacturing/compat/attributes",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/compat/attributes", COMPAT_MANAGE)],
)
async def list_attributes(svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.list_attribute_defs()}


@router.post(
    "/manufacturing/compat/attributes",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/attributes", COMPAT_MANAGE)],
)
async def create_attribute(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"attribute": await svc.create_attribute_def(payload.model_dump())}


@router.patch(
    "/manufacturing/compat/attributes/{attribute_id}",
    dependencies=[_scoped("PATCH", "/api/v1/manufacturing/compat/attributes/{attribute_id}", COMPAT_MANAGE)],
)
async def patch_attribute(attribute_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"attribute": await svc.update_attribute_def(attribute_id, payload.model_dump(exclude_unset=True))}


@router.get(
    "/items/{item_id}/compat-attributes",
    dependencies=[_scoped("GET", "/api/v1/items/{item_id}/compat-attributes", COMPAT_MANAGE)],
)
async def get_item_attributes(item_id: str, svc: ServiceDep) -> dict[str, Any]:
    return await svc.get_item_attributes(item_id)


@router.put(
    "/items/{item_id}/compat-attributes",
    dependencies=[_scoped("PUT", "/api/v1/items/{item_id}/compat-attributes", COMPAT_MANAGE)],
)
async def put_item_attributes(item_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"items": await svc.set_item_attributes(item_id, payload.model_dump().get("values") or [])}


@router.get(
    "/manufacturing/compat/rule-sets",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/compat/rule-sets", COMPAT_MANAGE)],
)
async def list_rule_sets(svc: ServiceDep, status: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_rule_sets(status=status)}


@router.post(
    "/manufacturing/compat/rule-sets",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/rule-sets", COMPAT_MANAGE)],
)
async def create_rule_set(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"rule_set": await svc.create_rule_set(payload.model_dump())}


@router.get(
    "/manufacturing/compat/rule-sets/{rule_set_id}",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", COMPAT_MANAGE)],
)
async def get_rule_set(rule_set_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"rule_set": await svc.get_rule_set(rule_set_id)}


@router.patch(
    "/manufacturing/compat/rule-sets/{rule_set_id}",
    dependencies=[_scoped("PATCH", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}", COMPAT_MANAGE)],
)
async def patch_rule_set(rule_set_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"rule_set": await svc.update_rule_set(rule_set_id, payload.model_dump(exclude_unset=True))}


@router.post(
    "/manufacturing/compat/rule-sets/{rule_set_id}/activate",
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/activate", COMPAT_MANAGE)],
)
async def activate_rule_set(rule_set_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"rule_set": await svc.activate_rule_set(rule_set_id)}


@router.post(
    "/manufacturing/compat/rule-sets/{rule_set_id}/retire",
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/retire", COMPAT_MANAGE)],
)
async def retire_rule_set(rule_set_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"rule_set": await svc.retire_rule_set(rule_set_id)}


@router.post(
    "/manufacturing/compat/rule-sets/{rule_set_id}/rules",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/rule-sets/{rule_set_id}/rules", COMPAT_MANAGE)],
)
async def add_rule(rule_set_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"rule": await svc.add_rule(rule_set_id, payload.model_dump())}


@router.patch(
    "/manufacturing/compat/rules/{rule_id}",
    dependencies=[_scoped("PATCH", "/api/v1/manufacturing/compat/rules/{rule_id}", COMPAT_MANAGE)],
)
async def patch_rule(rule_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"rule": await svc.update_rule(rule_id, payload.model_dump(exclude_unset=True))}


@router.get(
    "/manufacturing/compat/slots", dependencies=[_scoped("GET", "/api/v1/manufacturing/compat/slots", COMPAT_MANAGE)]
)
async def list_slots(svc: ServiceDep, assembly_item_id: str | None = None) -> dict[str, Any]:
    return {"items": await svc.list_slots(assembly_item_id=assembly_item_id)}


@router.post(
    "/manufacturing/compat/slots",
    status_code=201,
    dependencies=[_scoped("POST", "/api/v1/manufacturing/compat/slots", COMPAT_MANAGE)],
)
async def create_slot(payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"slot": await svc.create_slot(payload.model_dump())}


@router.patch(
    "/manufacturing/compat/slots/{slot_id}",
    dependencies=[_scoped("PATCH", "/api/v1/manufacturing/compat/slots/{slot_id}", COMPAT_MANAGE)],
)
async def patch_slot(slot_id: str, payload: Body, svc: ServiceDep) -> dict[str, Any]:
    return {"slot": await svc.update_slot(slot_id, payload.model_dump(exclude_unset=True))}


@router.post(
    "/manufacturing/configurations/validate",
    dependencies=[_scoped("POST", "/api/v1/manufacturing/configurations/validate", COMPAT_VALIDATE)],
)
async def validate_configuration(
    payload: ConfigurationValidate, svc: ServiceDep, principal: PrincipalDep
) -> dict[str, Any]:
    if payload.override and COMPAT_OVERRIDE not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN_SCOPE",
                "message": "compatibility override requires manufacturing.compat.override",
            },
        )
    try:
        return await svc.validate_configuration(payload.model_dump(exclude_none=True))
    except ManufacturingError as exc:
        _raise(exc)


@router.post(
    "/manufacturing/configurations/simulate",
    dependencies=[_scoped("POST", "/api/v1/manufacturing/configurations/simulate", COMPAT_VALIDATE)],
)
async def simulate_configuration(payload: ConfigurationValidate, svc: ServiceDep) -> dict[str, Any]:
    try:
        return await svc.validate_configuration(payload.model_dump(exclude_none=True), simulate=True)
    except ManufacturingError as exc:
        _raise(exc)


@router.get(
    "/manufacturing/configurations/{configuration_id}",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/configurations/{configuration_id}", COMPAT_VALIDATE)],
)
async def get_configuration(configuration_id: str, svc: ServiceDep) -> dict[str, Any]:
    return {"configuration": await svc.get_compat_configuration(configuration_id)}


@router.get(
    "/manufacturing/configurations",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/configurations", COMPAT_VALIDATE)],
)
async def list_configurations(
    svc: ServiceDep, assembly_item_id: str | None = None, result: str | None = None
) -> dict[str, Any]:
    return {"items": await svc.list_compat_configurations(assembly_item_id=assembly_item_id, result=result)}


@router.get(
    "/manufacturing/compat/report",
    dependencies=[_scoped("GET", "/api/v1/manufacturing/compat/report", COMPAT_VALIDATE)],
)
async def compat_report(svc: ServiceDep, assembly_item_id: str | None = None) -> dict[str, Any]:
    return await svc.compat_report(assembly_item_id=assembly_item_id)


__all__ = ["ROUTE_SCOPES", "router"]
