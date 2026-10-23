"""`procurement.p2p@1.0.0` in-process facade for dependant feature sets (FS-09/10/11/12/13/14/15).

The facade participates in the caller's AsyncSession transaction (never commits, never uses HTTP)
and exposes the frozen read/draft surfaces declared in ``feature-sets/FS-08.md`` §3:

``get_vendor``, ``create_draft_po`` (drafts only for FS-10), ``get_open_po_supply``, ``get_po``,
``get_receipt``, ``list_receipts_for_po``, ``create_vendor_bill_for_consignment`` (FS-13 atomic bill
creation in the caller's transaction, idempotent on the source transfer id) and
``get_vendor_purchase_history``. Event types and job names are exported for contract tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.procurement.service import ProcurementService

PROCUREMENT_CONTRACT_VERSION = "1.0.0"

EVENT_TYPES: tuple[str, ...] = (
    "po.created",
    "po.approved",
    "po.receipted",
    "po.billed",
    "po.closed",
    "inventory.receipt.posted",
    "inventory.receipt.reversed",
    "vendor_bill.created",
    "vendor_bill.posted",
    "vendor_bill.variance_created",
    "vendor_bill.variance_posted",
    "vendor_credit.posted",
    "vendor_return.shipped",
    "vendor_return.credited",
    "dropship.fulfilled",
)

JOB_NAMES: tuple[str, ...] = (
    "procurement.match.run",
    "procurement.po.close_overdue",
    "procurement.receipt.reconcile",
    "procurement.partition.maintenance",
)

OPEN_PO_STATUSES = ("APPROVED", "PENDING_RECEIPT", "PARTIALLY_RECEIVED")


class ProcurementFacade:
    """Frozen `procurement.p2p@1.0.0` facade bound to one caller transaction/session."""

    def __init__(self, session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.service = ProcurementService(session, tenant_id)

    # -- vendor / history ---------------------------------------------------------------------
    async def get_vendor(self, vendor_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_vendor(vendor_id)

    async def get_vendor_purchase_history(
        self, vendor_id: str | uuid.UUID, filters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        options = dict(filters or {})
        return await self.service.get_vendor_purchase_history(
            vendor_id,
            date_from=options.get("date_from"),
            date_to=options.get("date_to"),
            item_id=options.get("item_id"),
            limit=int(options.get("limit", 100)),
        )

    # -- purchase orders ----------------------------------------------------------------------
    async def get_po(self, po_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_purchase_order(po_id)

    async def create_draft_po(
        self,
        lines: Sequence[Mapping[str, Any]],
        vendor: Mapping[str, Any] | str | uuid.UUID,
        opts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a DRAFT purchase order for planning consumers (FS-10); drafts never post."""
        options = dict(opts or {})
        vendor_id = vendor.get("id") if isinstance(vendor, Mapping) else vendor
        payload = {
            "vendor_id": str(vendor_id),
            "po_type": str(options.get("po_type") or "STANDARD"),
            "transaction_date": options.get("transaction_date") or date.today(),
            "ship_to_location_id": options.get("ship_to_location_id"),
            "expected_receipt_date": options.get("expected_receipt_date"),
            "currency_code": options.get("currency_code"),
            "memo": options.get("memo") or "created by planning facade",
            "lines": [dict(line) for line in lines],
        }
        po = await self.service.create_purchase_order(payload, actor_id=options.get("actor_id"))
        return {
            "purchase_order_id": po["id"],
            "document_number": po["document_number"],
            "status": po["status"],
            "total": po["total"],
        }

    async def get_open_po_supply(
        self, item_id: str, location_id: str | None = None, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, Any]:
        clauses = ["po.tenant_id = :tenant_id", "pol.item_id = :item", f"po.status IN {OPEN_PO_STATUSES!r}"]
        params: dict[str, Any] = {"item": uuid.UUID(str(item_id))}
        if location_id:
            clauses.append(
                "(pol.ship_to_location_id IS NULL OR pol.ship_to_location_id = :location OR po.ship_to_location_id = :location)"
            )
            params["location"] = uuid.UUID(str(location_id))
        if date_from:
            clauses.append("coalesce(pol.expected_receipt_date, po.expected_receipt_date) >= :date_from")
            params["date_from"] = date_from
        if date_to:
            clauses.append("coalesce(pol.expected_receipt_date, po.expected_receipt_date) <= :date_to")
            params["date_to"] = date_to
        rows = await self.service._all(
            f"""
            SELECT pol.id AS purchase_order_line_id, po.id AS purchase_order_id, po.document_number, po.vendor_id,
                   po.status, coalesce(pol.expected_receipt_date, po.expected_receipt_date) AS expected_receipt_date,
                   pol.qty_ordered, pol.qty_received, pol.qty_cancelled,
                   GREATEST(pol.qty_ordered - pol.qty_received - pol.qty_cancelled, 0) AS open_qty
              FROM procurement.purchase_order po
              JOIN procurement.purchase_order_line pol ON pol.tenant_id = po.tenant_id AND pol.purchase_order_id = po.id
             WHERE {" AND ".join(clauses)}
             ORDER BY expected_receipt_date NULLS LAST, po.document_number
            """,
            **params,
        )
        total = sum((Decimal(str(row["open_qty"])) for row in rows), Decimal("0"))
        return {"item_id": str(item_id), "location_id": location_id, "items": rows, "total_open_qty": str(total)}

    async def create_po_release(self, blanket_po_id: str | uuid.UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.service.create_po_release(blanket_po_id, payload)

    # -- receipts -----------------------------------------------------------------------------
    async def get_receipt(self, receipt_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_receipt(receipt_id)

    async def list_receipts_for_po(self, po_id: str | uuid.UUID) -> list[dict[str, Any]]:
        result = await self.service.list_receipts(purchase_order_id=str(po_id))
        return result["items"]

    # -- consignment bill (FS-13) -------------------------------------------------------------
    async def create_vendor_bill_for_consignment(
        self,
        *,
        source_transfer_id: str,
        vendor_id: str,
        lines: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a consignment vendor bill atomically in the caller's transaction.

        Idempotent on ``source_transfer_id``: a replay returns the bill created for the same transfer
        (``vendor_bill.created`` is emitted in the same transaction; DM-PROC / FS-13 C-7).
        """
        opts = dict(options or {})
        endpoint_id = f"POST /facade/procurement/consignment-bills/{source_transfer_id}"
        key = f"consignment:{source_transfer_id}"
        existing = await self.service._one(
            "SELECT * FROM procurement.vendor_bill WHERE tenant_id = :tenant_id AND vendor_id = :vendor AND memo = :memo ORDER BY created_at DESC LIMIT 1",
            vendor=uuid.UUID(str(vendor_id)),
            memo=f"consignment:{source_transfer_id}",
        )
        if existing is not None:
            return await self.service.get_bill(existing["id"])
        replay = await self.service._idem_begin(
            endpoint_id, key, {"source_transfer_id": source_transfer_id, "vendor_id": vendor_id}
        )
        if replay:
            return replay["replay"]
        bill = await self.service.create_bill(
            {
                "vendor_id": vendor_id,
                "vendor_bill_number": opts.get("vendor_bill_number") or f"CONS-{str(source_transfer_id)[:24]}",
                "currency_code": opts.get("currency_code"),
                "exchange_rate": opts.get("exchange_rate", 1),
                "transaction_date": opts.get("transaction_date") or date.today(),
                "memo": f"consignment:{source_transfer_id}",
                "lines": [dict(line) for line in lines],
            },
            actor_id=opts.get("actor_id"),
        )
        await self.service._idem_complete(endpoint_id, key, bill, bill["id"], 201)
        return bill


__all__ = [
    "EVENT_TYPES",
    "JOB_NAMES",
    "OPEN_PO_STATUSES",
    "PROCUREMENT_CONTRACT_VERSION",
    "ProcurementFacade",
]
