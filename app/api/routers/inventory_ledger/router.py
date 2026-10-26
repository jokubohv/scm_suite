"""FS-04 inventory ledger API (posting, balances, commitments, transfers, adjustments, imports).

The integrator registers ``router`` in ``app/api/main.py``; no edits to main/registry/deps. Every
posting/command POST requires ``Idempotency-Key``; reads/writes are tenant-scoped and catalogue-scoped.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.api.schemas.common import ActionResult
from app.api.schemas.inventory import (
    AdjustmentCreate,
    AdjustmentOut,
    AdjustmentPage,
    AdjustmentPatch,
    AdjustmentPostRequest,
    AvailabilityOut,
    BalanceBinPage,
    BalanceLotPage,
    BalancePage,
    BulkPatchIn,
    CloseRequest,
    CommitmentCreate,
    CommitmentOut,
    CommitmentPage,
    CommitmentReallocateRequest,
    CommitmentReleaseRequest,
    DistributionCreate,
    DistributionOut,
    DistributionPage,
    FulfilmentCreate,
    FulfilmentResultOut,
    ImportBatchCreate,
    ImportBatchOut,
    ImportBatchPage,
    ImportPostResultOut,
    ImportRowPage,
    ImportValidationOut,
    InTransitPage,
    LedgerActionOut,
    PostingCommandIn,
    ReallocateOut,
    ReasonCodeCreate,
    ReasonCodeOut,
    ReasonCodePage,
    ReasonCodeUpdate,
    ReasonRequest,
    ReturnCreate,
    ReturnResultOut,
    TransactionDetailPage,
    TransactionLinePage,
    TransactionOut,
    TransactionPage,
    TransferBulkOut,
    TransferCreate,
    TransferOrderBulkOut,
    TransferOrderCreate,
    TransferOrderLinePage,
    TransferOrderOut,
    TransferOrderPage,
    TransferOrderStageRequest,
    TransferOrderUpdate,
    TransferOut,
    TransferPage,
    TransferUpdate,
    WorksheetBulkOut,
    WorksheetCreate,
    WorksheetOut,
    WorksheetPage,
    WorksheetUpdate,
)
from app.modules.inventory_ledger.documents import LedgerDocuments
from app.modules.inventory_ledger.errors import IdempotencyKeyRequired, ledger_error
from app.modules.inventory_ledger.service import LedgerService

router = APIRouter(prefix="/api/v1", tags=["inventory-ledger"])

TX_READ = "inventory.transaction.read"
TX_POST = "inventory.transaction.post"
TX_REVERSE = "inventory.transaction.reverse"
ADJ_CREATE = "inventory.adjustment.create"
ADJ_POST = "inventory.adjustment.post"
WS_CREATE = "inventory.worksheet.create"
WS_POST = "inventory.worksheet.post"
TRF_CREATE = "inventory.transfer.create"
TRF_POST = "inventory.transfer.post"
TO_CREATE = "inventory.to.create"
TO_APPROVE = "inventory.to.approve"
TO_FULFIL = "inventory.to.fulfil"
TO_RECEIVE = "inventory.to.receive"
TO_CLOSE = "inventory.to.close"
COMMIT = "inventory.commitment.commit"
RELEASE = "inventory.commitment.release"
REALLOCATE = "inventory.commitment.reallocate"
FULFIL = "inventory.fulfillment.post"
RETURN = "inventory.return.post"
BALANCE = "inventory.balance.read"
DIST_CREATE = "inventory.distribution.create"
DIST_POST = "inventory.distribution.post"
IMPORT_CREATE = "inventory.import.create"
IMPORT_POST = "inventory.import.post"
REASON = "inventory.reason.manage"


def _ledger(db: AsyncSession, principal: PrincipalDep) -> LedgerService:
    return LedgerService(db, principal.tenant_id)


def _documents(db: AsyncSession, principal: PrincipalDep) -> LedgerDocuments:
    return LedgerDocuments(_ledger(db, principal))


async def idempotency_key(x_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if not x_idempotency_key:
        raise IdempotencyKeyRequired("Idempotency-Key header is required")
    return x_idempotency_key


IdemDep = Annotated[str | None, Depends(idempotency_key)]
ReadDep = Annotated[PrincipalDep, Depends(require_scope(TX_READ))]
TxPostDep = Annotated[PrincipalDep, Depends(require_scope(TX_POST))]
TxReverseDep = Annotated[PrincipalDep, Depends(require_scope(TX_REVERSE))]
AdjCreateDep = Annotated[PrincipalDep, Depends(require_scope(ADJ_CREATE))]
AdjPostDep = Annotated[PrincipalDep, Depends(require_scope(ADJ_POST))]
WsCreateDep = Annotated[PrincipalDep, Depends(require_scope(WS_CREATE))]
WsPostDep = Annotated[PrincipalDep, Depends(require_scope(WS_POST))]
TrfCreateDep = Annotated[PrincipalDep, Depends(require_scope(TRF_CREATE))]
TrfPostDep = Annotated[PrincipalDep, Depends(require_scope(TRF_POST))]
ToCreateDep = Annotated[PrincipalDep, Depends(require_scope(TO_CREATE))]
ToApproveDep = Annotated[PrincipalDep, Depends(require_scope(TO_APPROVE))]
ToFulfilDep = Annotated[PrincipalDep, Depends(require_scope(TO_FULFIL))]
ToReceiveDep = Annotated[PrincipalDep, Depends(require_scope(TO_RECEIVE))]
ToCloseDep = Annotated[PrincipalDep, Depends(require_scope(TO_CLOSE))]
CommitDep = Annotated[PrincipalDep, Depends(require_scope(COMMIT))]
ReleaseDep = Annotated[PrincipalDep, Depends(require_scope(RELEASE))]
ReallocateDep = Annotated[PrincipalDep, Depends(require_scope(REALLOCATE))]
FulfilDep = Annotated[PrincipalDep, Depends(require_scope(FULFIL))]
ReturnDep = Annotated[PrincipalDep, Depends(require_scope(RETURN))]
BalanceDep = Annotated[PrincipalDep, Depends(require_scope(BALANCE))]
DistCreateDep = Annotated[PrincipalDep, Depends(require_scope(DIST_CREATE))]
DistPostDep = Annotated[PrincipalDep, Depends(require_scope(DIST_POST))]
ImportCreateDep = Annotated[PrincipalDep, Depends(require_scope(IMPORT_CREATE))]
ImportPostDep = Annotated[PrincipalDep, Depends(require_scope(IMPORT_POST))]
ReasonDep = Annotated[PrincipalDep, Depends(require_scope(REASON))]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


def _version(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError:
        raise ledger_error(
            "If-Match must be an integer document version", code="VALIDATION_FAILED", http_status=422
        ) from None


async def _bulk_patch(db: DbDep, principal: PrincipalDep, document: str, payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise ledger_error("ids must be a non-empty list", code="VALIDATION_FAILED", http_status=422)
    patch = payload.get("patch") or {}
    if not isinstance(patch, dict):
        raise ledger_error("patch must be an object", code="VALIDATION_FAILED", http_status=422)
    versions = payload.get("versions") or {}
    documents = _documents(db, principal)
    updated: list[dict[str, Any]] = []
    for document_id in ids:
        expected = (
            _version(str(versions.get(document_id))) if isinstance(versions, dict) and document_id in versions else None
        )
        updated.append(await documents.update_document(document, document_id, patch, expected_version=expected))
    return {"items": updated, "updated": len(updated)}

    # --------------------------------------------------------------------------- transactions -----


@router.get("/inventory-transactions", response_model=TransactionPage)
async def list_transactions(
    principal: ReadDep,
    db: DbDep,
    item_id: str | None = None,
    location_id: str | None = None,
    transaction_type: str | None = None,
    source_doc_type: str | None = None,
    source_doc_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    return await _ledger(db, principal).list_transactions(
        item_id=item_id,
        location_id=location_id,
        transaction_type=transaction_type,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )


@router.get("/inventory-transactions/{id}", response_model=TransactionOut)
async def get_transaction(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(id)


@router.get("/inventory-transactions/{id}/lines", response_model=TransactionLinePage)
async def get_transaction_lines(id: str, principal: ReadDep, db: DbDep, item_id: str | None = None) -> dict[str, Any]:
    return {"items": await _ledger(db, principal).transaction_lines(id, item_id=item_id)}


@router.get("/inventory-transactions/{id}/details", response_model=TransactionDetailPage)
async def get_transaction_details(id: str, principal: ReadDep, db: DbDep, line_id: str | None = None) -> dict[str, Any]:
    return {"items": await _ledger(db, principal).transaction_details(id, line_id=line_id)}


@router.post("/inventory-transactions/{id}/reverse", response_model=ActionResult)
async def reverse_transaction(
    id: str, principal: TxReverseDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _ledger(db, principal).reverse(
        id, reason=(body.reason if body and body.reason else "reversal"), idempotency_key=key or ""
    )


@router.post("/inventory-transactions", status_code=201, response_model=ActionResult)
async def generic_posting(
    body: PostingCommandIn, principal: TxPostDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, mode="json")
    return await _ledger(db, principal).post(payload | {"idempotency_key": key})

    # --------------------------------------------------------------------------- adjustments ------


@router.post("/inventory-adjustments", status_code=201, response_model=AdjustmentOut)
async def create_adjustment(
    body: AdjustmentCreate, principal: AdjCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_adjustment(
        body.model_dump(exclude_unset=True) | {"actor_id": principal.identity_id}, idempotency_key=key or ""
    )


@router.get("/inventory-adjustments", response_model=AdjustmentPage)
async def list_adjustments(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_adjustment", status=status)


@router.get("/inventory-adjustments/{id}", response_model=AdjustmentOut)
async def get_adjustment(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_adjustment", id)


@router.post("/inventory-adjustments/{id}/submit", response_model=LedgerActionOut)
async def submit_adjustment(id: str, principal: AdjCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_adjustment", id)


@router.post("/inventory-adjustments/{id}/post", response_model=LedgerActionOut)
async def post_adjustment(
    id: str, principal: AdjPostDep, db: DbDep, key: IdemDep = None, body: AdjustmentPostRequest | None = None
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, mode="json") if body else {}
    return await _documents(db, principal).post_adjustment(
        id, idempotency_key=key or "", transaction_date=payload.get("transaction_date")
    )


@router.post("/inventory-adjustments/{id}/reverse", response_model=LedgerActionOut)
async def reverse_adjustment(
    id: str, principal: TxReverseDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document(
        "inventory_adjustment", id, reason=(body.reason if body and body.reason else "reversal"), idempotency_key=key or ""
    )


@router.post("/inventory-adjustments/{id}/void", response_model=LedgerActionOut)
async def void_adjustment(
    id: str, principal: AdjPostDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).void_document(
        "inventory_adjustment", id, reason=(body.reason if body and body.reason else "void")
    )

    # --------------------------------------------------------------------------- worksheets --------


@router.post("/inventory-worksheets", status_code=201, response_model=WorksheetOut)
async def create_worksheet(
    body: WorksheetCreate, principal: WsCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_worksheet(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/inventory-worksheets", response_model=WorksheetPage)
async def list_worksheets(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_worksheet", status=status)


@router.get("/inventory-worksheets/{id}", response_model=WorksheetOut)
async def get_worksheet(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_worksheet", id)


@router.post("/inventory-worksheets/{id}/submit", response_model=WorksheetOut)
async def submit_worksheet(id: str, principal: WsCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_worksheet", id)


@router.post("/inventory-worksheets/{id}/post", response_model=ActionResult)
async def post_worksheet(id: str, principal: WsPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_worksheet(id, idempotency_key=key or "")


@router.post("/inventory-worksheets/{id}/reverse", response_model=ActionResult)
async def reverse_worksheet(
    id: str, principal: TxReverseDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document(
        "inventory_worksheet", id, reason=(body.reason if body and body.reason else "reversal"), idempotency_key=key or ""
    )


@router.post("/inventory-worksheets/{id}/void", response_model=ActionResult)
async def void_worksheet(
    id: str, principal: WsPostDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).void_document(
        "inventory_worksheet", id, reason=(body.reason if body and body.reason else "void")
    )

    # --------------------------------------------------------------------------- transfers ---------


@router.post("/inventory-transfers", status_code=201, response_model=TransferOut)
async def create_transfer(
    body: TransferCreate, principal: TrfCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_transfer(
        body.model_dump(exclude_unset=True), idempotency_key=key or ""
    )


@router.get("/inventory-transfers", response_model=TransferPage)
async def list_transfers(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_transfer", status=status)


@router.get("/inventory-transfers/{id}", response_model=TransferOut)
async def get_transfer(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_transfer", id)


@router.post("/inventory-transfers/{id}/submit", response_model=LedgerActionOut)
async def submit_transfer(id: str, principal: TrfCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_transfer", id)


@router.post("/inventory-transfers/{id}/post", response_model=LedgerActionOut)
async def post_transfer(id: str, principal: TrfPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_transfer(id, idempotency_key=key or "")


@router.post("/inventory-transfers/{id}/reverse", response_model=LedgerActionOut)
async def reverse_transfer(
    id: str, principal: TxReverseDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document(
        "inventory_transfer", id, reason=(body.reason if body and body.reason else "reversal"), idempotency_key=key or ""
    )


@router.post("/inventory-transfers/{id}/void", response_model=LedgerActionOut)
async def void_transfer(
    id: str, principal: TrfPostDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).void_document(
        "inventory_transfer", id, reason=(body.reason if body and body.reason else "void")
    )

    # --------------------------------------------------------------------------- transfer orders ---


@router.post("/transfer-orders", status_code=201, response_model=TransferOrderOut)
async def create_transfer_order(
    body: TransferOrderCreate, principal: ToCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_transfer_order(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/transfer-orders", response_model=TransferOrderPage)
async def list_transfer_orders(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("transfer_order", status=status)


@router.get("/transfer-orders/{id}", response_model=TransferOrderOut)
async def get_transfer_order(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("transfer_order", id)


@router.get("/transfer-orders/{id}/lines", response_model=TransferOrderLinePage)
async def get_transfer_order_lines(id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return {"items": (await _documents(db, principal).get_document("transfer_order", id))["lines"]}


@router.post("/transfer-orders/{id}/submit", response_model=TransferOrderOut)
async def submit_transfer_order(id: str, principal: ToCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_transfer_order(id)


@router.post("/transfer-orders/{id}/approve", response_model=ActionResult)
async def approve_transfer_order(id: str, principal: ToApproveDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).approve_transfer_order(id)


@router.post("/transfer-orders/{id}/fulfil", response_model=ActionResult)
async def fulfil_transfer_order(
    id: str, body: TransferOrderStageRequest, principal: ToFulfilDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).fulfil_transfer_order(
        id, body.model_dump(exclude_unset=True, mode="json").get("lines") or [], idempotency_key=key or ""
    )


@router.post("/transfer-orders/{id}/receive", response_model=ActionResult)
async def receive_transfer_order(
    id: str, body: TransferOrderStageRequest, principal: ToReceiveDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).receive_transfer_order(
        id, body.model_dump(exclude_unset=True, mode="json").get("lines") or [], idempotency_key=key or ""
    )


@router.post("/transfer-orders/{id}/close", response_model=ActionResult)
async def close_transfer_order(
    id: str, principal: ToCloseDep, db: DbDep, key: IdemDep = None, body: CloseRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).close_transfer_order(id, reason=(body.reason if body else None))


@router.post("/transfer-orders/{id}/cancel", response_model=ActionResult)
async def cancel_transfer_order(
    id: str, principal: ToCreateDep, db: DbDep, key: IdemDep = None, body: CloseRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).cancel_transfer_order(id, reason=(body.reason if body else None))

    # --------------------------------------------------------------------------- commitments -------


@router.post("/inventory/commitments", status_code=201, response_model=ActionResult)
async def commit_orders(
    body: CommitmentCreate, principal: CommitDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).commit(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/inventory/commitments", response_model=CommitmentPage)
async def list_commitments(
    principal: BalanceDep,
    db: DbDep,
    item_id: str | None = None,
    location_id: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    external_ref: str | None = None,
) -> dict[str, Any]:
    return await _documents(db, principal).list_commitments(
        item_id=item_id, location_id=location_id, source_type=source_type, status=status, external_ref=external_ref
    )


@router.get("/inventory/commitments/{id}", response_model=CommitmentOut)
async def get_commitment(id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_commitment(id)


@router.post("/inventory/commitments/{id}/release", response_model=ActionResult)
async def release_commitment(
    id: str, principal: ReleaseDep, db: DbDep, key: IdemDep = None, body: CommitmentReleaseRequest | None = None
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, mode="json") if body else {}
    return await _documents(db, principal).release_commitment(
        id, qty=payload.get("qty"), reason=payload.get("reason")
    )


@router.post("/inventory/commitments/reallocate", response_model=ReallocateOut)
async def reallocate(
    body: CommitmentReallocateRequest, principal: ReallocateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).reallocate(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )

    # --------------------------------------------------------------------------- fulfil/return ------


@router.post("/fulfillments", status_code=201, response_model=FulfilmentResultOut)
async def post_fulfillment(
    body: FulfilmentCreate, principal: FulfilDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).post_fulfillment(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/fulfillments/{transaction_id}", response_model=TransactionOut)
async def get_fulfillment(transaction_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(transaction_id)


@router.post("/returns", status_code=201, response_model=ReturnResultOut)
async def post_return(body: ReturnCreate, principal: ReturnDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_return(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/returns/{transaction_id}", response_model=TransactionOut)
async def get_return(transaction_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(transaction_id)

    # --------------------------------------------------------------------------- balances ----------


@router.get("/inventory/balances", response_model=BalancePage)
async def balances(
    principal: BalanceDep,
    db: DbDep,
    item_id: str | None = None,
    location_id: str | None = None,
    inventory_status_id: str | None = None,
    ownership: str | None = None,
    qty_class: str | None = None,
    lot_id: str | None = None,
    serial_id: str | None = None,
) -> dict[str, Any]:
    return await _ledger(db, principal).balances(
        item_id=item_id,
        location_id=location_id,
        inventory_status_id=inventory_status_id,
        ownership=ownership,
        qty_class=qty_class,
        lot_id=lot_id,
        serial_id=serial_id,
    )


@router.get("/inventory/balances/by-bin", response_model=BalanceBinPage)
async def balances_by_bin(
    principal: BalanceDep,
    db: DbDep,
    item_id: str | None = None,
    location_id: str | None = None,
    bin_id: str | None = None,
) -> dict[str, Any]:
    return await _ledger(db, principal).balance_by_bin(item_id=item_id, location_id=location_id, bin_id=bin_id)


@router.get("/inventory/balances/by-lot", response_model=BalanceLotPage)
async def balances_by_lot(
    principal: BalanceDep,
    db: DbDep,
    item_id: str | None = None,
    location_id: str | None = None,
    lot_id: str | None = None,
) -> dict[str, Any]:
    return await _ledger(db, principal).balance_by_lot(item_id=item_id, location_id=location_id, lot_id=lot_id)


@router.get("/inventory/availability", response_model=AvailabilityOut)
async def availability(
    principal: BalanceDep, db: DbDep, item_id: str, location_id: str, inventory_status_id: str | None = None
) -> dict[str, Any]:
    return await _ledger(db, principal).availability_summary(
        item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id
    )


@router.get("/inventory/in-transit", response_model=InTransitPage)
async def in_transit(
    principal: BalanceDep,
    db: DbDep,
    item_id: str | None = None,
    to_location_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return await _ledger(db, principal).in_transit(item_id=item_id, to_location_id=to_location_id, status=status)

    # --------------------------------------------------------------------------- distributions -----


@router.post("/inventory/distributions", status_code=201, response_model=DistributionOut)
async def create_distribution(
    body: DistributionCreate, principal: DistCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_distribution(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.get("/inventory/distributions", response_model=DistributionPage)
async def list_distributions(principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_distribution", status=status)


@router.get("/inventory/distributions/{id}", response_model=DistributionOut)
async def get_distribution(id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_distribution", id)


@router.post("/inventory/distributions/{id}/post", response_model=ActionResult)
async def post_distribution(id: str, principal: DistPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_distribution(id, idempotency_key=key or "")


@router.post("/inventory/distributions/{id}/reverse", response_model=ActionResult)
async def reverse_distribution(
    id: str, principal: TxReverseDep, db: DbDep, key: IdemDep = None, body: ReasonRequest | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document(
        "inventory_distribution", id, reason=(body.reason if body and body.reason else "reversal"), idempotency_key=key or ""
    )

    # --------------------------------------------------------------------------- imports ------------


@router.post("/imports", status_code=201, response_model=ImportBatchOut)
async def create_import(
    body: ImportBatchCreate, principal: ImportCreateDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    payload = body.model_dump(exclude_unset=True, mode="json")
    if not payload.get("filename") and payload.get("file_name"):
        payload["filename"] = payload["file_name"]
    return await _documents(db, principal).create_import(payload, idempotency_key=key or "")


@router.get("/imports", response_model=ImportBatchPage)
async def list_imports(principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("import_batch", status=status)


@router.get("/imports/{id}", response_model=ImportBatchOut)
async def get_import(id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_import(id)


@router.get("/imports/{id}/rows", response_model=ImportRowPage)
async def get_import_rows(id: str, principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).import_rows(id, status=status)


@router.post("/imports/{id}/validate", response_model=ImportValidationOut)
async def validate_import(id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).validate_import(id)


@router.post("/imports/{id}/post", response_model=ImportPostResultOut)
async def post_import(id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_import(id, idempotency_key=key or "")


@router.post("/imports/{id}/retry", response_model=ImportPostResultOut)
async def retry_import(id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_import(id, idempotency_key=key or "")

    # --------------------------------------------------------------------------- reason codes ------


@router.get("/inventory-reason-codes", response_model=ReasonCodePage)
async def list_reason_codes(
    principal: BalanceDep, db: DbDep, reason_type: str | None = None, active: bool | None = None
) -> dict[str, Any]:
    return await _documents(db, principal).list_reason_codes(reason_type=reason_type, active=active)


@router.post("/inventory-reason-codes", status_code=201, response_model=ReasonCodeOut)
async def create_reason_code(
    body: ReasonCodeCreate, principal: ReasonDep, db: DbDep, key: IdemDep = None
) -> dict[str, Any]:
    return await _documents(db, principal).create_reason_code(
        body.model_dump(exclude_unset=True, mode="json"), idempotency_key=key or ""
    )


@router.patch("/inventory-reason-codes/{id}", response_model=ReasonCodeOut)
async def update_reason_code(id: str, body: ReasonCodeUpdate, principal: ReasonDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).update_reason_code(
        id, body.model_dump(exclude_unset=True, mode="json")
    )

    # --------------------------------------------------------------------------- pre-post edits -----


@router.patch(
    "/inventory-adjustments/{id}",
    dependencies=[Depends(require_scope(ADJ_CREATE))],
    response_model=AdjustmentOut,
)
async def patch_adjustment(
    id: str, body: AdjustmentPatch, principal: PrincipalDep, db: DbDep, if_match: IfMatch = None
) -> dict[str, Any]:
    return await _documents(db, principal).update_document(
        "inventory_adjustment", id, body.model_dump(exclude_unset=True, mode="json"), expected_version=_version(if_match)
    )


@router.patch(
    "/inventory-worksheets",
    dependencies=[Depends(require_scope(WS_CREATE))],
    response_model=WorksheetBulkOut,
)
async def bulk_patch_worksheets(body: BulkPatchIn, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return await _bulk_patch(db, principal, "inventory_worksheet", body.model_dump(exclude_unset=True, mode="json"))


@router.patch(
    "/inventory-worksheets/{id}",
    dependencies=[Depends(require_scope(WS_CREATE))],
    response_model=WorksheetOut,
)
async def patch_worksheet(
    id: str, body: WorksheetUpdate, principal: PrincipalDep, db: DbDep, if_match: IfMatch = None
) -> dict[str, Any]:
    return await _documents(db, principal).update_document(
        "inventory_worksheet", id, body.model_dump(exclude_unset=True, mode="json"), expected_version=_version(if_match)
    )


@router.patch(
    "/inventory-transfers",
    dependencies=[Depends(require_scope(TRF_CREATE))],
    response_model=TransferBulkOut,
)
async def bulk_patch_transfers(body: BulkPatchIn, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return await _bulk_patch(db, principal, "inventory_transfer", body.model_dump(exclude_unset=True, mode="json"))


@router.patch(
    "/inventory-transfers/{id}",
    dependencies=[Depends(require_scope(TRF_CREATE))],
    response_model=TransferOut,
)
async def patch_transfer(
    id: str, body: TransferUpdate, principal: PrincipalDep, db: DbDep, if_match: IfMatch = None
) -> dict[str, Any]:
    return await _documents(db, principal).update_document(
        "inventory_transfer", id, body.model_dump(exclude_unset=True, mode="json"), expected_version=_version(if_match)
    )


@router.patch(
    "/transfer-orders",
    dependencies=[Depends(require_scope(TO_CREATE))],
    response_model=TransferOrderBulkOut,
)
async def bulk_patch_transfer_orders(body: BulkPatchIn, principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return await _bulk_patch(db, principal, "transfer_order", body.model_dump(exclude_unset=True, mode="json"))


@router.patch(
    "/transfer-orders/{id}",
    dependencies=[Depends(require_scope(TO_CREATE))],
    response_model=TransferOrderOut,
)
async def patch_transfer_order(
    id: str, body: TransferOrderUpdate, principal: PrincipalDep, db: DbDep, if_match: IfMatch = None
) -> dict[str, Any]:
    return await _documents(db, principal).update_document(
        "transfer_order", id, body.model_dump(exclude_unset=True, mode="json"), expected_version=_version(if_match)
    )

    # --------------------------------------------------------------------------- distributions -----


@router.post("/inventory/distributions/{id}/approve", response_model=DistributionOut)
async def approve_distribution(id: str, principal: DistCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).approve_distribution(id)


__all__ = ["router"]
