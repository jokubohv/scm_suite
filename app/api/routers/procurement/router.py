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

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.routing import SCMAPIRoute
from app.api.schemas import procurement as procurement_dto
from app.api.schemas.common import PROBLEM_RESPONSES, ActionResult
from app.api.schemas.procurement import (
    PurchaseOrderCreate,
    PurchaseOrderOut,
    PurchaseOrderPage,
    PurchaseOrderUpdate,
    ReceiptActionOut,
    ReceiptCreate,
    ReceiptOut,
    ReceiptPage,
    ReceiptUpdate,
    VendorBillCreate,
    VendorBillOut,
    VendorBillPage,
    VendorBillUpdate,
    VendorCreate,
    VendorOut,
    VendorPage,
    VendorUpdate,
)
from app.core.security.errors import ForbiddenScope
from app.modules.procurement.errors import ProcurementError
from app.modules.procurement.service import ProcurementService

router = APIRouter(prefix="/api/v1", responses=PROBLEM_RESPONSES, tags=["procurement"], route_class=SCMAPIRoute)

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
@router.get("/vendors", response_model=VendorPage, dependencies=[Depends(require_scope(VENDOR_READ))])
async def list_vendors(
    db: DbDep,
    principal: PrincipalDep,
    is_inactive: bool | None = None,
    category_id: str | None = None,
    q: str | None = None,
    tax_id: Annotated[str | None, Query(max_length=200)] = None,
    vendor_number: Annotated[str | None, Query(max_length=200)] = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_vendors(
            is_inactive=is_inactive, category_id=category_id, q=q, tax_id=tax_id,
            vendor_number=vendor_number, cursor=cursor, limit=_limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post("/vendors", status_code=201, response_model=VendorOut, dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def create_vendor(
    db: DbDep, principal: PrincipalDep, body: VendorCreate = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    try:
        return await _service(db, principal).create_vendor(
            body.model_dump(exclude_unset=True), actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get("/vendors/{vendor_id}", response_model=VendorOut, dependencies=[Depends(require_scope(VENDOR_READ))])
async def get_vendor(vendor_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    try:
        return await _service(db, principal).get_vendor(vendor_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch("/vendors/{vendor_id}", response_model=VendorOut, dependencies=[Depends(require_scope(VENDOR_WRITE))])
async def patch_vendor(
    vendor_id: str, db: DbDep, principal: PrincipalDep, body: VendorUpdate = Body(...)
) -> dict[str, Any]:
    try:
        return await _service(db, principal).update_vendor(
            vendor_id,
            body.model_dump(exclude_unset=True),
            actor_id=principal.app_user_id,
            expected_version=body.version,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/vendors/{vendor_id}/inactivate",
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.VendorOut,
)
async def inactivate_vendor(
    vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementInactivateVendorIn = procurement_dto.ProcurementInactivateVendorIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).inactivate_vendor(
            vendor_id, str(payload.get("reason") or ""), actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/vendors/{vendor_id}/merge",
    dependencies=[Depends(require_scope(VENDOR_MERGE))],
    response_model=procurement_dto.VendorOut,
)
async def merge_vendors(
    vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementMergeVendorsIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).merge_vendors(
            vendor_id,
            payload.get("target_vendor_id") or payload.get("target"),
            actor_id=principal.app_user_id,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get(
    "/vendors/{vendor_id}/purchase-history",
    dependencies=[Depends(require_scope(VENDOR_READ))],
    response_model=procurement_dto.VendorPurchaseHistoryPage,
)
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


@router.get(
    "/vendor-categories",
    dependencies=[Depends(require_scope(VENDOR_READ))],
    response_model=procurement_dto.VendorCategoryPage,
)
async def list_vendor_categories(db: DbDep, principal: PrincipalDep, is_inactive: bool | None = None) -> dict[str, Any]:
    return await _service(db, principal).list_vendor_categories(is_inactive=is_inactive)


@router.post(
    "/vendor-categories",
    status_code=201,
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.VendorCategoryOut,
)
async def create_vendor_category(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateVendorCategoryIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).create_vendor_category(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch(
    "/vendor-categories/{category_id}",
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.VendorCategoryOut,
)
async def patch_vendor_category(
    category_id: str, db: DbDep, principal: PrincipalDep, payload: procurement_dto.ProcurementPatchVendorCategoryIn
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).update_vendor_category(
            category_id, payload, actor_id=principal.app_user_id
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get(
    "/item-vendors", dependencies=[Depends(require_scope(VENDOR_READ))], response_model=procurement_dto.ItemVendorPage
)
async def list_item_vendors(
    db: DbDep,
    principal: PrincipalDep,
    item_id: str | None = None,
    vendor_id: str | None = None,
    is_preferred: bool | None = None,
    vendor_item_code: Annotated[str | None, Query(max_length=200)] = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        return await _service(db, principal).list_item_vendors(
            item_id=item_id, vendor_id=vendor_id, is_preferred=is_preferred,
            vendor_item_code=vendor_item_code, cursor=cursor, limit=_limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.post(
    "/item-vendors",
    status_code=201,
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.ItemVendorOut,
)
async def associate_item_vendor(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementAssociateItemVendorIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).associate_item_vendor(
            payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.patch(
    "/item-vendors/{item_vendor_id}",
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.ItemVendorOut,
)
async def patch_item_vendor(
    item_vendor_id: str, db: DbDep, principal: PrincipalDep, payload: procurement_dto.ProcurementPatchItemVendorIn
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).update_item_vendor(item_vendor_id, payload, actor_id=principal.app_user_id)
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@router.get(
    "/item-vendors/{item_vendor_id}/prices",
    dependencies=[Depends(require_scope(VENDOR_READ))],
    response_model=procurement_dto.ItemVendorPricePage,
)
async def list_item_vendor_prices(
    item_vendor_id: str, db: DbDep, principal: PrincipalDep, currency: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    return await _service(db, principal).list_item_vendor_prices(item_vendor_id, currency=currency, as_of=as_of)


@router.post(
    "/item-vendors/{item_vendor_id}/prices",
    status_code=201,
    dependencies=[Depends(require_scope(VENDOR_WRITE))],
    response_model=procurement_dto.ItemVendorPriceOut,
)
async def add_item_vendor_price(
    item_vendor_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementAddItemVendorPriceIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    try:
        return await _service(db, principal).add_item_vendor_price(
            item_vendor_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ----------------------------------------------------------------------------- contracts -----
@router.get(
    "/vendor-contracts",
    dependencies=[Depends(require_scope(CONTRACT_READ))],
    response_model=procurement_dto.VendorContractPage,
)
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


@router.post(
    "/vendor-contracts",
    status_code=201,
    dependencies=[Depends(require_scope(CONTRACT_WRITE))],
    response_model=procurement_dto.VendorContractOut,
)
async def create_contract(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateContractIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_contract(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/vendor-contracts/{contract_id}",
    dependencies=[Depends(require_scope(CONTRACT_READ))],
    response_model=procurement_dto.VendorContractOut,
)
async def get_contract(contract_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_contract(contract_id)


@router.patch(
    "/vendor-contracts/{contract_id}",
    dependencies=[Depends(require_scope(CONTRACT_WRITE))],
    response_model=procurement_dto.VendorContractOut,
)
async def patch_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, payload: procurement_dto.ProcurementPatchContractIn
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).update_contract(
        contract_id, payload, actor_id=principal.app_user_id, expected_version=payload.get("version")
    )


@router.post(
    "/vendor-contracts/{contract_id}/submit",
    dependencies=[Depends(require_scope(CONTRACT_WRITE))],
    response_model=procurement_dto.VendorContractOut,
)
async def submit_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(
        contract_id, "PENDING_APPROVAL", actor_id=principal.app_user_id
    )


@router.post(
    "/vendor-contracts/{contract_id}/approve",
    dependencies=[Depends(require_scope(CONTRACT_APPROVE))],
    response_model=procurement_dto.VendorContractOut,
)
async def approve_contract(
    contract_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementApproveContractIn = procurement_dto.ProcurementApproveContractIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal)._contract_transition(
        contract_id, "APPROVED", actor_id=principal.app_user_id, reason=payload.get("comment")
    )


@router.post(
    "/vendor-contracts/{contract_id}/activate",
    dependencies=[Depends(require_scope(CONTRACT_APPROVE))],
    response_model=procurement_dto.VendorContractOut,
)
async def activate_contract(
    contract_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal)._contract_transition(contract_id, "ACTIVE", actor_id=principal.app_user_id)


@router.post(
    "/vendor-contracts/{contract_id}/close",
    dependencies=[Depends(require_scope(CONTRACT_WRITE))],
    response_model=procurement_dto.VendorContractOut,
)
async def close_contract(
    contract_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCloseContractIn = procurement_dto.ProcurementCloseContractIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal)._contract_transition(
        contract_id, "CLOSED", actor_id=principal.app_user_id, reason=payload.get("reason")
    )


# -------------------------------------------------------------------------- requisitions -----
@router.get(
    "/purchase-requisitions",
    dependencies=[Depends(require_scope(REQ_READ))],
    response_model=procurement_dto.PurchaseRequisitionPage,
)
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


@router.post(
    "/purchase-requisitions",
    status_code=201,
    dependencies=[Depends(require_scope(REQ_WRITE))],
    response_model=procurement_dto.PurchaseRequisitionOut,
)
async def create_requisition(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateRequisitionIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_requisition(
        {**payload, "requested_by": payload.get("requested_by") or principal.app_user_id},
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/purchase-requisitions/{requisition_id}",
    dependencies=[Depends(require_scope(REQ_READ))],
    response_model=procurement_dto.PurchaseRequisitionOut,
)
async def get_requisition(requisition_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_requisition(requisition_id)


@router.patch(
    "/purchase-requisitions/{requisition_id}",
    dependencies=[Depends(require_scope(REQ_WRITE))],
    response_model=procurement_dto.PurchaseRequisitionOut,
)
async def patch_requisition(
    requisition_id: str, db: DbDep, principal: PrincipalDep, payload: procurement_dto.ProcurementPatchRequisitionIn
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).update_requisition(requisition_id, payload, actor_id=principal.app_user_id)


@router.post(
    "/purchase-requisitions/{requisition_id}/submit",
    dependencies=[Depends(require_scope(REQ_WRITE))],
    response_model=procurement_dto.PurchaseRequisitionOut,
)
async def submit_requisition(
    requisition_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_requisition(
        requisition_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/approve",
    dependencies=[Depends(require_scope(REQ_APPROVE))],
    response_model=procurement_dto.PurchaseRequisitionOut,
)
async def decide_requisition(
    requisition_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementDecideRequisitionIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).decide_requisition(
        requisition_id,
        str(payload.get("decision") or "APPROVE"),
        payload.get("comment"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/order",
    dependencies=[Depends(require_scope(REQ_ORDER))],
    response_model=procurement_dto.PurchaseOrderOut,
)
async def order_requisition(
    requisition_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementOrderRequisitionIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).order_requisition(
        requisition_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


# ------------------------------------------------------------------------ purchase requests ---
@router.get(
    "/purchase-requests",
    dependencies=[Depends(require_scope(REQUEST_READ))],
    response_model=procurement_dto.PurchaseRequestPage,
)
async def list_purchase_requests(
    db: DbDep, principal: PrincipalDep, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_purchase_requests(status=status, limit=_limit(limit))


@router.post(
    "/purchase-requests",
    status_code=201,
    dependencies=[Depends(require_scope(REQUEST_WRITE))],
    response_model=procurement_dto.PurchaseRequestOut,
)
async def create_purchase_request(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreatePurchaseRequestIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_purchase_request(
        {**payload, "requested_by": payload.get("requested_by") or principal.app_user_id},
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/purchase-requests/{request_id}",
    dependencies=[Depends(require_scope(REQUEST_READ))],
    response_model=procurement_dto.PurchaseRequestOut,
)
async def get_purchase_request(request_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_purchase_request(request_id)


@router.post(
    "/purchase-requests/{request_id}/approve",
    dependencies=[Depends(require_scope(REQUEST_APPROVE))],
    response_model=procurement_dto.PurchaseRequestOut,
)
async def decide_purchase_request(
    request_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementDecidePurchaseRequestIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).decide_purchase_request(
        request_id, str(payload.get("decision") or "APPROVE"), payload.get("comment"), actor_id=principal.app_user_id
    )


@router.post(
    "/purchase-requests/{request_id}/convert-to-po",
    dependencies=[Depends(require_scope(REQUEST_WRITE))],
    response_model=procurement_dto.PurchaseOrderOut,
)
async def convert_purchase_request(
    request_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementConvertPurchaseRequestIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).convert_purchase_request(request_id, payload, actor_id=principal.app_user_id)


# ------------------------------------------------------------------------------------- RFQ ----
@router.get("/rfqs", dependencies=[Depends(require_scope(RFQ_READ))], response_model=procurement_dto.RfqPage)
async def list_rfqs(
    db: DbDep, principal: PrincipalDep, status: str | None = None, bid_close_before: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_rfqs(
        status=status, bid_close_before=bid_close_before, limit=_limit(limit)
    )


@router.post(
    "/rfqs", status_code=201, dependencies=[Depends(require_scope(RFQ_WRITE))], response_model=procurement_dto.RfqOut
)
async def create_rfq(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateRfqIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_rfq(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/rfqs/{rfq_id}", dependencies=[Depends(require_scope(RFQ_READ))], response_model=procurement_dto.RfqOut)
async def get_rfq(rfq_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_rfq(rfq_id)


@router.post(
    "/rfqs/{rfq_id}/open", dependencies=[Depends(require_scope(RFQ_WRITE))], response_model=procurement_dto.RfqOut
)
async def open_rfq(
    rfq_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).open_rfq(rfq_id, actor_id=principal.app_user_id)


@router.post(
    "/rfqs/{rfq_id}/responses",
    status_code=201,
    dependencies=[Depends(require_scope(RFQ_WRITE))],
    response_model=procurement_dto.RfqResponseOut,
)
async def register_rfq_response(
    rfq_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementRegisterRfqResponseIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).register_rfq_response(rfq_id, payload, actor_id=principal.app_user_id)


@router.get(
    "/rfqs/{rfq_id}/responses",
    dependencies=[Depends(require_scope(RFQ_READ))],
    response_model=procurement_dto.RfqResponsePage,
)
async def list_rfq_responses(rfq_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).list_rfq_responses(rfq_id)


@router.post(
    "/rfqs/{rfq_id}/award", dependencies=[Depends(require_scope(RFQ_AWARD))], response_model=procurement_dto.RfqOut
)
async def award_rfq(
    rfq_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementAwardRfqIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).award_rfq(rfq_id, payload, actor_id=principal.app_user_id)


@router.post(
    "/rfqs/{rfq_id}/close", dependencies=[Depends(require_scope(RFQ_WRITE))], response_model=procurement_dto.RfqOut
)
async def close_rfq(
    rfq_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).close_rfq(rfq_id, actor_id=principal.app_user_id)


# --------------------------------------------------------------------------- purchase orders ---
@router.get("/purchase-orders", response_model=PurchaseOrderPage, dependencies=[Depends(require_scope(PO_READ))])
async def list_purchase_orders(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    status: str | None = None,
    po_type: str | None = None,
    expected_receipt_from: str | None = None,
    expected_receipt_to: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_purchase_orders(
        vendor_id=vendor_id,
        status=status,
        po_type=po_type,
        expected_receipt_from=expected_receipt_from,
        expected_receipt_to=expected_receipt_to,
        q=q,
        cursor=cursor,
        limit=_limit(limit),
    )


@router.post(
    "/purchase-orders",
    status_code=201,
    response_model=PurchaseOrderOut,
    dependencies=[Depends(require_scope(PO_WRITE))],
)
async def create_purchase_order(
    db: DbDep, principal: PrincipalDep, body: PurchaseOrderCreate = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_purchase_order(
        body.model_dump(exclude_unset=True), actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/purchase-orders/{po_id}", response_model=PurchaseOrderOut, dependencies=[Depends(require_scope(PO_READ))])
async def get_purchase_order(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_purchase_order(po_id)


@router.patch(
    "/purchase-orders/{po_id}", response_model=PurchaseOrderOut, dependencies=[Depends(require_scope(PO_WRITE))]
)
async def patch_purchase_order(
    po_id: str, db: DbDep, principal: PrincipalDep, body: PurchaseOrderUpdate = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_purchase_order(
        po_id, body.model_dump(exclude_unset=True), actor_id=principal.app_user_id, expected_version=body.version
    )


@router.post(
    "/purchase-orders/{po_id}/submit", response_model=ActionResult, dependencies=[Depends(require_scope(PO_WRITE))]
)
async def submit_purchase_order(
    po_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_purchase_order(
        po_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/purchase-orders/{po_id}/approve", response_model=ActionResult, dependencies=[Depends(require_scope(PO_APPROVE))]
)
async def approve_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementApprovePurchaseOrderIn = procurement_dto.ProcurementApprovePurchaseOrderIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).approve_purchase_order(
        po_id, actor_id=principal.app_user_id, comment=payload.get("comment"), idempotency_key=idempotency_key
    )


@router.post(
    "/purchase-orders/{po_id}/reject", response_model=ActionResult, dependencies=[Depends(require_scope(PO_APPROVE))]
)
async def reject_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementRejectPurchaseOrderIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).reject_purchase_order(
        po_id, str(payload.get("reason") or "rejected"), actor_id=principal.app_user_id
    )


@router.post(
    "/purchase-orders/{po_id}/cancel", response_model=ActionResult, dependencies=[Depends(require_scope(PO_WRITE))]
)
async def cancel_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCancelPurchaseOrderIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).cancel_purchase_order(
        po_id, str(payload.get("reason") or "cancelled"), actor_id=principal.app_user_id
    )


@router.post(
    "/purchase-orders/{po_id}/close", response_model=ActionResult, dependencies=[Depends(require_scope(PO_CLOSE))]
)
async def close_purchase_order(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementClosePurchaseOrderIn = procurement_dto.ProcurementClosePurchaseOrderIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).close_purchase_order(
        po_id,
        line_ids=payload.get("line_ids"),
        reason=payload.get("reason"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/purchase-orders/{po_id}/status",
    dependencies=[Depends(require_scope(PO_READ))],
    response_model=procurement_dto.PurchaseOrderStatusOut,
)
async def purchase_order_status(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).po_status(po_id)


@router.get(
    "/purchase-orders/{po_id}/history",
    dependencies=[Depends(require_scope(PO_READ))],
    response_model=procurement_dto.PurchaseOrderHistoryPage,
)
async def purchase_order_history(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).po_history(po_id)


@router.post(
    "/purchase-orders/{po_id}/releases",
    status_code=201,
    dependencies=[Depends(require_scope(PO_WRITE))],
    response_model=procurement_dto.PurchaseOrderReleaseOut,
)
async def create_po_release(
    po_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreatePoReleaseIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_po_release(
        po_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/purchase-orders/{po_id}/releases",
    dependencies=[Depends(require_scope(PO_READ))],
    response_model=procurement_dto.PurchaseOrderReleasePage,
)
async def list_po_releases(po_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).list_po_releases(po_id)


# ------------------------------------------------------------------------------- receipts -----
@router.get("/item-receipts", response_model=ReceiptPage, dependencies=[Depends(require_scope(RECEIPT_READ))])
async def list_receipts(
    db: DbDep,
    principal: PrincipalDep,
    purchase_order_id: str | None = None,
    status: str | None = None,
    received_from: str | None = None,
    received_to: str | None = None,
    vendor_id: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_receipts(
        purchase_order_id=purchase_order_id,
        status=status,
        received_from=received_from,
        received_to=received_to,
        vendor_id=vendor_id,
        q=q,
        cursor=cursor,
        limit=_limit(limit),
    )


@router.post(
    "/item-receipts", status_code=201, response_model=ReceiptOut, dependencies=[Depends(require_scope(RECEIPT_WRITE))]
)
async def create_receipt(
    db: DbDep, principal: PrincipalDep, body: ReceiptCreate = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_receipt(
        body.model_dump(exclude_unset=True), actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/item-receipts/unbilled",
    dependencies=[Depends(require_scope(RECEIPT_READ))],
    response_model=procurement_dto.ReceivedNotBilledPage,
)
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


@router.get(
    "/item-receipts/{receipt_id}", response_model=ReceiptOut, dependencies=[Depends(require_scope(RECEIPT_READ))]
)
async def get_receipt(receipt_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_receipt(receipt_id)


@router.patch(
    "/item-receipts/{receipt_id}", response_model=ReceiptOut, dependencies=[Depends(require_scope(RECEIPT_WRITE))]
)
async def patch_receipt(
    receipt_id: str, db: DbDep, principal: PrincipalDep, body: ReceiptUpdate = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_receipt(
        receipt_id, body.model_dump(exclude_unset=True), actor_id=principal.app_user_id
    )


@router.post(
    "/item-receipts/{receipt_id}/post",
    response_model=ReceiptActionOut,
    dependencies=[Depends(require_scope(RECEIPT_POST))],
)
async def post_receipt(
    receipt_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementPostReceiptIn = procurement_dto.ProcurementPostReceiptIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).post_receipt(
        receipt_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/item-receipts/{receipt_id}/reverse",
    response_model=ReceiptActionOut,
    dependencies=[Depends(require_scope(RECEIPT_REVERSE))],
)
async def reverse_receipt(
    receipt_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementReverseReceiptIn = procurement_dto.ProcurementReverseReceiptIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).reverse_receipt(
        receipt_id,
        str(payload.get("reason") or "reversal"),
        actor_id=principal.app_user_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------------- bills -----
@router.get("/vendor-bills", response_model=VendorBillPage, dependencies=[Depends(require_scope(BILL_READ))])
async def list_bills(
    db: DbDep,
    principal: PrincipalDep,
    vendor_id: str | None = None,
    status: str | None = None,
    match_status: str | None = None,
    vendor_bill_number: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _service(db, principal).list_bills(
        vendor_id=vendor_id, status=status, match_status=match_status,
        vendor_bill_number=vendor_bill_number, q=q, cursor=cursor, limit=_limit(limit)
    )


@router.post(
    "/vendor-bills", status_code=201, response_model=VendorBillOut, dependencies=[Depends(require_scope(BILL_WRITE))]
)
async def create_bill(
    db: DbDep, principal: PrincipalDep, body: VendorBillCreate = Body(...), idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).create_bill(
        body.model_dump(exclude_unset=True), actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get("/vendor-bills/{bill_id}", response_model=VendorBillOut, dependencies=[Depends(require_scope(BILL_READ))])
async def get_bill(bill_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_bill(bill_id)


@router.patch(
    "/vendor-bills/{bill_id}", response_model=VendorBillOut, dependencies=[Depends(require_scope(BILL_WRITE))]
)
async def patch_bill(
    bill_id: str, db: DbDep, principal: PrincipalDep, body: VendorBillUpdate = Body(...)
) -> dict[str, Any]:
    return await _service(db, principal).update_bill(
        bill_id, body.model_dump(exclude_unset=True), actor_id=principal.app_user_id
    )


@router.post(
    "/vendor-bills/{bill_id}/match",
    response_model=procurement_dto.BillMatchOut,
    dependencies=[Depends(require_scope(BILL_MATCH))],
)
async def match_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementMatchBillIn = procurement_dto.ProcurementMatchBillIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
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


@router.post(
    "/vendor-bills/{bill_id}/submit", response_model=ActionResult, dependencies=[Depends(require_scope(BILL_WRITE))]
)
async def submit_bill(
    bill_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).submit_bill(
        bill_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/vendor-bills/{bill_id}/approve", response_model=ActionResult, dependencies=[Depends(require_scope(BILL_APPROVE))]
)
async def approve_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementApproveBillIn = procurement_dto.ProcurementApproveBillIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).approve_bill(
        bill_id, actor_id=principal.app_user_id, comment=payload.get("comment"), idempotency_key=idempotency_key
    )


@router.post(
    "/vendor-bills/{bill_id}/post", response_model=ActionResult, dependencies=[Depends(require_scope(BILL_POST))]
)
async def post_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementPostBillIn = procurement_dto.ProcurementPostBillIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).post_bill(
        bill_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/vendor-bills/{bill_id}/cancel", response_model=ActionResult, dependencies=[Depends(require_scope(BILL_WRITE))]
)
async def cancel_bill(
    bill_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCancelBillIn = procurement_dto.ProcurementCancelBillIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).cancel_bill(
        bill_id, str(payload.get("reason") or "cancelled"), actor_id=principal.app_user_id
    )


@router.get(
    "/vendor-bills/{bill_id}/variances",
    dependencies=[Depends(require_scope(BILL_READ))],
    response_model=procurement_dto.VendorBillVariancePage,
)
async def bill_variances(bill_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).bill_variances(bill_id)


# -------------------------------------------------------------------------------- credits -----
@router.get(
    "/vendor-credits",
    dependencies=[Depends(require_scope(CREDIT_READ))],
    response_model=procurement_dto.VendorCreditPage,
)
async def list_credits(
    db: DbDep, principal: PrincipalDep, vendor_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_credits(vendor_id=vendor_id, status=status, limit=_limit(limit))


@router.post(
    "/vendor-credits",
    status_code=201,
    dependencies=[Depends(require_scope(CREDIT_WRITE))],
    response_model=procurement_dto.VendorCreditOut,
)
async def create_credit(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateCreditIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_credit(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/vendor-credits/{credit_id}",
    dependencies=[Depends(require_scope(CREDIT_READ))],
    response_model=procurement_dto.VendorCreditOut,
)
async def get_credit(credit_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_credit(credit_id)


@router.post(
    "/vendor-credits/{credit_id}/apply",
    dependencies=[Depends(require_scope(CREDIT_APPLY))],
    response_model=procurement_dto.VendorCreditOut,
)
async def apply_credit(
    credit_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementApplyCreditIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).apply_credit(
        credit_id, payload.get("applications") or [], actor_id=principal.app_user_id
    )


@router.post(
    "/vendor-credits/{credit_id}/post",
    dependencies=[Depends(require_scope(CREDIT_POST))],
    response_model=procurement_dto.VendorCreditOut,
)
async def post_credit(
    credit_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementPostCreditIn = procurement_dto.ProcurementPostCreditIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).post_credit(
        credit_id, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


# -------------------------------------------------------------------------------- returns -----
@router.get(
    "/vendor-returns",
    dependencies=[Depends(require_scope(RETURN_READ))],
    response_model=procurement_dto.VendorReturnPage,
)
async def list_returns(
    db: DbDep, principal: PrincipalDep, vendor_id: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return await _service(db, principal).list_returns(vendor_id=vendor_id, status=status, limit=_limit(limit))


@router.post(
    "/vendor-returns",
    status_code=201,
    dependencies=[Depends(require_scope(RETURN_WRITE))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def create_return(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateReturnIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_return(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/vendor-returns/{return_id}",
    dependencies=[Depends(require_scope(RETURN_READ))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def get_return(return_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_return(return_id)


@router.post(
    "/vendor-returns/{return_id}/approve",
    dependencies=[Depends(require_scope(RETURN_APPROVE))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def approve_return(
    return_id: str, db: DbDep, principal: PrincipalDep, idempotency_key: IdempotencyHeader = None
) -> dict[str, Any]:
    return await _service(db, principal).approve_return(return_id, actor_id=principal.app_user_id)


@router.post(
    "/vendor-returns/{return_id}/ship",
    dependencies=[Depends(require_scope(RETURN_SHIP))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def ship_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementShipReturnIn = procurement_dto.ProcurementShipReturnIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).ship_return(
        return_id, payload.get("lines") or [], actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.post(
    "/vendor-returns/{return_id}/credit",
    dependencies=[Depends(require_scope(RETURN_CREDIT))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def credit_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreditReturnIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).credit_return(return_id, payload, actor_id=principal.app_user_id)


@router.post(
    "/vendor-returns/{return_id}/close",
    dependencies=[Depends(require_scope(RETURN_CLOSE))],
    response_model=procurement_dto.VendorReturnAuthorizationOut,
)
async def close_return(
    return_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCloseReturnIn = procurement_dto.ProcurementCloseReturnIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).close_return(
        return_id, line_ids=payload.get("line_ids"), actor_id=principal.app_user_id
    )


# ---------------------------------------------------------------------------- drop-ship -------
@router.get(
    "/special-order-links",
    dependencies=[Depends(require_scope(PO_READ))],
    response_model=procurement_dto.SpecialOrderLinkPage,
)
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


@router.post(
    "/special-order-links",
    status_code=201,
    dependencies=[Depends(require_scope(DROPSHIP_WRITE))],
    response_model=procurement_dto.SpecialOrderLinkOut,
)
async def create_special_order_link(
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementCreateSpecialOrderLinkIn,
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).create_special_order_link(
        payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


@router.get(
    "/special-order-links/{link_id}",
    dependencies=[Depends(require_scope(PO_READ))],
    response_model=procurement_dto.SpecialOrderLinkOut,
)
async def get_special_order_link(link_id: str, db: DbDep, principal: PrincipalDep) -> dict[str, Any]:
    return await _service(db, principal).get_special_order_link(link_id)


@router.post(
    "/special-order-links/{link_id}/fulfil",
    dependencies=[Depends(require_scope(DROPSHIP_WRITE))],
    response_model=procurement_dto.SpecialOrderLinkOut,
)
async def fulfil_dropship(
    link_id: str,
    db: DbDep,
    principal: PrincipalDep,
    payload: procurement_dto.ProcurementFulfilDropshipIn = procurement_dto.ProcurementFulfilDropshipIn(),
    idempotency_key: IdempotencyHeader = None,
) -> dict[str, Any]:
    payload = payload.model_dump(exclude_unset=True)
    return await _service(db, principal).fulfil_dropship(
        link_id, payload, actor_id=principal.app_user_id, idempotency_key=idempotency_key
    )


__all__ = ["ROUTE_SCOPES", "router"]


# --------------------------------------------------------------------------- canonical aliases -----
# The FS-08 contract declares resource paths with the canonical ``{id}`` placeholder while the
# implementation historically used resource-specific names (``{po_id}``/``{vendor_id}``/...). These
# aliases mount the contract-declared operations without changing behaviour or concrete URLs: the
# wrapper renames ``id`` back to the implementation parameter and delegates to the same handler.
import inspect as _inspect  # noqa: E402

_CANONICAL_ALIASES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/api/v1/vendors/{vendor_id}", "/vendors/{id}", "vendor_id"),
    ("PATCH", "/api/v1/vendors/{vendor_id}", "/vendors/{id}", "vendor_id"),
    ("POST", "/api/v1/vendors/{vendor_id}/inactivate", "/vendors/{id}/inactivate", "vendor_id"),
    ("POST", "/api/v1/vendors/{vendor_id}/merge", "/vendors/{id}/merge", "vendor_id"),
    ("GET", "/api/v1/vendors/{vendor_id}/purchase-history", "/vendors/{id}/purchase-history", "vendor_id"),
    ("PATCH", "/api/v1/vendor-categories/{category_id}", "/vendor-categories/{id}", "category_id"),
    ("PATCH", "/api/v1/item-vendors/{item_vendor_id}", "/item-vendors/{id}", "item_vendor_id"),
    ("GET", "/api/v1/item-vendors/{item_vendor_id}/prices", "/item-vendors/{id}/prices", "item_vendor_id"),
    ("POST", "/api/v1/item-vendors/{item_vendor_id}/prices", "/item-vendors/{id}/prices", "item_vendor_id"),
    ("GET", "/api/v1/item-receipts/{receipt_id}", "/item-receipts/{id}", "receipt_id"),
    ("PATCH", "/api/v1/item-receipts/{receipt_id}", "/item-receipts/{id}", "receipt_id"),
    ("POST", "/api/v1/item-receipts/{receipt_id}/post", "/item-receipts/{id}/post", "receipt_id"),
    ("POST", "/api/v1/item-receipts/{receipt_id}/reverse", "/item-receipts/{id}/reverse", "receipt_id"),
    ("GET", "/api/v1/purchase-orders/{po_id}", "/purchase-orders/{id}", "po_id"),
    ("PATCH", "/api/v1/purchase-orders/{po_id}", "/purchase-orders/{id}", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/submit", "/purchase-orders/{id}/submit", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/approve", "/purchase-orders/{id}/approve", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/reject", "/purchase-orders/{id}/reject", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/cancel", "/purchase-orders/{id}/cancel", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/close", "/purchase-orders/{id}/close", "po_id"),
    ("GET", "/api/v1/purchase-orders/{po_id}/status", "/purchase-orders/{id}/status", "po_id"),
    ("GET", "/api/v1/purchase-orders/{po_id}/history", "/purchase-orders/{id}/history", "po_id"),
    ("GET", "/api/v1/purchase-orders/{po_id}/releases", "/purchase-orders/{id}/releases", "po_id"),
    ("POST", "/api/v1/purchase-orders/{po_id}/releases", "/purchase-orders/{id}/releases", "po_id"),
    ("GET", "/api/v1/purchase-requests/{request_id}", "/purchase-requests/{id}", "request_id"),
    ("POST", "/api/v1/purchase-requests/{request_id}/approve", "/purchase-requests/{id}/approve", "request_id"),
    (
        "POST",
        "/api/v1/purchase-requests/{request_id}/convert-to-po",
        "/purchase-requests/{id}/convert-to-po",
        "request_id",
    ),
    ("GET", "/api/v1/purchase-requisitions/{requisition_id}", "/purchase-requisitions/{id}", "requisition_id"),
    ("PATCH", "/api/v1/purchase-requisitions/{requisition_id}", "/purchase-requisitions/{id}", "requisition_id"),
    (
        "POST",
        "/api/v1/purchase-requisitions/{requisition_id}/submit",
        "/purchase-requisitions/{id}/submit",
        "requisition_id",
    ),
    (
        "POST",
        "/api/v1/purchase-requisitions/{requisition_id}/approve",
        "/purchase-requisitions/{id}/approve",
        "requisition_id",
    ),
    (
        "POST",
        "/api/v1/purchase-requisitions/{requisition_id}/order",
        "/purchase-requisitions/{id}/order",
        "requisition_id",
    ),
    ("GET", "/api/v1/rfqs/{rfq_id}", "/rfqs/{id}", "rfq_id"),
    ("POST", "/api/v1/rfqs/{rfq_id}/open", "/rfqs/{id}/open", "rfq_id"),
    ("POST", "/api/v1/rfqs/{rfq_id}/close", "/rfqs/{id}/close", "rfq_id"),
    ("POST", "/api/v1/rfqs/{rfq_id}/award", "/rfqs/{id}/award", "rfq_id"),
    ("GET", "/api/v1/rfqs/{rfq_id}/responses", "/rfqs/{id}/responses", "rfq_id"),
    ("POST", "/api/v1/rfqs/{rfq_id}/responses", "/rfqs/{id}/responses", "rfq_id"),
    ("GET", "/api/v1/special-order-links/{link_id}", "/special-order-links/{id}", "link_id"),
    ("POST", "/api/v1/special-order-links/{link_id}/fulfil", "/special-order-links/{id}/fulfil", "link_id"),
    ("GET", "/api/v1/vendor-bills/{bill_id}", "/vendor-bills/{id}", "bill_id"),
    ("PATCH", "/api/v1/vendor-bills/{bill_id}", "/vendor-bills/{id}", "bill_id"),
    ("POST", "/api/v1/vendor-bills/{bill_id}/submit", "/vendor-bills/{id}/submit", "bill_id"),
    ("POST", "/api/v1/vendor-bills/{bill_id}/approve", "/vendor-bills/{id}/approve", "bill_id"),
    ("POST", "/api/v1/vendor-bills/{bill_id}/cancel", "/vendor-bills/{id}/cancel", "bill_id"),
    ("POST", "/api/v1/vendor-bills/{bill_id}/match", "/vendor-bills/{id}/match", "bill_id"),
    ("POST", "/api/v1/vendor-bills/{bill_id}/post", "/vendor-bills/{id}/post", "bill_id"),
    ("GET", "/api/v1/vendor-bills/{bill_id}/variances", "/vendor-bills/{id}/variances", "bill_id"),
    ("GET", "/api/v1/vendor-contracts/{contract_id}", "/vendor-contracts/{id}", "contract_id"),
    ("PATCH", "/api/v1/vendor-contracts/{contract_id}", "/vendor-contracts/{id}", "contract_id"),
    ("POST", "/api/v1/vendor-contracts/{contract_id}/submit", "/vendor-contracts/{id}/submit", "contract_id"),
    ("POST", "/api/v1/vendor-contracts/{contract_id}/approve", "/vendor-contracts/{id}/approve", "contract_id"),
    ("POST", "/api/v1/vendor-contracts/{contract_id}/activate", "/vendor-contracts/{id}/activate", "contract_id"),
    ("POST", "/api/v1/vendor-contracts/{contract_id}/close", "/vendor-contracts/{id}/close", "contract_id"),
    ("GET", "/api/v1/vendor-credits/{credit_id}", "/vendor-credits/{id}", "credit_id"),
    ("POST", "/api/v1/vendor-credits/{credit_id}/apply", "/vendor-credits/{id}/apply", "credit_id"),
    ("POST", "/api/v1/vendor-credits/{credit_id}/post", "/vendor-credits/{id}/post", "credit_id"),
    ("GET", "/api/v1/vendor-returns/{return_id}", "/vendor-returns/{id}", "return_id"),
    ("POST", "/api/v1/vendor-returns/{return_id}/ship", "/vendor-returns/{id}/ship", "return_id"),
    ("POST", "/api/v1/vendor-returns/{return_id}/approve", "/vendor-returns/{id}/approve", "return_id"),
    ("POST", "/api/v1/vendor-returns/{return_id}/credit", "/vendor-returns/{id}/credit", "return_id"),
    ("POST", "/api/v1/vendor-returns/{return_id}/close", "/vendor-returns/{id}/close", "return_id"),
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
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            routes_by_path[(method.upper(), getattr(route, "path", ""))] = route
    for method, declared_path, relative_path, implementation_param in _CANONICAL_ALIASES:
        alias_route: Any = routes_by_path.get((method, declared_path))
        if alias_route is None:  # pragma: no cover - defensive: contract drift detected by tests
            continue
        endpoint = alias_route.endpoint
        alias = _alias_endpoint(endpoint, implementation_param)
        router.add_api_route(
            relative_path,
            alias,
            methods=[method],
            status_code=int(getattr(alias_route, "status_code", None) or 200),
            dependencies=[Depends(require_scope(ROUTE_SCOPES[(method, declared_path)]))],
            response_model=getattr(alias_route, "response_model", None),
        )
        ROUTE_SCOPES[(method, "/api/v1" + relative_path)] = ROUTE_SCOPES[(method, declared_path)]


_register_canonical_aliases()
