"""FS-04 inventory ledger API (posting, balances, commitments, transfers, adjustments, imports).

The integrator registers ``router`` in ``app/api/main.py``; no edits to main/registry/deps. Every
posting/command POST requires ``Idempotency-Key``; reads/writes are tenant-scoped and catalogue-scoped.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, PrincipalDep, require_scope
from app.modules.inventory_ledger.documents import LedgerDocuments
from app.modules.inventory_ledger.errors import IdempotencyKeyRequired
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


# --------------------------------------------------------------------------- transactions -----
@router.get("/inventory-transactions")
async def list_transactions(
    principal: ReadDep, db: DbDep, item_id: str | None = None, location_id: str | None = None,
    transaction_type: str | None = None, source_doc_type: str | None = None, source_doc_id: str | None = None,
    date_from: str | None = None, date_to: str | None = None, cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    return await _ledger(db, principal).list_transactions(
        item_id=item_id, location_id=location_id, transaction_type=transaction_type,
        source_doc_type=source_doc_type, source_doc_id=source_doc_id, date_from=date_from, date_to=date_to,
        cursor=cursor, limit=limit,
    )


@router.get("/inventory-transactions/{transaction_id}")
async def get_transaction(transaction_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(transaction_id)


@router.get("/inventory-transactions/{transaction_id}/lines")
async def get_transaction_lines(transaction_id: str, principal: ReadDep, db: DbDep, item_id: str | None = None) -> dict[str, Any]:
    return {"items": await _ledger(db, principal).transaction_lines(transaction_id, item_id=item_id)}


@router.get("/inventory-transactions/{transaction_id}/details")
async def get_transaction_details(transaction_id: str, principal: ReadDep, db: DbDep, line_id: str | None = None) -> dict[str, Any]:
    return {"items": await _ledger(db, principal).transaction_details(transaction_id, line_id=line_id)}


@router.post("/inventory-transactions/{transaction_id}/reverse")
async def reverse_transaction(transaction_id: str, payload: dict[str, Any], principal: TxReverseDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _ledger(db, principal).reverse(transaction_id, reason=str(payload.get("reason") or "reversal"), idempotency_key=key or "")


@router.post("/inventory-transactions", status_code=201)
async def generic_posting(payload: dict[str, Any], principal: TxPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _ledger(db, principal).post(payload | {"idempotency_key": key})


# --------------------------------------------------------------------------- adjustments ------
@router.post("/inventory-adjustments", status_code=201)
async def create_adjustment(payload: dict[str, Any], principal: AdjCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_adjustment(payload | {"actor_id": principal.identity_id}, idempotency_key=key or "")


@router.get("/inventory-adjustments")
async def list_adjustments(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_adjustment", status=status)


@router.get("/inventory-adjustments/{document_id}")
async def get_adjustment(document_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_adjustment", document_id)


@router.post("/inventory-adjustments/{document_id}/submit")
async def submit_adjustment(document_id: str, principal: AdjCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_adjustment", document_id)


@router.post("/inventory-adjustments/{document_id}/post")
async def post_adjustment(document_id: str, principal: AdjPostDep, db: DbDep, key: IdemDep = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _documents(db, principal).post_adjustment(document_id, idempotency_key=key or "", transaction_date=(payload or {}).get("transaction_date"))


@router.post("/inventory-adjustments/{document_id}/reverse")
async def reverse_adjustment(document_id: str, payload: dict[str, Any], principal: TxReverseDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document("inventory_adjustment", document_id, reason=str(payload.get("reason") or "reversal"), idempotency_key=key or "")


@router.post("/inventory-adjustments/{document_id}/void")
async def void_adjustment(document_id: str, payload: dict[str, Any], principal: AdjPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).void_document("inventory_adjustment", document_id, reason=str(payload.get("reason") or "void"))


# --------------------------------------------------------------------------- worksheets --------
@router.post("/inventory-worksheets", status_code=201)
async def create_worksheet(payload: dict[str, Any], principal: WsCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_worksheet(payload, idempotency_key=key or "")


@router.get("/inventory-worksheets")
async def list_worksheets(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_worksheet", status=status)


@router.get("/inventory-worksheets/{document_id}")
async def get_worksheet(document_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_worksheet", document_id)


@router.post("/inventory-worksheets/{document_id}/submit")
async def submit_worksheet(document_id: str, principal: WsCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_worksheet", document_id)


@router.post("/inventory-worksheets/{document_id}/post")
async def post_worksheet(document_id: str, principal: WsPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_worksheet(document_id, idempotency_key=key or "")


@router.post("/inventory-worksheets/{document_id}/reverse")
async def reverse_worksheet(document_id: str, payload: dict[str, Any], principal: TxReverseDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document("inventory_worksheet", document_id, reason=str(payload.get("reason") or "reversal"), idempotency_key=key or "")


@router.post("/inventory-worksheets/{document_id}/void")
async def void_worksheet(document_id: str, payload: dict[str, Any], principal: WsPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).void_document("inventory_worksheet", document_id, reason=str(payload.get("reason") or "void"))


# --------------------------------------------------------------------------- transfers ---------
@router.post("/inventory-transfers", status_code=201)
async def create_transfer(payload: dict[str, Any], principal: TrfCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_transfer(payload, idempotency_key=key or "")


@router.get("/inventory-transfers")
async def list_transfers(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_transfer", status=status)


@router.get("/inventory-transfers/{document_id}")
async def get_transfer(document_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_transfer", document_id)


@router.post("/inventory-transfers/{document_id}/submit")
async def submit_transfer(document_id: str, principal: TrfCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_document("inventory_transfer", document_id)


@router.post("/inventory-transfers/{document_id}/post")
async def post_transfer(document_id: str, principal: TrfPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_transfer(document_id, idempotency_key=key or "")


@router.post("/inventory-transfers/{document_id}/reverse")
async def reverse_transfer(document_id: str, payload: dict[str, Any], principal: TxReverseDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document("inventory_transfer", document_id, reason=str(payload.get("reason") or "reversal"), idempotency_key=key or "")


@router.post("/inventory-transfers/{document_id}/void")
async def void_transfer(document_id: str, payload: dict[str, Any], principal: TrfPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).void_document("inventory_transfer", document_id, reason=str(payload.get("reason") or "void"))


# --------------------------------------------------------------------------- transfer orders ---
@router.post("/transfer-orders", status_code=201)
async def create_transfer_order(payload: dict[str, Any], principal: ToCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_transfer_order(payload, idempotency_key=key or "")


@router.get("/transfer-orders")
async def list_transfer_orders(principal: ReadDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("transfer_order", status=status)


@router.get("/transfer-orders/{order_id}")
async def get_transfer_order(order_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("transfer_order", order_id)


@router.get("/transfer-orders/{order_id}/lines")
async def get_transfer_order_lines(order_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return {"items": (await _documents(db, principal).get_document("transfer_order", order_id))["lines"]}


@router.post("/transfer-orders/{order_id}/submit")
async def submit_transfer_order(order_id: str, principal: ToCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).submit_transfer_order(order_id)


@router.post("/transfer-orders/{order_id}/approve")
async def approve_transfer_order(order_id: str, principal: ToApproveDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).approve_transfer_order(order_id)


@router.post("/transfer-orders/{order_id}/fulfil")
async def fulfil_transfer_order(order_id: str, payload: dict[str, Any], principal: ToFulfilDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).fulfil_transfer_order(order_id, payload.get("lines") or [], idempotency_key=key or "")


@router.post("/transfer-orders/{order_id}/receive")
async def receive_transfer_order(order_id: str, payload: dict[str, Any], principal: ToReceiveDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).receive_transfer_order(order_id, payload.get("lines") or [], idempotency_key=key or "")


@router.post("/transfer-orders/{order_id}/close")
async def close_transfer_order(order_id: str, principal: ToCloseDep, db: DbDep, key: IdemDep = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _documents(db, principal).close_transfer_order(order_id, reason=(payload or {}).get("reason"))


@router.post("/transfer-orders/{order_id}/cancel")
async def cancel_transfer_order(order_id: str, principal: ToCreateDep, db: DbDep, key: IdemDep = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _documents(db, principal).cancel_transfer_order(order_id, reason=(payload or {}).get("reason"))


# --------------------------------------------------------------------------- commitments -------
@router.post("/inventory/commitments", status_code=201)
async def commit_orders(payload: dict[str, Any], principal: CommitDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).commit(payload, idempotency_key=key or "")


@router.get("/inventory/commitments")
async def list_commitments(principal: BalanceDep, db: DbDep, item_id: str | None = None, location_id: str | None = None,
                           source_type: str | None = None, status: str | None = None, external_ref: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_commitments(item_id=item_id, location_id=location_id, source_type=source_type, status=status, external_ref=external_ref)


@router.get("/inventory/commitments/{commitment_id}")
async def get_commitment(commitment_id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_commitment(commitment_id)


@router.post("/inventory/commitments/{commitment_id}/release")
async def release_commitment(commitment_id: str, principal: ReleaseDep, db: DbDep, key: IdemDep = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _documents(db, principal).release_commitment(commitment_id, qty=(payload or {}).get("qty"), reason=(payload or {}).get("reason"))


@router.post("/inventory/commitments/reallocate")
async def reallocate(principal: ReallocateDep, db: DbDep, key: IdemDep = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _documents(db, principal).reallocate(payload or {}, idempotency_key=key or "")


# --------------------------------------------------------------------------- fulfil/return ------
@router.post("/fulfillments", status_code=201)
async def post_fulfillment(payload: dict[str, Any], principal: FulfilDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_fulfillment(payload, idempotency_key=key or "")


@router.get("/fulfillments/{transaction_id}")
async def get_fulfillment(transaction_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(transaction_id)


@router.post("/returns", status_code=201)
async def post_return(payload: dict[str, Any], principal: ReturnDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_return(payload, idempotency_key=key or "")


@router.get("/returns/{transaction_id}")
async def get_return(transaction_id: str, principal: ReadDep, db: DbDep) -> dict[str, Any]:
    return await _ledger(db, principal).get_transaction(transaction_id)


# --------------------------------------------------------------------------- balances ----------
@router.get("/inventory/balances")
async def balances(principal: BalanceDep, db: DbDep, item_id: str | None = None, location_id: str | None = None,
                   inventory_status_id: str | None = None, ownership: str | None = None, qty_class: str | None = None,
                   lot_id: str | None = None, serial_id: str | None = None) -> dict[str, Any]:
    return await _ledger(db, principal).balances(item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id, ownership=ownership, qty_class=qty_class, lot_id=lot_id, serial_id=serial_id)


@router.get("/inventory/balances/by-bin")
async def balances_by_bin(principal: BalanceDep, db: DbDep, item_id: str | None = None, location_id: str | None = None, bin_id: str | None = None) -> dict[str, Any]:
    return await _ledger(db, principal).balance_by_bin(item_id=item_id, location_id=location_id, bin_id=bin_id)


@router.get("/inventory/balances/by-lot")
async def balances_by_lot(principal: BalanceDep, db: DbDep, item_id: str | None = None, location_id: str | None = None, lot_id: str | None = None) -> dict[str, Any]:
    return await _ledger(db, principal).balance_by_lot(item_id=item_id, location_id=location_id, lot_id=lot_id)


@router.get("/inventory/availability")
async def availability(principal: BalanceDep, db: DbDep, item_id: str, location_id: str, inventory_status_id: str | None = None) -> dict[str, Any]:
    return await _ledger(db, principal).availability_summary(item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id)


@router.get("/inventory/in-transit")
async def in_transit(principal: BalanceDep, db: DbDep, item_id: str | None = None, to_location_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    return await _ledger(db, principal).in_transit(item_id=item_id, to_location_id=to_location_id, status=status)


# --------------------------------------------------------------------------- distributions -----
@router.post("/inventory/distributions", status_code=201)
async def create_distribution(payload: dict[str, Any], principal: DistCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_distribution(payload, idempotency_key=key or "")


@router.get("/inventory/distributions")
async def list_distributions(principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("inventory_distribution", status=status)


@router.get("/inventory/distributions/{document_id}")
async def get_distribution(document_id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_document("inventory_distribution", document_id)


@router.post("/inventory/distributions/{document_id}/post")
async def post_distribution(document_id: str, principal: DistPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_distribution(document_id, idempotency_key=key or "")


@router.post("/inventory/distributions/{document_id}/reverse")
async def reverse_distribution(document_id: str, payload: dict[str, Any], principal: TxReverseDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).reverse_document("inventory_distribution", document_id, reason=str(payload.get("reason") or "reversal"), idempotency_key=key or "")


# --------------------------------------------------------------------------- imports ------------
@router.post("/imports", status_code=201)
async def create_import(payload: dict[str, Any], principal: ImportCreateDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_import(payload, idempotency_key=key or "")


@router.get("/imports")
async def list_imports(principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_documents("import_batch", status=status)


@router.get("/imports/{batch_id}")
async def get_import(batch_id: str, principal: BalanceDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).get_import(batch_id)


@router.get("/imports/{batch_id}/rows")
async def get_import_rows(batch_id: str, principal: BalanceDep, db: DbDep, status: str | None = None) -> dict[str, Any]:
    return await _documents(db, principal).import_rows(batch_id, status=status)


@router.post("/imports/{batch_id}/validate")
async def validate_import(batch_id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).validate_import(batch_id)


@router.post("/imports/{batch_id}/post")
async def post_import(batch_id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_import(batch_id, idempotency_key=key or "")


@router.post("/imports/{batch_id}/retry")
async def retry_import(batch_id: str, principal: ImportPostDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).post_import(batch_id, idempotency_key=key or "")


# --------------------------------------------------------------------------- reason codes ------
@router.get("/inventory-reason-codes")
async def list_reason_codes(principal: BalanceDep, db: DbDep, reason_type: str | None = None, active: bool | None = None) -> dict[str, Any]:
    return await _documents(db, principal).list_reason_codes(reason_type=reason_type, active=active)


@router.post("/inventory-reason-codes", status_code=201)
async def create_reason_code(payload: dict[str, Any], principal: ReasonDep, db: DbDep, key: IdemDep = None) -> dict[str, Any]:
    return await _documents(db, principal).create_reason_code(payload, idempotency_key=key or "")


@router.patch("/inventory-reason-codes/{reason_id}")
async def update_reason_code(reason_id: str, payload: dict[str, Any], principal: ReasonDep, db: DbDep) -> dict[str, Any]:
    return await _documents(db, principal).update_reason_code(reason_id, payload)


__all__ = ["router"]
