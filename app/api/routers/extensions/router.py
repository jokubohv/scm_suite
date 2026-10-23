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


__all__ = ["ROUTE_SCOPES", "router"]
