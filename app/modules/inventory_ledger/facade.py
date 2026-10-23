"""`inventory.ledger@1.0.0` / `inventory.balances@1.0.0` in-process facades.

Other modules post only through :class:`PostingPort` (no direct ledger table writes). Costing is a
dependency *port*: callers pass a costing adapter implementing the FS-05 `CostingPort` methods; the
ledger engine itself never imports FS-05 tables. All methods take the caller's AsyncSession/transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory_ledger.documents import LedgerDocuments
from app.modules.inventory_ledger.service import LedgerService, availability

LEDGER_CONTRACT_VERSION = "1.0.0"
BALANCES_CONTRACT_VERSION = "1.0.0"


class CostingPort(Protocol):
    """FS-05 synchronous costing adapter (optional injection; ledger works without it in unit tests)."""

    async def value_movement(self, movement: Mapping[str, Any], *, transaction_id: str) -> Mapping[str, Any]: ...

    async def assert_postable(self, item_id: str, as_of: Any) -> None: ...


class PostingPort:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | str, *, costing: CostingPort | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        adapter: Any | None = costing
        if adapter is None:
            try:
                from app.modules.inventory_ledger.costing import LedgerCostingAdapter

                adapter = LedgerCostingAdapter(session, tenant_id)
            except Exception:  # noqa: BLE001 - FS-05 unavailable in some deployments/tests
                adapter = None
        self.costing = adapter
        self.ledger = LedgerService(session, tenant_id, costing=adapter)
        self.documents = LedgerDocuments(self.ledger)

    # -- core ---------------------------------------------------------------------------------
    async def post(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return await self.ledger.post(command)

    async def reverse(self, transaction_id: str, *, reason: str, idempotency_key: str) -> dict[str, Any]:
        return await self.ledger.reverse(transaction_id, reason=reason, idempotency_key=idempotency_key)

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        return await self.ledger.get_transaction(transaction_id)

    # -- typed helpers (all 31-type surface; required args in motion payloads) ----------------
    async def _typed(self, posting_type: str, *, transaction_date: Any, source_doc_type: str, source_doc_id: Any,
                     movements: Sequence[Mapping[str, Any]], idempotency_key: str, external_ref: str | None = None) -> dict[str, Any]:
        return await self.post({
            "posting_type": posting_type, "transaction_date": transaction_date,
            "source_doc_type": source_doc_type, "source_doc_id": source_doc_id,
            "external_ref": external_ref, "idempotency_key": idempotency_key, "movements": list(movements),
        })

    async def post_receipt(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-01", **kwargs)

    async def reverse_receipt(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-02", **kwargs)

    async def post_vendor_return(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-03", **kwargs)

    async def post_vendor_bill(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-04", **kwargs)

    async def post_vendor_credit(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-05", **kwargs)

    async def post_variance(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-06", **kwargs)

    async def post_adjustment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-07", **kwargs)

    async def post_worksheet(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-08", **kwargs)

    async def post_transfer(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-09", **kwargs)

    async def post_fulfillment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-12", **kwargs)

    async def post_return(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-13", **kwargs)

    async def post_kit_fulfillment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-14", **kwargs)

    async def post_assembly_build(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-15", **kwargs)

    async def post_assembly_unbuild(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-16", **kwargs)

    async def post_wip_issue(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-17", **kwargs)

    async def post_wip_completion(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-18", **kwargs)

    async def post_wip_close(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-19", **kwargs)

    async def post_count_variance(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-20", **kwargs)

    async def post_consigned_receipt(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-21", **kwargs)

    async def post_consigned_ownership(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-22", **kwargs)

    async def post_consigned_adjustment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-23", **kwargs)

    async def post_revaluation(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-24", **kwargs)

    async def post_cogs_adjustment(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-25", **kwargs)

    async def post_landed_cost(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-26", **kwargs)

    async def post_estimated_landed_cost(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-27", **kwargs)

    async def post_drop_ship(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-28", **kwargs)

    async def post_bin_transfer(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-29", **kwargs)

    async def post_status_change(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-30", **kwargs)

    async def post_import_rows(self, **kwargs: Any) -> dict[str, Any]:
        return await self._typed("T-07", **kwargs)

    # -- commitments / reads ------------------------------------------------------------------
    async def commit(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self.documents.commit(payload, idempotency_key=idempotency_key)

    async def release_commitment(self, commitment_id: str, *, qty: Any = None, reason: str | None = None) -> dict[str, Any]:
        return await self.documents.release_commitment(commitment_id, qty=qty, reason=reason)

    async def get_commitment(self, commitment_id: str) -> dict[str, Any]:
        return await self.documents.get_commitment(commitment_id)

    async def list_availability(self, *, item_id: str, location_id: str, inventory_status_id: str | None = None) -> dict[str, Any]:
        return await self.ledger.availability_summary(item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id)

    async def get_balance(self, *, item_id: str, location_id: str, inventory_status_id: str | None = None) -> dict[str, Any]:
        return await self.list_availability(item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id)

    async def apply_on_order_delta(self, *, item_id: str, location_id: str, delta: Any, source_ref: str | None = None) -> dict[str, Any]:
        item_uuid = uuid.UUID(item_id)
        location_uuid = uuid.UUID(location_id)
        await self.ledger._exec(
            """
            INSERT INTO inventory_ledger.inventory_balance
                (id, tenant_id, item_id, location_id, inventory_status_id, ownership, qty_class, lot_id, serial_id,
                 qty_on_hand, qty_committed, qty_on_order, qty_backordered, value_base, created_at, updated_at, version)
            VALUES (:id, :tenant_id, :item, :location,
                    (SELECT id FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND code = 'AVAILABLE' LIMIT 1),
                    'OWNED', 'ON_HAND', NULL, NULL, 0, 0, :delta, 0, 0, now(), now(), 1)
            ON CONFLICT ON CONSTRAINT uq_inventory_balance_key
            DO UPDATE SET qty_on_order = GREATEST(inventory_ledger.inventory_balance.qty_on_order + EXCLUDED.qty_on_order, 0),
                          updated_at = now(), version = inventory_ledger.inventory_balance.version + 1
            """,
            id=uuid.uuid4(), item=item_uuid, location=location_uuid, delta=Decimal(str(delta)),
        )
        return {"item_id": item_id, "location_id": location_id, "on_order_delta": str(delta), "source_ref": source_ref}


class BalanceReadPort:
    """`inventory.balances@1.0.0` read facade for planning/WMS/reporting consumers."""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | str) -> None:
        self.ledger = LedgerService(session, tenant_id)

    async def balances(self, **filters: Any) -> dict[str, Any]:
        return await self.ledger.balances(**filters)

    async def availability(self, *, item_id: str, location_id: str, inventory_status_id: str | None = None) -> dict[str, Any]:
        return await self.ledger.availability_summary(item_id=item_id, location_id=location_id, inventory_status_id=inventory_status_id)

    async def by_bin(self, **filters: Any) -> dict[str, Any]:
        return await self.ledger.balance_by_bin(**filters)

    async def by_lot(self, **filters: Any) -> dict[str, Any]:
        return await self.ledger.balance_by_lot(**filters)

    async def in_transit(self, **filters: Any) -> dict[str, Any]:
        return await self.ledger.in_transit(**filters)


async def get_balance(session: AsyncSession, tenant_id: uuid.UUID | str, *, item_id: str, location_id: str) -> dict[str, Any]:
    return await BalanceReadPort(session, tenant_id).availability(item_id=item_id, location_id=location_id)


__all__ = [
    "BALANCES_CONTRACT_VERSION",
    "LEDGER_CONTRACT_VERSION",
    "BalanceReadPort",
    "CostingPort",
    "PostingPort",
    "availability",
    "get_balance",
]
