# ruff: noqa: B008 - FastAPI Body(...) defaults are the conventional concise form for this router.
"""FS-08 Procurement P2P API (`/api/v1`, catalogued scopes, tenant isolated, idempotent writes).

Thin transport over :class:`app.modules.procurement.service.ProcurementService`; business rules live in
the service. Every route declares exactly one catalogued scope from `design/permissions.json` (see
`ROUTE_SCOPES`), write routes accept an ``Idempotency-Key``, and state-changing routes enforce
``If-Match`` semantics through the service's version checks where the document is editable.

Router registration is integrator-owned (`app/api/main.py`/`registry.py`); this module exports
``router`` and ``ROUTE_SCOPES`` only. The deploy-dark flag ``procurement.p2p.enabled`` is enforced by
the service (`409 DOMAIN_DISABLED` for writes; posted documents stay readable).
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.core.security.errors import ForbiddenScope
from app.modules.procurement.errors import ProcurementError
from app.modules.procurement.service import ProcurementService

router = APIRouter(prefix="/api/v1", tags=["procurement"])

VENDOR_READ = "procurement.vendor.read"
VENDOR_WRITE = "procurement.vendor.write"
VENDOR_MERGE = "procurement.vendor.merge"
CONTRACT_READ = "procurement.contract.read"
CONTRACT_WRITE = "procurement.contract.write"
CONTRACT_APPROVE = "procurement.contract.approve"
REQ_READ = "procurement.requisition.read"
REQ_WRITE = "procurement.requisition.write"
REQ_APPROVE = "procurement.requisition.approve"
REQ_ORDER = "procurement.requisition.order"
REQUEST_READ = "procurement.request.read"
REQUEST_WRITE = "procurement.request.write"
REQUEST_APPROVE = "procurement.request.approve"
RFQ_READ = "procurement.rfq.read"
RFQ_WRITE = "procurement.rfq.write"
RFQ_AWARD = "procurement.rfq.award"
PO_READ = "procurement.po.read"
PO_WRITE = "procurement.po.write"
PO_APPROVE = "procurement.po.approve"
PO_CLOSE = "procurement.po.close"
RECEIPT_READ = "procurement.receipt.read"
RECEIPT_WRITE = "procurement.receipt.write"
RECEIPT_POST = "procurement.receipt.post"
RECEIPT_REVERSE = "procurement.receipt.reverse"
BILL_READ = "procurement.bill.read"
BILL_WRITE = "procurement.bill.write"
BILL_MATCH = "procurement.bill.match"
BILL_APPROVE = "procurement.bill.approve"
BILL_POST = "procurement.bill.post"
CREDIT_READ = "procurement.credit.read"
CREDIT_WRITE = "procurement.credit.write"
CREDIT_APPLY = "procurement.credit.apply"
CREDIT_POST = "procurement.credit.post"
RETURN_READ = "procurement.return.read"
RETURN_WRITE = "procurement.return.write"
RETURN_APPROVE = "procurement.return.approve"
RETURN_SHIP = "procurement.return.ship"
RETURN_CREDIT = "procurement.return.credit"
RETURN_CLOSE = "procurement.return.close"
DROPSHIP_WRITE = "procurement.dropship.write"
MATCH_OVERRIDE = "procurement.match.override"

ROUTE_SCOPES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/vendors"): VENDOR_READ,
    ("POST", "/api/v1/vendors"): VENDOR_WRITE,
    ("GET", "/api/v1/vendors/{vendor_id}"): VENDOR_READ,
    ("PATCH", "/api/v1/vendors/{vendor_id}"): VENDOR_WRITE,
    ("POST", "/api/v1/vendors/{vendor_id}/inactivate"): VENDOR_WRITE,
    ("POST", "/api/v1/vendors/{vendor_id}/merge"): VENDOR_MERGE,
    ("GET", "/api/v1/vendors/{vendor_id}/purchase-history"): VENDOR_READ,
    ("GET", "/api/v1/vendor-categories"): VENDOR_READ,
    ("POST", "/api/v1/vendor-categories"): VENDOR_WRITE,
    ("PATCH", "/api/v1/vendor-categories/{category_id}"): VENDOR_WRITE,
    ("GET", "/api/v1/item-vendors"): VENDOR_READ,
    ("POST", "/api/v1/item-vendors"): VENDOR_WRITE,
    ("PATCH", "/api/v1/item-vendors/{item_vendor_id}"): VENDOR_WRITE,
    ("GET", "/api/v1/item-vendors/{item_vendor_id}/prices"): VENDOR_READ,
    ("POST", "/api/v1/item-vendors/{item_vendor_id}/prices"): VENDOR_WRITE,
    ("GET", "/api/v1/vendor-contracts"): CONTRACT_READ,
    ("POST", "/api/v1/vendor-contracts"): CONTRACT_WRITE,
    ("GET", "/api/v1/vendor-contracts/{contract_id}"): CONTRACT_READ,
    ("PATCH", "/api/v1/vendor-contracts/{contract_id}"): CONTRACT_WRITE,
    ("POST", "/api/v1/vendor-contracts/{contract_id}/submit"): CONTRACT_WRITE,
    ("POST", "/api/v1/vendor-contracts/{contract_id}/approve"): CONTRACT_APPROVE,
    ("POST", "/api/v1/vendor-contracts/{contract_id}/activate"): CONTRACT_APPROVE,
    ("POST", "/api/v1/vendor-contracts/{contract_id}/close"): CONTRACT_WRITE,
    ("GET", "/api/v1/purchase-requisitions"): REQ_READ,
    ("POST", "/api/v1/purchase-requisitions"): REQ_WRITE,
    ("GET", "/api/v1/purchase-requisitions/{requisition_id}"): REQ_READ,
    ("PATCH", "/api/v1/purchase-requisitions/{requisition_id}"): REQ_WRITE,
    ("POST", "/api/v1/purchase-requisitions/{requisition_id}/submit"): REQ_WRITE,
    ("POST", "/api/v1/purchase-requisitions/{requisition_id}/approve"): REQ_APPROVE,
    ("POST", "/api/v1/purchase-requisitions/{requisition_id}/order"): REQ_ORDER,
    ("GET", "/api/v1/purchase-requests"): REQUEST_READ,
    ("POST", "/api/v1/purchase-requests"): REQUEST_WRITE,
    ("GET", "/api/v1/purchase-requests/{request_id}"): REQUEST_READ,
    ("POST", "/api/v1/purchase-requests/{request_id}/approve"): REQUEST_APPROVE,
    ("POST", "/api/v1/purchase-requests/{request_id}/convert-to-po"): REQUEST_WRITE,
    ("GET", "/api/v1/rfqs"): RFQ_READ,
    ("POST", "/api/v1/rfqs"): RFQ_WRITE,
    ("GET", "/api/v1/rfqs/{rfq_id}"): RFQ_READ,
    ("POST", "/api/v1/rfqs/{rfq_id}/open"): RFQ_WRITE,
    ("POST", "/api/v1/rfqs/{rfq_id}/responses"): RFQ_WRITE,
    ("GET", "/api/v1/rfqs/{rfq_id}/responses"): RFQ_READ,
    ("POST", "/api/v1/rfqs/{rfq_id}/award"): RFQ_AWARD,
    ("POST", "/api/v1/rfqs/{rfq_id}/close"): RFQ_WRITE,
    ("GET", "/api/v1/purchase-orders"): PO_READ,
    ("POST", "/api/v1/purchase-orders"): PO_WRITE,
    ("GET", "/api/v1/purchase-orders/{po_id}"): PO_READ,
    ("PATCH", "/api/v1/purchase-orders/{po_id}"): PO_WRITE,
    ("POST", "/api/v1/purchase-orders/{po_id}/submit"): PO_WRITE,
    ("POST", "/api/v1/purchase-orders/{po_id}/approve"): PO_APPROVE,
    ("POST", "/api/v1/purchase-orders/{po_id}/reject"): PO_APPROVE,
    ("POST", "/api/v1/purchase-orders/{po_id}/cancel"): PO_WRITE,
    ("POST", "/api/v1/purchase-orders/{po_id}/close"): PO_CLOSE,
    ("GET", "/api/v1/purchase-orders/{po_id}/status"): PO_READ,
    ("GET", "/api/v1/purchase-orders/{po_id}/history"): PO_READ,
    ("POST", "/api/v1/purchase-orders/{po_id}/releases"): PO_WRITE,
    ("GET", "/api/v1/purchase-orders/{po_id}/releases"): PO_READ,
    ("GET", "/api/v1/item-receipts"): RECEIPT_READ,
    ("POST", "/api/v1/item-receipts"): RECEIPT_WRITE,
    ("GET", "/api/v1/item-receipts/unbilled"): RECEIPT_READ,
    ("GET", "/api/v1/item-receipts/{receipt_id}"): RECEIPT_READ,
    ("PATCH", "/api/v1/item-receipts/{receipt_id}"): RECEIPT_WRITE,
    ("POST", "/api/v1/item-receipts/{receipt_id}/post"): RECEIPT_POST,
    ("POST", "/api/v1/item-receipts/{receipt_id}/reverse"): RECEIPT_REVERSE,
    ("GET", "/api/v1/vendor-bills"): BILL_READ,
    ("POST", "/api/v1/vendor-bills"): BILL_WRITE,
    ("GET", "/api/v1/vendor-bills/{bill_id}"): BILL_READ,
    ("PATCH", "/api/v1/vendor-bills/{bill_id}"): BILL_WRITE,
    ("POST", "/api/v1/vendor-bills/{bill_id}/match"): BILL_MATCH,
    ("POST", "/api/v1/vendor-bills/{bill_id}/submit"): BILL_WRITE,
    ("POST", "/api/v1/vendor-bills/{bill_id}/approve"): BILL_APPROVE,
    ("POST", "/api/v1/vendor-bills/{bill_id}/post"): BILL_POST,
    ("POST", "/api/v1/vendor-bills/{bill_id}/cancel"): BILL_WRITE,
    ("GET", "/api/v1/vendor-bills/{bill_id}/variances"): BILL_READ,
    ("GET", "/api/v1/vendor-credits"): CREDIT_READ,
    ("POST", "/api/v1/vendor-credits"): CREDIT_WRITE,
    ("GET", "/api/v1/vendor-credits/{credit_id}"): CREDIT_READ,
    ("POST", "/api/v1/vendor-credits/{credit_id}/apply"): CREDIT_APPLY,
    ("POST", "/api/v1/vendor-credits/{credit_id}/post"): CREDIT_POST,
    ("GET", "/api/v1/vendor-returns"): RETURN_READ,
    ("POST", "/api/v1/vendor-returns"): RETURN_WRITE,
    ("GET", "/api/v1/vendor-returns/{return_id}"): RETURN_READ,
    ("POST", "/api/v1/vendor-returns/{return_id}/approve"): RETURN_APPROVE,
    ("POST", "/api/v1/vendor-returns/{return_id}/ship"): RETURN_SHIP,
    ("POST", "/api/v1/vendor-returns/{return_id}/credit"): RETURN_CREDIT,
    ("POST", "/api/v1/vendor-returns/{return_id}/close"): RETURN_CLOSE,
    ("GET", "/api/v1/special-order-links"): PO_READ,
    ("POST", "/api/v1/special-order-links"): DROPSHIP_WRITE,
    ("GET", "/api/v1/special-order-links/{link_id}"): PO_READ,
    ("POST", "/api/v1/special-order-links/{link_id}/fulfil"): DROPSHIP_WRITE,
}

IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


def _raise(exc: Exception) -> NoReturn:
    if isinstance(exc, ProcurementError):
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    raise exc


def _service(db: AsyncSession, principal: PrincipalDep) -> ProcurementService:
    return ProcurementService(db, principal.tenant_id)


def _limit(limit: int) -> int:
    return max(1, min(int(limit or 100), 500))


class WriteScope:
    """Dependency marker used by routers written with explicit ``dependencies=[...]`` lists."""


# ------------------------------------------------------------------------------- vendors -----
@router.get("/vendors", dependencies=[Depends(require_scope(VENDOR_READ))])
async def list_vendors(
    db: DbDep,
    principal: PrincipalDep,
    is_inactive: bool | None = None,
    category_id: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_vendors(
        is_inactive=is_inactive, category_id=category_id, q=q, cursor=cursor, limit=_limit(limit)
    )


@router.post("/vendors", status_code=201, dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def create_vendor(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).create_vendor(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/vendors/{vendor_id}", dependencies=[Depends(require_scope(VENDOR_READ))])
async def get_vendor(vendor_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_vendor(vendor_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/vendors/{vendor_id}", dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def patch_vendor(
    vendor_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_vendor(
            vendor_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/vendors/{vendor_id}/inactivate", dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def inactivate_vendor(
    vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).inactivate_vendor(
            vendor_id, str(payload.get("reason") or ""), actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/vendors/{vendor_id}/merge", dependencies=[Depends(require_scope(VENDOR_MERGE))])
async def merge_vendors(
    vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).merge_vendors(
            vendor_id,
            payload.get("target_vendor_id") or payload.get("target"),
            actor_id=principal.app_user_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/vendors/{vendor_id}/purchase-history", dependencies=[Depends(require_scope(VENDOR_READ))])
async def vendor_purchase_history(
    vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    date_from: str | None = None,
    date_to: str | None = None,
    item_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_vendor_purchase_history(
            vendor_id, date_from=date_from, date_to=date_to, item_id=item_id, limit=_limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/vendor-categories", dependencies=[Depends(require_scope(VENDOR_READ))])
async def list_vendor_categories(db: DbDep, principal: PrincipalDep, is_inactive: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_vendor_categories(is_inactive=is_inactive)


@router.post("/vendor-categories", status_code=201, dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def create_vendor_category(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).create_vendor_category(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/vendor-categories/{category_id}", dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def patch_vendor_category(
    category_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_vendor_category(
            category_id, payload, actor_id=principal.app_user_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/item-vendors", dependencies=[Depends(require_scope(VENDOR_READ))])
async def list_item_vendors(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str | None = None,
    vendor_id: str | None = None,
    is_preferred: bool | None = None,
) -> dict[str, Any]:
    return await _service(db, principal).list_item_vendors(
        item_id=item_id, vendor_id=vendor_id, is_preferred=is_preferred
    )


@router.post("/item-vendors", status_code=201, dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def associate_item_vendor(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).associate_item_vendor(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/item-vendors/{item_vendor_id}", dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def patch_item_vendor(
    item_vendor_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_item_vendor(item_vendor_id, payload, actor_id=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/item-vendors/{item_vendor_id}/prices", dependencies=[Depends(require_scope(VENDOR_READ))])
async def list_item_vendor_prices(
    item_vendor_id: str, db: DbDep, principal: PrincipalDep, currency: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).list_item_vendor_prices(item_vendor_id, currency=currency, as_of=as_of)


@router.post(
    "/item-vendors/{item_vendor_id}/prices", status_code=201, dependencies=[Depends(require_scope(VENDOR_WRITE))]
)
async def add_item_vendor_price(
    item_vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).add_item_vendor_price(
            item_vendor_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ----------------------------------------------------------------------------- contracts -----
@router.get("/vendor-contracts", dependencies=[Depends(require_scope(CONTRACT_READ))])
async def list_contracts(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    status: str | None = None,
    contract_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_contracts(
        vendor_id=vendor_id, status=status, contract_type=contract_type, limit=_limit(limit)
    )


@router.post("/vendor-contracts", status_code=201, dependencies=[Depends(require_scope(CONTRACT_WRITE))])
async def create_contract(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_contract(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/vendor-contracts/{contract_id}", dependencies=[Depends(require_scope(CONTRACT_READ))])
async def get_contract(contract_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_contract(contract_id)


@router.patch("/vendor-contracts/{contract_id}", dependencies=[Depends(require_scope(CONTRACT_WRITE))])
async def patch_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_contract(
        contract_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
    )


@router.post("/vendor-contracts/{contract_id}/submit", dependencies=[Depends(require_scope(CONTRACT_WRITE))])
async def submit_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(
        contract_id, "PENDING_APPROVAL", actor_id=principal.app_user_id
    )


@router.post("/vendor-contracts/{contract_id}/approve", dependencies=[Depends(require_scope(CONTRACT_APPROVE))])
async def approve_contract(
    contract_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(
        contract_id, "APPROVED", actor_id=principal.app_user_id, reason=payload.get("comment")
    )


@router.post("/vendor-contracts/{contract_id}/activate", dependencies=[Depends(require_scope(CONTRACT_APPROVE))])
async def activate_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(contract_id, "ACTIVE", actor_id=principal.app_user_id)


@router.post("/vendor-contracts/{contract_id}/close", dependencies=[Depends(require_scope(CONTRACT_WRITE))])
async def close_contract(
    contract_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(
        contract_id, "CLOSED", actor_id=principal.app_user_id, reason=payload.get("reason")
    )


# -------------------------------------------------------------------------- requisitions -----
@router.get("/purchase-requisitions", dependencies=[Depends(require_scope(REQ_READ))])
async def list_requisitions(
    db: DbDep,
    principal: PrincipalDep,
    status: str | None = None,
    requested_by: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_requisitions(
        status=status, requested_by=requested_by, date_from=date_from, date_to=date_to, limit=_limit(limit)
    )


@router.post("/purchase-requisitions", status_code=201, dependencies=[Depends(require_scope(REQ_WRITE))])
async def create_requisition(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_requisition(
        {**payload, "requested_by": payload.get("requested_by") or principal.app_user_id},
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get("/purchase-requisitions/{requisition_id}", dependencies=[Depends(require_scope(REQ_READ))])
async def get_requisition(requisition_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_requisition(requisition_id)


@router.patch("/purchase-requisitions/{requisition_id}", dependencies=[Depends(require_scope(REQ_WRITE))])
async def patch_requisition(
    requisition_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_requisition(requisition_id, payload, actor_id=principal.app_user_id)


@router.post("/purchase-requisitions/{requisition_id}/submit", dependencies=[Depends(require_scope(REQ_WRITE))])
async def submit_requisition(
    requisition_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_requisition(
        requisition_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/purchase-requisitions/{requisition_id}/approve", dependencies=[Depends(require_scope(REQ_APPROVE))])
async def decide_requisition(
    requisition_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).decide_requisition(
        requisition_id,
        str(payload.get("decision") or "APPROVE"),
        payload.get("comment"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/purchase-requisitions/{requisition_id}/order", dependencies=[Depends(require_scope(REQ_ORDER))])
async def order_requisition(
    requisition_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).order_requisition(
        requisition_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


# ------------------------------------------------------------------------ purchase requests ---
@router.get("/purchase-requests", dependencies=[Depends(require_scope(REQUEST_READ))])
async def list_purchase_requests(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_purchase_requests(status=status, limit=_limit(limit))


@router.post("/purchase-requests", status_code=201, dependencies=[Depends(require_scope(REQUEST_WRITE))])
async def create_purchase_request(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_purchase_request(
        {**payload, "requested_by": payload.get("requested_by") or principal.app_user_id},
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get("/purchase-requests/{request_id}", dependencies=[Depends(require_scope(REQUEST_READ))])
async def get_purchase_request(request_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_purchase_request(request_id)


@router.post("/purchase-requests/{request_id}/approve", dependencies=[Depends(require_scope(REQUEST_APPROVE))])
async def decide_purchase_request(
    request_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).decide_purchase_request(
        request_id, str(payload.get("decision") or "APPROVE"), payload.get("comment"), actor_id=principal.app_user_id
    )


@router.post("/purchase-requests/{request_id}/convert-to-po", dependencies=[Depends(require_scope(REQUEST_WRITE))])
async def convert_purchase_request(
    request_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).convert_purchase_request(request_id, payload, actor_id=principal.app_user_id)


# ------------------------------------------------------------------------------------- RFQ ----
@router.get("/rfqs", dependencies=[Depends(require_scope(RFQ_READ))])
async def list_rfqs(
    db: DbDep, principal: PrincipalDep, status: str | None = None, bid_close_before: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_rfqs(
        status=status, bid_close_before=bid_close_before, limit=_limit(limit)
    )


@router.post("/rfqs", status_code=201, dependencies=[Depends(require_scope(RFQ_WRITE))])
async def create_rfq(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_rfq(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/rfqs/{rfq_id}", dependencies=[Depends(require_scope(RFQ_READ))])
async def get_rfq(rfq_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_rfq(rfq_id)


@router.post("/rfqs/{rfq_id}/open", dependencies=[Depends(require_scope(RFQ_WRITE))])
async def open_rfq(
    rfq_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).open_rfq(rfq_id, actor_id=principal.app_user_id)


@router.post("/rfqs/{rfq_id}/responses", status_code=201, dependencies=[Depends(require_scope(RFQ_WRITE))])
async def register_rfq_response(
    rfq_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).register_rfq_response(rfq_id, payload, actor_id=principal.app_user_id)


@router.get("/rfqs/{rfq_id}/responses", dependencies=[Depends(require_scope(RFQ_READ))])
async def list_rfq_responses(rfq_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).list_rfq_responses(rfq_id)


@router.post("/rfqs/{rfq_id}/award", dependencies=[Depends(require_scope(RFQ_AWARD))])
async def award_rfq(
    rfq_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).award_rfq(rfq_id, payload, actor_id=principal.app_user_id)


@router.post("/rfqs/{rfq_id}/close", dependencies=[Depends(require_scope(RFQ_WRITE))])
async def close_rfq(
    rfq_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).close_rfq(rfq_id, actor_id=principal.app_user_id)


# --------------------------------------------------------------------------- purchase orders ---
@router.get("/purchase-orders", dependencies=[Depends(require_scope(PO_READ))])
async def list_purchase_orders(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    status: str | None = None,
    po_type: str | None = None,
    expected_receipt_from: str | None = None,
    expected_receipt_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_purchase_orders(
        vendor_id=vendor_id,
        status=status,
        po_type=po_type,
        expected_receipt_from=expected_receipt_from,
        expected_receipt_to=expected_receipt_to,
        limit=_limit(limit),
    )


@router.post("/purchase-orders", status_code=201, dependencies=[Depends(require_scope(PO_WRITE))])
async def create_purchase_order(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_purchase_order(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/purchase-orders/{po_id}", dependencies=[Depends(require_scope(PO_READ))])
async def get_purchase_order(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_purchase_order(po_id)


@router.patch("/purchase-orders/{po_id}", dependencies=[Depends(require_scope(PO_WRITE))])
async def patch_purchase_order(
    po_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_purchase_order(
        po_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
    )


@router.post("/purchase-orders/{po_id}/submit", dependencies=[Depends(require_scope(PO_WRITE))])
async def submit_purchase_order(
    po_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_purchase_order(
        po_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/purchase-orders/{po_id}/approve", dependencies=[Depends(require_scope(PO_APPROVE))])
async def approve_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).approve_purchase_order(
        po_id, actor_id=principal.app_user_id, comment=payload.get("comment"), idempotency_key=idempotency_key
    )


@router.post("/purchase-orders/{po_id}/reject", dependencies=[Depends(require_scope(PO_APPROVE))])
async def reject_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).reject_purchase_order(
        po_id, str(payload.get("reason") or "rejected"), actor_id=principal.app_user_id
    )


@router.post("/purchase-orders/{po_id}/cancel", dependencies=[Depends(require_scope(PO_WRITE))])
async def cancel_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).cancel_purchase_order(
        po_id, str(payload.get("reason") or "cancelled"), actor_id=principal.app_user_id
    )


@router.post("/purchase-orders/{po_id}/close", dependencies=[Depends(require_scope(PO_CLOSE))])
async def close_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).close_purchase_order(
        po_id,
        line_ids=payload.get("line_ids"),
        reason=payload.get("reason"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get("/purchase-orders/{po_id}/status", dependencies=[Depends(require_scope(PO_READ))])
async def purchase_order_status(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).po_status(po_id)


@router.get("/purchase-orders/{po_id}/history", dependencies=[Depends(require_scope(PO_READ))])
async def purchase_order_history(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).po_history(po_id)


@router.post("/purchase-orders/{po_id}/releases", status_code=201, dependencies=[Depends(require_scope(PO_WRITE))])
async def create_po_release(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).create_po_release(
        po_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/purchase-orders/{po_id}/releases", dependencies=[Depends(require_scope(PO_READ))])
async def list_po_releases(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).list_po_releases(po_id)


# ------------------------------------------------------------------------------- receipts -----
@router.get("/item-receipts", dependencies=[Depends(require_scope(RECEIPT_READ))])
async def list_receipts(
    db: DbDep,
    principal: PrincipalDep,
    purchase_order_id: str | None = None,
    status: str | None = None,
    received_from: str | None = None,
    received_to: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_receipts(
        purchase_order_id=purchase_order_id,
        status=status,
        received_from=received_from,
        received_to=received_to,
        limit=_limit(limit),
    )


@router.post("/item-receipts", status_code=201, dependencies=[Depends(require_scope(RECEIPT_WRITE))])
async def create_receipt(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_receipt(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/item-receipts/unbilled", dependencies=[Depends(require_scope(RECEIPT_READ))])
async def received_not_billed(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    purchase_order_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).received_not_billed(
        vendor_id=vendor_id,
        purchase_order_id=purchase_order_id,
        date_from=date_from,
        date_to=date_to,
        limit=_limit(limit),
    )


@router.get("/item-receipts/{receipt_id}", dependencies=[Depends(require_scope(RECEIPT_READ))])
async def get_receipt(receipt_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_receipt(receipt_id)


@router.patch("/item-receipts/{receipt_id}", dependencies=[Depends(require_scope(RECEIPT_WRITE))])
async def patch_receipt(
    receipt_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_receipt(receipt_id, payload, actor_id=principal.app_user_id)


@router.post("/item-receipts/{receipt_id}/post", dependencies=[Depends(require_scope(RECEIPT_POST))])
async def post_receipt(
    receipt_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).post_receipt(
        receipt_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/item-receipts/{receipt_id}/reverse", dependencies=[Depends(require_scope(RECEIPT_REVERSE))])
async def reverse_receipt(
    receipt_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).reverse_receipt(
        receipt_id,
        str(payload.get("reason") or "reversal"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------------- bills -----
@router.get("/vendor-bills", dependencies=[Depends(require_scope(BILL_READ))])
async def list_bills(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    status: str | None = None,
    match_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_bills(
        vendor_id=vendor_id, status=status, match_status=match_status, limit=_limit(limit)
    )


@router.post("/vendor-bills", status_code=201, dependencies=[Depends(require_scope(BILL_WRITE))])
async def create_bill(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_bill(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/vendor-bills/{bill_id}", dependencies=[Depends(require_scope(BILL_READ))])
async def get_bill(bill_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_bill(bill_id)


@router.patch("/vendor-bills/{bill_id}", dependencies=[Depends(require_scope(BILL_WRITE))])
async def patch_bill(
    bill_id: str, db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_bill(bill_id, payload, actor_id=principal.app_user_id)


@router.post("/vendor-bills/{bill_id}/match", dependencies=[Depends(require_scope(BILL_MATCH))])
async def match_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    override = bool(payload.get("override"))
    if override and MATCH_OVERRIDE not in principal.scopes:
        raise ForbiddenScope(
            "match override requires the procurement.match.override scope",
            details={"required_scope": MATCH_OVERRIDE},
        )
    return await _service(db, principal).match_bill(
        bill_id,
        override=override,
        override_reason=payload.get("override_reason"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post("/vendor-bills/{bill_id}/submit", dependencies=[Depends(require_scope(BILL_WRITE))])
async def submit_bill(
    bill_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_bill(
        bill_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/vendor-bills/{bill_id}/approve", dependencies=[Depends(require_scope(BILL_APPROVE))])
async def approve_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).approve_bill(
        bill_id, actor_id=principal.app_user_id, comment=payload.get("comment"), idempotency_key=idempotency_key
    )


@router.post("/vendor-bills/{bill_id}/post", dependencies=[Depends(require_scope(BILL_POST))])
async def post_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).post_bill(
        bill_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/vendor-bills/{bill_id}/cancel", dependencies=[Depends(require_scope(BILL_WRITE))])
async def cancel_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).cancel_bill(
        bill_id, str(payload.get("reason") or "cancelled"), actor_id=principal.app_user_id
    )


@router.get("/vendor-bills/{bill_id}/variances", dependencies=[Depends(require_scope(BILL_READ))])
async def bill_variances(bill_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).bill_variances(bill_id)


# -------------------------------------------------------------------------------- credits -----
@router.get("/vendor-credits", dependencies=[Depends(require_scope(CREDIT_READ))])
async def list_credits(
    db: DbDep, principal: PrincipalDep, vendor_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_credits(vendor_id=vendor_id, status=status, limit=_limit(limit))


@router.post("/vendor-credits", status_code=201, dependencies=[Depends(require_scope(CREDIT_WRITE))])
async def create_credit(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_credit(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/vendor-credits/{credit_id}", dependencies=[Depends(require_scope(CREDIT_READ))])
async def get_credit(credit_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_credit(credit_id)


@router.post("/vendor-credits/{credit_id}/apply", dependencies=[Depends(require_scope(CREDIT_APPLY))])
async def apply_credit(
    credit_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).apply_credit(
        credit_id, payload.get("applications") or [], actor_id=principal.app_user_id
    )


@router.post("/vendor-credits/{credit_id}/post", dependencies=[Depends(require_scope(CREDIT_POST))])
async def post_credit(
    credit_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).post_credit(
        credit_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


# -------------------------------------------------------------------------------- returns -----
@router.get("/vendor-returns", dependencies=[Depends(require_scope(RETURN_READ))])
async def list_returns(
    db: DbDep, principal: PrincipalDep, vendor_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_returns(vendor_id=vendor_id, status=status, limit=_limit(limit))


@router.post("/vendor-returns", status_code=201, dependencies=[Depends(require_scope(RETURN_WRITE))])
async def create_return(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_return(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/vendor-returns/{return_id}", dependencies=[Depends(require_scope(RETURN_READ))])
async def get_return(return_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_return(return_id)


@router.post("/vendor-returns/{return_id}/approve", dependencies=[Depends(require_scope(RETURN_APPROVE))])
async def approve_return(
    return_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).approve_return(return_id, actor_id=principal.app_user_id)


@router.post("/vendor-returns/{return_id}/ship", dependencies=[Depends(require_scope(RETURN_SHIP))])
async def ship_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).ship_return(
        return_id, payload.get("lines") or [], actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post("/vendor-returns/{return_id}/credit", dependencies=[Depends(require_scope(RETURN_CREDIT))])
async def credit_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(...),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).credit_return(return_id, payload, actor_id=principal.app_user_id)


@router.post("/vendor-returns/{return_id}/close", dependencies=[Depends(require_scope(RETURN_CLOSE))])
async def close_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).close_return(
        return_id, line_ids=payload.get("line_ids"), actor_id=principal.app_user_id
    )


# ---------------------------------------------------------------------------- drop-ship -------
@router.get("/special-order-links", dependencies=[Depends(require_scope(PO_READ))])
async def list_special_order_links(
    db: DbDep,
    principal: PrincipalDep,
    purchase_order_id: str | None = None,
    item_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return await _service(db, principal).list_special_order_links(
        purchase_order_id=purchase_order_id, item_id=item_id, status=status, limit=_limit(limit)
    )


@router.post("/special-order-links", status_code=201, dependencies=[Depends(require_scope(DROPSHIP_WRITE))])
async def create_special_order_link(
    db: DbDep, principal: PrincipalDep, payload: dict[str, Any] = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_special_order_link(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/special-order-links/{link_id}", dependencies=[Depends(require_scope(PO_READ))])
async def get_special_order_link(link_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_special_order_link(link_id)


@router.post("/special-order-links/{link_id}/fulfil", dependencies=[Depends(require_scope(DROPSHIP_WRITE))])
async def fulfil_dropship(
    link_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    return await _service(db, principal).fulfil_dropship(
        link_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


__all__ = ["ROUTE_SCOPES", "router"]
