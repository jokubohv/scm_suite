# ruff: noqa: B008 - concise FastAPI Body(...) defaults for this router.
"""FS-14 extension-registry API (`/api/v1`; warranty, customer parts, Auto Assign UPC; catalogued scopes)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.modules.extensions.service import ExtensionService

router = APIRouter(prefix="/api/v1", tags=["extensions"])

READ = "extensions.read"
WARRANTY = "extensions.warranty.write"
CLAIMS = "extensions.claims.write"
CUSTOMER_PART = "extensions.customerpart.write"
UPC = "extensions.upc.write"
SETTINGS = "platform.flag.manage"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/items/{item_id}/warranty"): READ,
    ("PUT", "/api/v1/items/{item_id}/warranty"): WARRANTY,
    ("GET", "/api/v1/warranty/registrations"): READ,
    ("POST", "/api/v1/warranty/registrations"): WARRANTY,
    ("GET", "/api/v1/warranty/registrations/{registration_id}"): READ,
    ("POST", "/api/v1/warranty/registrations/{registration_id}/void"): WARRANTY,
    ("POST", "/api/v1/warranty/registrations/import"): WARRANTY,
    ("GET", "/api/v1/warranty/coverage"): READ,
    ("GET", "/api/v1/warranty/claims"): READ,
    ("POST", "/api/v1/warranty/claims"): CLAIMS,
    ("GET", "/api/v1/warranty/claims/{claim_id}"): READ,
    ("POST", "/api/v1/warranty/claims/{claim_id}/validate"): CLAIMS,
    ("POST", "/api/v1/warranty/claims/{claim_id}/approve"): CLAIMS,
    ("POST", "/api/v1/warranty/claims/{claim_id}/reject"): CLAIMS,
    ("POST", "/api/v1/warranty/claims/{claim_id}/actions"): CLAIMS,
    ("POST", "/api/v1/warranty/claims/{claim_id}/complete"): CLAIMS,
    ("POST", "/api/v1/warranty/claims/{claim_id}/cancel"): CLAIMS,
    ("GET", "/api/v1/warranty/repair-orders"): READ,
    ("POST", "/api/v1/warranty/repair-orders"): WARRANTY,
    ("PATCH", "/api/v1/warranty/repair-orders"): WARRANTY,
    ("PATCH", "/api/v1/warranty/repair-orders/{repair_id}"): WARRANTY,
    ("GET", "/api/v1/customer-parts"): READ,
    ("POST", "/api/v1/customer-parts"): CUSTOMER_PART,
    ("GET", "/api/v1/customer-parts/{mapping_id}"): READ,
    ("PATCH", "/api/v1/customer-parts/{mapping_id}"): CUSTOMER_PART,
    ("POST", "/api/v1/customer-parts/import"): CUSTOMER_PART,
    ("GET", "/api/v1/customer-parts/resolve"): READ,
    ("GET", "/api/v1/items/{item_id}/customer-parts"): READ,
    ("GET", "/api/v1/upc-registry"): READ,
    ("POST", "/api/v1/upc-registry"): UPC,
    ("GET", "/api/v1/upc-registry/{registry_id}"): READ,
    ("POST", "/api/v1/upc-registry/{registry_id}/retire"): UPC,
    ("POST", "/api/v1/upc-registry/assign-batch"): UPC,
    ("GET", "/api/v1/upc-registry/validate"): READ,
    ("GET", "/api/v1/upc-registry/monitoring"): READ,
    ("GET", "/api/v1/extensions/settings"): READ,
    ("PATCH", "/api/v1/extensions/settings"): SETTINGS,
}
Idem = Annotated[str | None, Header(alias="Idempotency-Key")]


def _svc(db: DbDep, principal: PrincipalDep) -> ExtensionService:
    return ExtensionService(db, principal.tenant_id)


@router.post("/customer-parts/import", dependencies=[Depends(require_scope(CUSTOMER_PART))])
async def import_customer_parts(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).import_customer_parts(payload.get("rows") or [], actor_id=principal.app_user_id)


@router.get("/customer-parts/resolve", dependencies=[Depends(require_scope(READ))])
async def resolve_customer_part(
    db: DbDep, principal: PrincipalDep, external_customer_ref: str, part_number: str
) -> dict[str, Any]:
    return await _svc(db, principal).resolve_customer_part(
        external_customer_ref=external_customer_ref, part_number=part_number
    )


@router.post("/upc-registry/assign-batch", dependencies=[Depends(require_scope(UPC))])
async def assign_batch(
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).assign_batch(
        limit=int(payload.get("limit") or 100), actor_id=principal.app_user_id
    )


@router.get("/upc-registry/validate", dependencies=[Depends(require_scope(READ))])
async def validate_upc(db: DbDep, principal: PrincipalDep, upc_code: str) -> dict[str, Any]:
    return await _svc(db, principal).validate_upc(upc_code)


@router.get("/upc-registry/monitoring", dependencies=[Depends(require_scope(READ))])
async def monitoring(db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).monitoring()


@router.get("/items/{item_id}/warranty", dependencies=[Depends(require_scope(READ))])
async def get_item_warranty(item_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_item_warranty(item_id)


@router.put("/items/{item_id}/warranty", dependencies=[Depends(require_scope(WARRANTY))])
async def put_item_warranty(
    item_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).put_item_warranty(
        item_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/warranty/registrations", dependencies=[Depends(require_scope(READ))])
async def list_registrations(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str | None = None,
    serial_id: str | None = None,
    status: str | None = None,
    customer_ref: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _svc(db, principal).list_registrations(
        item_id=item_id, serial_id=serial_id, status=status, customer_ref=customer_ref, limit=limit
    )


@router.post("/warranty/registrations", status_code=201, dependencies=[Depends(require_scope(WARRANTY))])
async def create_registration(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).create_registration(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/warranty/registrations/{registration_id}", dependencies=[Depends(require_scope(READ))])
async def get_registration(registration_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_registration(registration_id)


@router.post("/warranty/registrations/{registration_id}/void", dependencies=[Depends(require_scope(WARRANTY))])
async def void_registration(
    registration_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).void_registration(
        registration_id, str(payload.get("reason") or "voided"), actor_id=principal.app_user_id
    )


@router.post("/warranty/registrations/import", dependencies=[Depends(require_scope(WARRANTY))])
async def import_registrations(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).import_registrations(payload.get("rows") or [], actor_id=principal.app_user_id)


@router.get("/warranty/coverage", dependencies=[Depends(require_scope(READ))])
async def warranty_coverage(
    db: DbDep, principal: PrincipalDep, item_id: str, serial_id: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    return await _svc(db, principal).coverage(item_id=item_id, serial_id=serial_id, as_of=as_of)


@router.get("/warranty/claims", dependencies=[Depends(require_scope(READ))])
async def list_claims(
    db: DbDep,
    principal: PrincipalDep,
    status: str | None = None,
    item_id: str | None = None,
    serial_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _svc(db, principal).list_claims(status=status, item_id=item_id, serial_id=serial_id, limit=limit)


@router.post("/warranty/claims", status_code=201, dependencies=[Depends(require_scope(CLAIMS))])
async def create_claim(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).create_claim(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/warranty/claims/{claim_id}", dependencies=[Depends(require_scope(READ))])
async def get_claim(claim_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_claim(claim_id)


@router.post("/warranty/claims/{claim_id}/validate", dependencies=[Depends(require_scope(CLAIMS))])
async def validate_claim(
    claim_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).validate_claim(
        claim_id, as_of=payload.get("as_of"), override_ref=payload.get("override_ref"), actor_id=principal.app_user_id
    )


@router.post("/warranty/claims/{claim_id}/approve", dependencies=[Depends(require_scope(CLAIMS))])
async def approve_claim(
    claim_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).approve_claim(
        claim_id,
        approval_ref=payload.get("approval_ref"),
        override_reason=payload.get("override_reason"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/warranty/claims/{claim_id}/reject", dependencies=[Depends(require_scope(CLAIMS))])
async def reject_claim(
    claim_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).reject_claim(
        claim_id,
        str(payload.get("reason") or "rejected"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/warranty/claims/{claim_id}/actions", dependencies=[Depends(require_scope(CLAIMS))])
async def add_claim_action(
    claim_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).add_claim_action(
        claim_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/warranty/claims/{claim_id}/complete", dependencies=[Depends(require_scope(CLAIMS))])
async def complete_claim(
    claim_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).complete_claim(claim_id, payload.get("notes"), actor_id=principal.app_user_id)


@router.post("/warranty/claims/{claim_id}/cancel", dependencies=[Depends(require_scope(CLAIMS))])
async def cancel_claim(
    claim_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).cancel_claim(
        claim_id, str(payload.get("reason") or "cancelled"), actor_id=principal.app_user_id
    )


@router.get("/warranty/repair-orders", dependencies=[Depends(require_scope(READ))])
async def list_repair_orders(
    db: DbDep,
    principal: PrincipalDep,
    status: str | None = None,
    claim_id: str | None = None,
    item_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _svc(db, principal).list_repair_orders(status=status, claim_id=claim_id, item_id=item_id, limit=limit)


@router.post("/warranty/repair-orders", status_code=201, dependencies=[Depends(require_scope(WARRANTY))])
async def create_repair_order(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).create_repair_order(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.patch("/warranty/repair-orders/{repair_id}", dependencies=[Depends(require_scope(WARRANTY))])
async def patch_repair_order(
    repair_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _svc(db, principal).update_repair_order(repair_id, payload, actor_id=principal.app_user_id)


@router.get("/customer-parts", dependencies=[Depends(require_scope(READ))])
async def list_customer_parts(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str | None = None,
    customer_ref: str | None = None,
    is_active: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _svc(db, principal).list_customer_parts(
        item_id=item_id, customer_ref=customer_ref, is_active=is_active, limit=limit
    )


@router.post("/customer-parts", status_code=201, dependencies=[Depends(require_scope(CUSTOMER_PART))])
async def create_customer_part(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).create_customer_part(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/customer-parts/{mapping_id}", dependencies=[Depends(require_scope(READ))])
async def get_customer_part(mapping_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_customer_part(mapping_id)


@router.patch("/customer-parts/{mapping_id}", dependencies=[Depends(require_scope(CUSTOMER_PART))])
async def patch_customer_part(
    mapping_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _svc(db, principal).update_customer_part(mapping_id, payload, actor_id=principal.app_user_id)


@router.get("/items/{item_id}/customer-parts", dependencies=[Depends(require_scope(READ))])
async def list_item_customer_parts(item_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).list_item_customer_parts(item_id)


@router.get("/upc-registry", dependencies=[Depends(require_scope(READ))])
async def list_upc(
    db: DbDep, principal: PrincipalDep, status: str | None = None, item_id: str | None = None, limit: int = 100
) -> dict[str, Any]:
    return await _svc(db, principal).list_upc(status=status, item_id=item_id, limit=limit)


@router.post("/upc-registry", status_code=201, dependencies=[Depends(require_scope(UPC))])
async def assign_upc(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).assign_upc(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/upc-registry/{registry_id}", dependencies=[Depends(require_scope(READ))])
async def get_upc(registry_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_upc(registry_id)


@router.post("/upc-registry/{registry_id}/retire", dependencies=[Depends(require_scope(UPC))])
async def retire_upc(
    registry_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: Idem = None,
) -> dict[str, Any]:
    return await _svc(db, principal).retire_upc(registry_id, payload, actor_id=principal.app_user_id)


@router.get("/extensions/settings", dependencies=[Depends(require_scope(READ))])
async def get_settings(db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _svc(db, principal).get_settings()


@router.patch("/extensions/settings", dependencies=[Depends(require_scope(SETTINGS))])
async def patch_settings(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: Idem = None
) -> dict[str, Any]:
    return await _svc(db, principal).patch_settings(payload, actor_id=principal.app_user_id)


# ===================================================== collection-level repair-order update =====
# The frozen FS-14 contract declares ``PATCH /warranty/repair-orders`` with the repair id in the body;
# this delegates to the same service method as the by-id PATCH handler.
@router.patch("/warranty/repair-orders", dependencies=[Depends(require_scope(WARRANTY))])
async def patch_repair_order_collection(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    repair_id = payload.get("id") or payload.get("repair_id")
    if not repair_id:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "id or repair_id is required", "details": []},
        )
    return await _svc(db, principal).update_repair_order(
        str(repair_id), payload, actor_id=principal.app_user_id
    )


# ------------------------------------------------------------------ canonical {id} aliases ----
# The frozen FS-14 contract declares canonical ``{id}`` resource placeholders while the implementation
# historically mounted resource-specific names (``{mapping_id}``/``{claim_id}``/...). These aliases mount
# the declared operations without changing concrete URLs or behaviour.
import inspect as _inspect  # noqa: E402

_EXTENSIONS_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/customer-parts/{mapping_id}", "/customer-parts/{id}", "mapping_id"),
    ("PATCH", "/api/v1/customer-parts/{mapping_id}", "/customer-parts/{id}", "mapping_id"),
    ("GET", "/api/v1/items/{item_id}/customer-parts", "/items/{id}/customer-parts", "item_id"),
    ("GET", "/api/v1/items/{item_id}/warranty", "/items/{id}/warranty", "item_id"),
    ("PUT", "/api/v1/items/{item_id}/warranty", "/items/{id}/warranty", "item_id"),
    ("GET", "/api/v1/upc-registry/{registry_id}", "/upc-registry/{id}", "registry_id"),
    ("POST", "/api/v1/upc-registry/{registry_id}/retire", "/upc-registry/{id}/retire", "registry_id"),
    ("GET", "/api/v1/warranty/claims/{claim_id}", "/warranty/claims/{id}", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/validate", "/warranty/claims/{id}/validate", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/approve", "/warranty/claims/{id}/approve", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/reject", "/warranty/claims/{id}/reject", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/actions", "/warranty/claims/{id}/actions", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/complete", "/warranty/claims/{id}/complete", "claim_id"),
    ("POST", "/api/v1/warranty/claims/{claim_id}/cancel", "/warranty/claims/{id}/cancel", "claim_id"),
    ("GET", "/api/v1/warranty/registrations/{registration_id}", "/warranty/registrations/{id}", "registration_id"),
    ("POST", "/api/v1/warranty/registrations/{registration_id}/void", "/warranty/registrations/{id}/void", "registration_id"),
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
    for method, declared_path, relative_path, implementation_param in _EXTENSIONS_CANONICAL_ALIASES:
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
