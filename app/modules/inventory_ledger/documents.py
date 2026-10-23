"""FS-04 owned commands/documents: commitments, adjustments, worksheets, transfers, transfer orders,
distributions, imports and reason codes (all inside the caller's transaction; no cross-context writes)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.outbox import emit
from app.modules.inventory_ledger.errors import (
    InsufficientAvailable,
    InvalidStateTransition,
    TransactionNotFound,
    ValidationFailed,
    ledger_error,
)
from app.modules.inventory_ledger.service import (
    QTY_ZERO,
    LedgerService,
    _as_date,
    _as_uuid,
    _jsonable,
    quantize,
    utc_now,
)


class LedgerDocuments:
    def __init__(self, ledger: LedgerService) -> None:
        self.ledger = ledger
        self.session = ledger.session
        self.tenant_id = ledger.tenant_id

    # -- generic helpers ----------------------------------------------------------------------
    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        return await self.ledger._one(sql, **params)

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return await self.ledger._all(sql, **params)

    async def _exec(self, sql: str, **params: Any):
        return await self.ledger._exec(sql, **params)

    async def _emit(self, event_type: str, *, aggregate_id: uuid.UUID | str, payload: Mapping[str, Any]) -> None:
        await emit(
            self.session, event_type=event_type, tenant_id=str(self.tenant_id),
            aggregate_type="inventory_document", aggregate_id=str(aggregate_id), payload=_jsonable(dict(payload)),
        )

    async def _insert_balance_committed(self, *, item_id: Any, location_id: Any, status_id: Any, ownership: str, delta: Decimal) -> None:
        updated = await self._exec(
            """
            UPDATE inventory_ledger.inventory_balance
            SET qty_committed = qty_committed + :delta, updated_at = now(), version = version + 1
            WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location
              AND inventory_status_id = :status AND ownership = :ownership AND qty_class = 'ON_HAND'
              AND lot_id IS NULL AND serial_id IS NULL
            """,
            item=item_id, location=location_id, status=status_id, ownership=ownership, delta=delta,
        )
        if not int(getattr(updated, "rowcount", 0) or 0):
            if delta < 0:
                raise ledger_error("commitment position is missing", code="INVALID_STATE_TRANSITION", http_status=409)
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_balance
                    (id, tenant_id, item_id, location_id, inventory_status_id, ownership, qty_class, lot_id, serial_id,
                     qty_on_hand, qty_committed, qty_on_order, qty_backordered, value_base, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :item, :location, :status, :ownership, 'ON_HAND', NULL, NULL, 0, :delta, 0, 0, 0, now(), now(), 1)
                """,
                id=uuid.uuid4(), item=item_id, location=location_id, status=status_id, ownership=ownership, delta=delta,
            )

    # -- commitments --------------------------------------------------------------------------
    async def commit(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        item_id = _as_uuid(payload.get("item_id"), field="item_id")
        location_id = _as_uuid(payload.get("location_id"), field="location_id")
        status_id = _as_uuid(payload.get("inventory_status_id") or await self._default_status_id(), field="inventory_status_id")
        ownership = str(payload.get("ownership") or "OWNED").upper()
        qty = quantize(payload.get("qty"))
        if qty <= 0:
            raise ValidationFailed("qty must be > 0")
        source_type = str(payload.get("source_type") or "OTHER").upper()
        source_ref = str(payload.get("source_line_ref") or "")
        if not source_ref:
            raise ValidationFailed("source_line_ref is required")
        status = await self._one("SELECT is_committable FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND id = :id", id=status_id)
        if status is not None and not status["is_committable"]:
            raise ledger_error("inventory status is not committable", code="STATUS_NOT_COMMITTABLE", http_status=409)
        available = (await self.ledger.availability_summary(item_id=str(item_id), location_id=str(location_id), inventory_status_id=str(status_id)))["available"]
        if Decimal(str(available)) < qty:
            raise InsufficientAvailable("insufficient available quantity")
        commitment_id = uuid.uuid4()
        try:
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_commitment
                    (id, tenant_id, source_type, source_line_ref, external_ref, item_id, location_id, inventory_status_id,
                     ownership, qty_committed, qty_fulfilled, qty_backordered, status, priority, required_by_date,
                     committed_at, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :source_type, :source_ref, :external_ref, :item, :location, :status, :ownership,
                        :qty, 0, 0, 'ACTIVE', :priority, :required_by, now(), now(), now(), 1)
                """,
                id=commitment_id, source_type=source_type, source_ref=source_ref,
                external_ref=payload.get("external_ref"), item=item_id, location=location_id, status=status_id,
                ownership=ownership, qty=qty, priority=int(payload.get("priority", 100)),
                required_by=_as_date(payload["required_by_date"]) if payload.get("required_by_date") else None,
            )
        except IntegrityError as exc:
            existing = await self._one(
                "SELECT * FROM inventory_ledger.inventory_commitment WHERE tenant_id = :tenant_id AND source_type = :source AND source_line_ref = :ref AND status = 'ACTIVE'",
                source=source_type, ref=source_ref,
            )
            if existing is not None:
                return _jsonable(existing)
            raise ledger_error("commitment source already active", code="INVALID_STATE_TRANSITION", http_status=409) from exc
        await self._insert_balance_committed(item_id=item_id, location_id=location_id, status_id=status_id, ownership=ownership, delta=qty)
        await self._emit("inventory.committed.changed", aggregate_id=commitment_id, payload={
            "commitment_id": str(commitment_id), "item_id": str(item_id), "location_id": str(location_id),
            "qty_committed": _jsonable(qty), "status": "ACTIVE",
        })
        return {"commitment_id": str(commitment_id), "status": "ACTIVE", "qty_committed": _jsonable(qty), "qty_backordered": "0"}

    async def release_commitment(self, commitment_id: uuid.UUID | str, *, qty: Any = None, reason: str | None = None) -> dict[str, Any]:
        commitment_uuid = _as_uuid(commitment_id, field="commitment_id")
        commitment = await self._one("SELECT * FROM inventory_ledger.inventory_commitment WHERE tenant_id = :tenant_id AND id = :id", id=commitment_uuid)
        if commitment is None:
            raise TransactionNotFound("commitment not found")
        current = Decimal(str(commitment["qty_committed"]))
        release_qty = current if qty is None else quantize(qty)
        if release_qty <= 0 or release_qty > current:
            raise ValidationFailed("invalid release quantity")
        remaining = current - release_qty
        status = "RELEASED" if remaining == 0 else "ACTIVE"
        # Snapshot tables keep the promised qty for audit; only the projection balance is decremented.
        await self._exec(
            "UPDATE inventory_ledger.inventory_commitment SET released_qty = released_qty + :released, status = :status, released_at = CASE WHEN :status = 'RELEASED' THEN now() ELSE released_at END, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=commitment_uuid, released=release_qty, status=status,
        )
        await self._insert_balance_committed(
            item_id=commitment["item_id"], location_id=commitment["location_id"], status_id=commitment["inventory_status_id"],
            ownership=str(commitment["ownership"]), delta=-release_qty,
        )
        await self._emit("inventory.committed.changed", aggregate_id=commitment_uuid, payload={
            "commitment_id": str(commitment_uuid), "qty_committed": _jsonable(remaining), "released_qty": _jsonable(release_qty), "status": status,
        })
        return {"commitment_id": str(commitment_uuid), "status": status, "qty_committed": _jsonable(remaining), "released_qty": _jsonable(release_qty)}

    async def list_commitments(self, *, item_id: str | None = None, location_id: str | None = None, source_type: str | None = None,
                               status: str | None = None, external_ref: str | None = None, limit: int = 100) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        for column, value in (("item_id", item_id), ("location_id", location_id)):
            if value:
                clauses.append(f"{column} = :{column}")
                params[column] = _as_uuid(value, field=column)
        if source_type:
            clauses.append("source_type = :source_type")
            params["source_type"] = source_type.upper()
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        if external_ref:
            clauses.append("external_ref = :external_ref")
            params["external_ref"] = external_ref
        rows = await self._all("SELECT * FROM inventory_ledger.inventory_commitment WHERE " + " AND ".join(clauses) + " ORDER BY priority, required_by_date NULLS LAST, committed_at LIMIT :limit", **params)
        return {"items": [_jsonable(row) for row in rows], "next_cursor": None}

    async def reallocate(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        item_id = _as_uuid(payload.get("item_id"), field="item_id")
        location_id = _as_uuid(payload.get("location_id"), field="location_id")
        rows = await self._all(
            "SELECT id FROM inventory_ledger.inventory_commitment WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location AND status = 'ACTIVE' ORDER BY priority, required_by_date NULLS LAST, committed_at",
            item=item_id, location=location_id,
        )
        await self._emit("inventory.committed.changed", aggregate_id=item_id, payload={"item_id": str(item_id), "location_id": str(location_id), "reallocated": len(rows)})
        return {"moved": [str(row["id"]) for row in rows], "status": "OK"}

    async def get_commitment(self, commitment_id: uuid.UUID | str) -> dict[str, Any]:
        row = await self._one("SELECT * FROM inventory_ledger.inventory_commitment WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(commitment_id, field="commitment_id"))
        if row is None:
            raise TransactionNotFound("commitment not found")
        return _jsonable(row)

    async def _default_status_id(self) -> uuid.UUID:
        row = await self._one("SELECT id FROM traceability.inventory_status WHERE tenant_id = :tenant_id AND code = 'AVAILABLE' AND is_active ORDER BY is_default DESC LIMIT 1")
        if row is None:
            raise ValidationFailed("inventory_status_id is required")
        return row["id"]

    # -- document state helpers ---------------------------------------------------------------
    async def _transition(self, table: str, document_id: uuid.UUID, *, from_status: Sequence[str], to_status: str) -> None:
        row = await self._one(f"SELECT status FROM inventory_ledger.{table} WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=document_id)
        if row is None:
            raise TransactionNotFound("document not found")
        if str(row["status"]) not in from_status:
            raise InvalidStateTransition(f"document is {row['status']}, expected one of {list(from_status)}")
        await self._exec(
            f"UPDATE inventory_ledger.{table} SET status = :status, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=document_id, status=to_status,
        )

    async def _doc_lines(self, table: str, header_col: str, document_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self._all(
            f"SELECT * FROM inventory_ledger.{table} WHERE tenant_id = :tenant_id AND {header_col} = :id ORDER BY line_number",
            id=document_id,
        )

    # -- adjustments --------------------------------------------------------------------------
    async def create_adjustment(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        adjustment_id = uuid.uuid4()
        document_number = str(payload.get("document_number") or f"ADJ-{uuid.uuid4().hex[:12].upper()}")
        await self._exec(
            "INSERT INTO inventory_ledger.inventory_adjustment (id, tenant_id, document_number, status, transaction_date, memo, reason_code_id, adjustment_source, created_at, updated_at, created_by, version) "
            "VALUES (:id, :tenant_id, :number, 'DRAFT', :date, :memo, :reason, :source, now(), now(), :actor, 1)",
            id=adjustment_id, number=document_number, date=_as_date(payload["transaction_date"]) if payload.get("transaction_date") else None, memo=payload.get("memo"),
            reason=payload.get("reason_code_id"), source=str(payload.get("adjustment_source") or "MANUAL"),
            actor=payload.get("actor_id"),
        )
        for index, line in enumerate(payload.get("lines") or [], start=1):
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_adjustment_line (id, tenant_id, inventory_adjustment_id, line_number, item_id, location_id, inventory_status_id, ownership, bin_id, lot_id, serial_id, uom_id, qty_delta, unit_cost, value_delta, reason_code_id, memo, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :header, :line, :item, :location, :status, :ownership, :bin, :lot, :serial, :uom, :qty, :cost, :value, :reason, :memo, now(), now(), 1)",
                id=uuid.uuid4(), header=adjustment_id, line=index, item=_as_uuid(line.get("item_id"), field="item_id"),
                location=_as_uuid(line.get("location_id"), field="location_id"),
                status=_as_uuid(line.get("inventory_status_id") or await self._default_status_id(), field="inventory_status_id"),
                ownership=str(line.get("ownership") or "OWNED"), bin=_as_uuid(line["bin_id"], field="bin_id") if line.get("bin_id") else None,
                lot=_as_uuid(line["lot_id"], field="lot_id") if line.get("lot_id") else None,
                serial=_as_uuid(line["serial_id"], field="serial_id") if line.get("serial_id") else None,
                uom=_as_uuid(line["uom_id"], field="uom_id") if line.get("uom_id") else None,
                qty=quantize(line.get("qty_delta")), cost=quantize(line["unit_cost"]) if line.get("unit_cost") is not None else None,
                value=quantize(line["value_delta"]) if line.get("value_delta") is not None else None,
                reason=_as_uuid(line["reason_code_id"], field="reason_code_id") if line.get("reason_code_id") else None,
                memo=line.get("memo"),
            )
        return await self.get_document("inventory_adjustment", adjustment_id)

    async def get_document(self, table: str, document_id: uuid.UUID | str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="id")
        header = await self._one(f"SELECT * FROM inventory_ledger.{table} WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid)
        if header is None:
            raise TransactionNotFound("document not found")
        line_table = f"{table}_line"
        if table == "inventory_transfer":
            line_table = "inventory_transfer_line"
        elif table == "transfer_order":
            line_table = "transfer_order_line"
        lines = await self._doc_lines(line_table, table + "_id", document_uuid)
        details = _jsonable(header)
        details["lines"] = [_jsonable(line) for line in lines]
        return details

    async def list_documents(self, table: str, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        rows = await self._all(f"SELECT * FROM inventory_ledger.{table} WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT :limit", **params)
        return {"items": [_jsonable(row) for row in rows], "next_cursor": None}

    async def submit_document(self, table: str, document_id: uuid.UUID | str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="id")
        await self._exec(
            f"UPDATE inventory_ledger.{table} SET status = 'PENDING_APPROVAL', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id AND status = 'DRAFT'",
            id=document_uuid,
        )
        return await self.get_document(table, document_uuid)

    async def post_adjustment(self, document_id: uuid.UUID | str, *, idempotency_key: str, transaction_date: str | None = None) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="adjustment_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_adjustment WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=document_uuid)
        if header is None:
            raise TransactionNotFound("adjustment not found")
        if header["posted_transaction_id"] is not None:
            txn = await self.ledger.get_transaction(header["posted_transaction_id"])
            return {"transaction_id": str(txn["id"]), "ledger_sequence": txn["ledger_sequence"], "warnings": [], "replayed": True}
        if str(header["status"]) not in {"APPROVED", "PENDING_APPROVAL", "DRAFT"}:
            raise InvalidStateTransition("adjustment cannot be posted from its current state")
        lines = await self._doc_lines("inventory_adjustment_line", "inventory_adjustment_id", document_uuid)
        movements = [
            {
                "item_id": line["item_id"], "location_id": line["location_id"], "inventory_status_id": line["inventory_status_id"],
                "ownership": line["ownership"], "bin_id": line["bin_id"], "lot_id": line["lot_id"], "serial_id": line["serial_id"],
                "uom_id": line["uom_id"], "qty": line["qty_delta"], "unit_cost": line["unit_cost"],
                "value_base": line["value_delta"] if line["value_delta"] is not None else (Decimal(str(line["qty_delta"])) * Decimal(str(line["unit_cost"] or 0))),
                "movement_type": "ADJUSTMENT", "reason_code_id": line["reason_code_id"],
                "source_line_ref": str(line["line_number"]),
            }
            for line in lines
        ]
        command = {
            "posting_type": "T-07", "transaction_date": transaction_date or header["transaction_date"] or utc_now().date().isoformat(),
            "source_doc_type": "INVENTORY_ADJUSTMENT", "source_doc_id": document_uuid, "idempotency_key": idempotency_key,
            "memo": header.get("memo"), "movements": movements,
        }
        result = await self.ledger.post(command)
        await self._exec(
            "UPDATE inventory_ledger.inventory_adjustment SET status = 'POSTED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=document_uuid, txn=result["transaction_id"],
        )
        await self._emit("inventory.adjusted", aggregate_id=document_uuid, payload={"adjustment_id": str(document_uuid), "transaction_id": result["transaction_id"]})
        return result

    async def void_document(self, table: str, document_id: uuid.UUID | str, *, reason: str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="id")
        header = await self._one(f"SELECT status, posted_transaction_id FROM inventory_ledger.{table} WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid)
        if header is None:
            raise TransactionNotFound("document not found")
        if header["posted_transaction_id"] is not None:
            raise InvalidStateTransition("posted documents cannot be voided; reverse instead")
        await self._exec(
            f"UPDATE inventory_ledger.{table} SET status = 'VOID', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=document_uuid,
        )
        return {"id": str(document_uuid), "status": "VOID"}

    async def reverse_document(self, table: str, document_id: uuid.UUID | str, *, reason: str, idempotency_key: str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="id")
        header = await self._one(f"SELECT posted_transaction_id FROM inventory_ledger.{table} WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid)
        if header is None:
            raise TransactionNotFound("document not found")
        if header["posted_transaction_id"] is None:
            raise InvalidStateTransition("document has not been posted")
        reversal = await self.ledger.reverse(header["posted_transaction_id"], reason=reason, idempotency_key=idempotency_key)
        await self._exec(
            f"UPDATE inventory_ledger.{table} SET status = 'REVERSED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id",
            id=document_uuid,
        )
        return {"reversal_transaction_id": reversal["transaction_id"], "ledger_sequence": reversal["ledger_sequence"]}

    # -- worksheets ---------------------------------------------------------------------------
    async def create_worksheet(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        worksheet_id = uuid.uuid4()
        await self._exec(
            "INSERT INTO inventory_ledger.inventory_worksheet (id, tenant_id, document_number, status, transaction_date, memo, worksheet_type, count_date, created_at, updated_at, version) "
            "VALUES (:id, :tenant_id, :number, 'DRAFT', :date, :memo, 'ABSOLUTE', :count_date, now(), now(), 1)",
            id=worksheet_id, number=str(payload.get("document_number") or f"WS-{uuid.uuid4().hex[:12].upper()}"),
            date=_as_date(payload["transaction_date"]) if payload.get("transaction_date") else None, memo=payload.get("memo"), count_date=_as_date(payload["count_date"]) if payload.get("count_date") else None,
        )
        for index, line in enumerate(payload.get("lines") or [], start=1):
            item_id = _as_uuid(line.get("item_id"), field="item_id")
            location_id = _as_uuid(line.get("location_id"), field="location_id")
            status_id = _as_uuid(line.get("inventory_status_id") or await self._default_status_id(), field="inventory_status_id")
            system_qty = await self._system_qty(item_id, location_id, status_id)
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_worksheet_line (id, tenant_id, inventory_worksheet_id, line_number, item_id, location_id, inventory_status_id, bin_id, lot_id, serial_id, system_qty, new_qty, variance_qty, unit_cost, reason_code_id, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :header, :line, :item, :location, :status, :bin, :lot, :serial, :system, :new_qty, :new_qty - :system, :cost, :reason, now(), now(), 1)",
                id=uuid.uuid4(), header=worksheet_id, line=index, item=item_id, location=location_id, status=status_id,
                bin=_as_uuid(line["bin_id"], field="bin_id") if line.get("bin_id") else None,
                lot=_as_uuid(line["lot_id"], field="lot_id") if line.get("lot_id") else None,
                serial=_as_uuid(line["serial_id"], field="serial_id") if line.get("serial_id") else None,
                system=system_qty, new_qty=quantize(line.get("new_qty")),
                cost=quantize(line["unit_cost"]) if line.get("unit_cost") is not None else None,
                reason=_as_uuid(line["reason_code_id"], field="reason_code_id") if line.get("reason_code_id") else None,
            )
        return await self.get_document("inventory_worksheet", worksheet_id)

    async def _system_qty(self, item_id: uuid.UUID, location_id: uuid.UUID, status_id: uuid.UUID) -> Decimal:
        row = await self._one(
            "SELECT qty_on_hand FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location AND inventory_status_id = :status AND ownership = 'OWNED' AND qty_class = 'ON_HAND' AND lot_id IS NULL AND serial_id IS NULL",
            item=item_id, location=location_id, status=status_id,
        )
        return Decimal(str(row["qty_on_hand"])) if row else QTY_ZERO

    async def post_worksheet(self, document_id: uuid.UUID | str, *, idempotency_key: str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="worksheet_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_worksheet WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=document_uuid)
        if header is None:
            raise TransactionNotFound("worksheet not found")
        if header["posted_transaction_id"] is not None:
            txn = await self.ledger.get_transaction(header["posted_transaction_id"])
            return {"transaction_id": str(txn["id"]), "ledger_sequence": txn["ledger_sequence"], "warnings": [], "replayed": True}
        lines = await self._doc_lines("inventory_worksheet_line", "inventory_worksheet_id", document_uuid)
        movements = []
        for line in lines:
            system = await self._system_qty(line["item_id"], line["location_id"], line["inventory_status_id"])
            variance = Decimal(str(line["new_qty"])) - system
            if variance == 0:
                continue
            movements.append({
                "item_id": line["item_id"], "location_id": line["location_id"], "inventory_status_id": line["inventory_status_id"],
                "ownership": "OWNED", "bin_id": line["bin_id"], "lot_id": line["lot_id"], "serial_id": line["serial_id"],
                "qty": variance, "movement_type": "WORKSHEET", "reason_code_id": line["reason_code_id"],
            })
        if not movements:
            raise ValidationFailed("worksheet has no variance to post")
        result = await self.ledger.post({
            "posting_type": "T-08", "transaction_date": header["transaction_date"] or utc_now().date().isoformat(),
            "source_doc_type": "INVENTORY_WORKSHEET", "source_doc_id": document_uuid, "idempotency_key": idempotency_key,
            "movements": movements,
        })
        await self._exec("UPDATE inventory_ledger.inventory_worksheet SET status = 'POSTED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid, txn=result["transaction_id"])
        return result

    # -- one-step transfers -------------------------------------------------------------------
    async def create_transfer(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        transfer_id = uuid.uuid4()
        await self._exec(
            "INSERT INTO inventory_ledger.inventory_transfer (id, tenant_id, document_number, status, transaction_date, memo, from_location_id, to_location_id, created_at, updated_at, version) "
            "VALUES (:id, :tenant_id, :number, 'DRAFT', :date, :memo, :from_location, :to_location, now(), now(), 1)",
            id=transfer_id, number=str(payload.get("document_number") or f"TRF-{uuid.uuid4().hex[:12].upper()}"),
            date=_as_date(payload["transaction_date"]) if payload.get("transaction_date") else None, memo=payload.get("memo"),
            from_location=_as_uuid(payload.get("from_location_id"), field="from_location_id"),
            to_location=_as_uuid(payload.get("to_location_id"), field="to_location_id"),
        )
        for index, line in enumerate(payload.get("lines") or [], start=1):
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_transfer_line (id, tenant_id, inventory_transfer_id, line_number, item_id, uom_id, qty, bin_from_id, bin_to_id, lot_id, serial_ids, inventory_status_id, ownership, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :header, :line, :item, :uom, :qty, :bin_from, :bin_to, :lot, :serials, :status, :ownership, now(), now(), 1)",
                id=uuid.uuid4(), header=transfer_id, line=index, item=_as_uuid(line.get("item_id"), field="item_id"),
                uom=_as_uuid(line["uom_id"], field="uom_id") if line.get("uom_id") else None, qty=quantize(line.get("qty")),
                bin_from=_as_uuid(line["bin_from_id"], field="bin_from_id") if line.get("bin_from_id") else None,
                bin_to=_as_uuid(line["bin_to_id"], field="bin_to_id") if line.get("bin_to_id") else None,
                lot=_as_uuid(line["lot_id"], field="lot_id") if line.get("lot_id") else None,
                serials=[_as_uuid(value, field="serial_id") for value in line.get("serial_ids") or []] or None,
                status=_as_uuid(line["inventory_status_id"], field="inventory_status_id") if line.get("inventory_status_id") else None,
                ownership=str(line.get("ownership") or "OWNED"),
            )
        return await self.get_document("inventory_transfer", transfer_id)

    async def post_transfer(self, document_id: uuid.UUID | str, *, idempotency_key: str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="transfer_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_transfer WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=document_uuid)
        if header is None:
            raise TransactionNotFound("transfer not found")
        if header["posted_transaction_id"] is not None:
            txn = await self.ledger.get_transaction(header["posted_transaction_id"])
            return {"transaction_id": str(txn["id"]), "ledger_sequence": txn["ledger_sequence"], "warnings": [], "replayed": True}
        lines = await self._doc_lines("inventory_transfer_line", "inventory_transfer_id", document_uuid)
        status_id = await self._default_status_id()
        movements = []
        for line in lines:
            qty = Decimal(str(line["qty"]))
            status = line["inventory_status_id"] or status_id
            movements.append({
                "item_id": line["item_id"], "location_id": header["from_location_id"], "inventory_status_id": status,
                "ownership": line["ownership"], "bin_id": line["bin_from_id"], "lot_id": line["lot_id"],
                "qty": -qty, "movement_type": "TRANSFER", "source_line_ref": str(line["line_number"]),
            })
            movements.append({
                "item_id": line["item_id"], "location_id": header["to_location_id"], "inventory_status_id": status,
                "ownership": line["ownership"], "bin_id": line["bin_to_id"], "lot_id": line["lot_id"],
                "qty": qty, "movement_type": "TRANSFER", "source_line_ref": str(line["line_number"]),
            })
        result = await self.ledger.post({
            "posting_type": "T-09", "transaction_date": header["transaction_date"] or utc_now().date().isoformat(),
            "source_doc_type": "INVENTORY_TRANSFER", "source_doc_id": document_uuid, "idempotency_key": idempotency_key,
            "movements": movements,
        })
        await self._exec("UPDATE inventory_ledger.inventory_transfer SET status = 'POSTED', posted_transaction_id = :txn, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid, txn=result["transaction_id"])
        return result

    # -- transfer orders ----------------------------------------------------------------------
    async def create_transfer_order(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        order_id = uuid.uuid4()
        await self._exec(
            "INSERT INTO inventory_ledger.transfer_order (id, tenant_id, document_number, status, transaction_date, memo, from_location_id, to_location_id, in_transit_ownership, transfer_price, created_at, updated_at, version) "
            "VALUES (:id, :tenant_id, :number, 'DRAFT', :date, :memo, :from_location, :to_location, :ownership, :price, now(), now(), 1)",
            id=order_id, number=str(payload.get("document_number") or f"TO-{uuid.uuid4().hex[:12].upper()}"),
            date=_as_date(payload["transaction_date"]) if payload.get("transaction_date") else None, memo=payload.get("memo"),
            from_location=_as_uuid(payload.get("from_location_id"), field="from_location_id"),
            to_location=_as_uuid(payload.get("to_location_id"), field="to_location_id"),
            ownership=str(payload.get("in_transit_ownership") or "SOURCE"),
            price=quantize(payload["transfer_price"]) if payload.get("transfer_price") is not None else None,
        )
        for index, line in enumerate(payload.get("lines") or [], start=1):
            await self._exec(
                "INSERT INTO inventory_ledger.transfer_order_line (id, tenant_id, transfer_order_id, line_number, item_id, uom_id, qty_requested, bin_from_id, bin_to_id, lot_id, serial_ids, required_by_date, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :header, :line, :item, :uom, :qty, :bin_from, :bin_to, :lot, :serials, :required, now(), now(), 1)",
                id=uuid.uuid4(), header=order_id, line=index, item=_as_uuid(line.get("item_id"), field="item_id"),
                uom=_as_uuid(line["uom_id"], field="uom_id") if line.get("uom_id") else None,
                qty=quantize(line.get("qty_requested")), bin_from=_as_uuid(line["bin_from_id"], field="bin_from_id") if line.get("bin_from_id") else None,
                bin_to=_as_uuid(line["bin_to_id"], field="bin_to_id") if line.get("bin_to_id") else None,
                lot=_as_uuid(line["lot_id"], field="lot_id") if line.get("lot_id") else None,
                serials=[_as_uuid(value, field="serial_id") for value in line.get("serial_ids") or []] or None,
                required=_as_date(line["required_by_date"]) if line.get("required_by_date") else None,
            )
        return await self.get_document("transfer_order", order_id)

    async def submit_transfer_order(self, order_id: uuid.UUID | str) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        await self._exec("UPDATE inventory_ledger.transfer_order SET status = 'PENDING_APPROVAL', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id AND status = 'DRAFT'", id=order_uuid)
        return await self.get_document("transfer_order", order_uuid)

    async def approve_transfer_order(self, order_id: uuid.UUID | str) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        header = await self._one("SELECT * FROM inventory_ledger.transfer_order WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=order_uuid)
        if header is None:
            raise TransactionNotFound("transfer order not found")
        if header["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition("transfer order cannot be approved from its current state")
        lines = await self._doc_lines("transfer_order_line", "transfer_order_id", order_uuid)
        status_id = await self._default_status_id()
        commitment_ids: list[str] = []
        for line in lines:
            commitment = await self.commit(
                {"source_type": "TRANSFER_ORDER", "source_line_ref": f"{order_uuid}:{line['line_number']}",
                 "item_id": line["item_id"], "location_id": header["from_location_id"], "inventory_status_id": status_id,
                 "qty": line["qty_requested"]},
                idempotency_key=f"to-commit:{order_uuid}:{line['line_number']}",
            )
            commitment_ids.append(commitment["commitment_id"])
            await self._exec("UPDATE inventory_ledger.transfer_order_line SET qty_committed = qty_requested, updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=line["id"])
        await self._exec("UPDATE inventory_ledger.transfer_order SET status = 'APPROVED', fulfilment_status = 'PENDING', receipt_status = 'PENDING', approved_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        await self._emit("to.approved", aggregate_id=order_uuid, payload={"transfer_order_id": str(order_uuid), "commitment_ids": commitment_ids})
        return {"status": "APPROVED", "commitment_ids": commitment_ids}

    async def fulfil_transfer_order(self, order_id: uuid.UUID | str, lines: Sequence[Mapping[str, Any]], *, idempotency_key: str) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        header = await self._one("SELECT * FROM inventory_ledger.transfer_order WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=order_uuid)
        if header is None:
            raise TransactionNotFound("transfer order not found")
        if header["status"] not in {"APPROVED", "PARTIALLY_FULFILLED"}:
            raise InvalidStateTransition("transfer order is not approved")
        movements: list[dict[str, Any]] = []
        staged = []
        status_id = await self._default_status_id()
        for requested in lines:
            line_number = int(requested.get("line_number") or 0)
            qty = quantize(requested.get("qty"))
            if qty <= 0:
                raise ValidationFailed("staged qty must be > 0")
            line = await self._one("SELECT * FROM inventory_ledger.transfer_order_line WHERE tenant_id = :tenant_id AND transfer_order_id = :order AND line_number = :line", order=order_uuid, line=line_number)
            if line is None:
                raise ValidationFailed(f"transfer order line {line_number} not found")
            movements.append({"item_id": line["item_id"], "location_id": header["from_location_id"], "inventory_status_id": status_id, "qty": -qty, "movement_type": "TO_FULFILLMENT", "source_line_ref": str(line_number)})
            movements.append({"item_id": line["item_id"], "location_id": header["to_location_id"], "inventory_status_id": status_id, "qty_class": "IN_TRANSIT", "qty": qty, "movement_type": "TO_FULFILLMENT", "source_line_ref": str(line_number)})
            await self._exec("UPDATE inventory_ledger.transfer_order_line SET qty_fulfilled = qty_fulfilled + :qty, qty_in_transit = qty_in_transit + :qty, updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=line["id"], qty=qty)
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_in_transit (id, tenant_id, transfer_order_line_id, item_id, from_location_id, to_location_id, qty_in_transit, value_base, fulfilled_at, status, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :line, :item, :from_location, :to_location, :qty, 0, now(), 'IN_TRANSIT', now(), now(), 1) "
                "ON CONFLICT (tenant_id, transfer_order_line_id) DO UPDATE SET qty_in_transit = inventory_ledger.inventory_in_transit.qty_in_transit + EXCLUDED.qty_in_transit, status = 'IN_TRANSIT', updated_at = now()",
                id=uuid.uuid4(), line=line["id"], item=line["item_id"], from_location=header["from_location_id"], to_location=header["to_location_id"], qty=qty,
            )
            staged.append({"line_number": line_number, "qty": _jsonable(qty)})
        result = await self.ledger.post({"posting_type": "T-10", "transaction_date": utc_now().date().isoformat(), "source_doc_type": "TRANSFER_ORDER", "source_doc_id": order_uuid, "idempotency_key": idempotency_key, "movements": movements})
        await self._exec("UPDATE inventory_ledger.transfer_order SET status = 'FULFILLED', fulfilment_status = 'FULFILLED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        await self._emit("to.fulfilled", aggregate_id=order_uuid, payload={"transfer_order_id": str(order_uuid), "transaction_id": result["transaction_id"]})
        return {"transaction_id": result["transaction_id"], "staged": staged}

    async def receive_transfer_order(self, order_id: uuid.UUID | str, lines: Sequence[Mapping[str, Any]], *, idempotency_key: str) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        header = await self._one("SELECT * FROM inventory_ledger.transfer_order WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=order_uuid)
        if header is None:
            raise TransactionNotFound("transfer order not found")
        movements: list[dict[str, Any]] = []
        received = []
        status_id = await self._default_status_id()
        for requested in lines:
            line_number = int(requested.get("line_number") or 0)
            qty = quantize(requested.get("qty"))
            if qty <= 0:
                raise ValidationFailed("received qty must be > 0")
            line = await self._one("SELECT * FROM inventory_ledger.transfer_order_line WHERE tenant_id = :tenant_id AND transfer_order_id = :order AND line_number = :line", order=order_uuid, line=line_number)
            if line is None:
                raise ValidationFailed(f"transfer order line {line_number} not found")
            if qty > Decimal(str(line["qty_in_transit"])):
                raise ValidationFailed("received quantity exceeds in-transit quantity")
            movements.append({"item_id": line["item_id"], "location_id": header["to_location_id"], "inventory_status_id": status_id, "qty_class": "IN_TRANSIT", "qty": -qty, "movement_type": "TO_RECEIPT", "source_line_ref": str(line_number)})
            movements.append({"item_id": line["item_id"], "location_id": header["to_location_id"], "inventory_status_id": status_id, "qty": qty, "movement_type": "TO_RECEIPT", "source_line_ref": str(line_number)})
            await self._exec("UPDATE inventory_ledger.transfer_order_line SET qty_received = qty_received + :qty, qty_in_transit = qty_in_transit - :qty, updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=line["id"], qty=qty)
            await self._exec("UPDATE inventory_ledger.inventory_in_transit SET qty_in_transit = qty_in_transit - :qty, status = 'PARTIALLY_RECEIVED', updated_at = now() WHERE tenant_id = :tenant_id AND transfer_order_line_id = :line", line=line["id"], qty=qty)
            received.append({"line_number": line_number, "qty": _jsonable(qty)})
        result = await self.ledger.post({"posting_type": "T-11", "transaction_date": utc_now().date().isoformat(), "source_doc_type": "TRANSFER_ORDER", "source_doc_id": order_uuid, "idempotency_key": idempotency_key, "movements": movements})
        await self._exec("UPDATE inventory_ledger.transfer_order SET receipt_status = 'PARTIALLY_RECEIVED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        await self._emit("to.received", aggregate_id=order_uuid, payload={"transfer_order_id": str(order_uuid), "transaction_id": result["transaction_id"]})
        return {"transaction_id": result["transaction_id"], "received": received}

    async def close_transfer_order(self, order_id: uuid.UUID | str, *, reason: str | None = None) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        await self._exec("UPDATE inventory_ledger.transfer_order SET status = 'CLOSED', receipt_status = 'CLOSED', closed_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        await self._emit("to.closed", aggregate_id=order_uuid, payload={"transfer_order_id": str(order_uuid), "reason": reason})
        return {"status": "CLOSED"}

    async def cancel_transfer_order(self, order_id: uuid.UUID | str, *, reason: str | None = None) -> dict[str, Any]:
        order_uuid = _as_uuid(order_id, field="transfer_order_id")
        header = await self._one("SELECT status FROM inventory_ledger.transfer_order WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        if header is None:
            raise TransactionNotFound("transfer order not found")
        if header["status"] not in {"DRAFT", "PENDING_APPROVAL"}:
            raise InvalidStateTransition("transfer order cannot be cancelled after approval/posting")
        await self._exec("UPDATE inventory_ledger.transfer_order SET status = 'CANCELLED', updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=order_uuid)
        return {"status": "CANCELLED"}

    # -- fulfilments / returns ----------------------------------------------------------------
    async def post_fulfillment(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        lines = payload.get("lines") or []
        movements: list[dict[str, Any]] = []
        status_id = await self._default_status_id()
        commitments: list[Mapping[str, Any]] = []
        for line in lines:
            qty = quantize(line.get("qty"))
            commitment = None
            if line.get("commitment_id"):
                loaded = await self._one("SELECT * FROM inventory_ledger.inventory_commitment WHERE tenant_id = :tenant_id AND id = :id", id=_as_uuid(line["commitment_id"], field="commitment_id"))
                if loaded is not None:
                    commitments.append(loaded)
            movements.append({
                "item_id": _as_uuid(line.get("item_id"), field="item_id"),
                "location_id": _as_uuid(line.get("location_id"), field="location_id"),
                "inventory_status_id": _as_uuid(line.get("inventory_status_id") or status_id, field="inventory_status_id"),
                "ownership": str(line.get("ownership") or "OWNED"), "bin_id": line.get("bin_id"), "lot_id": line.get("lot_id"),
                "qty": -qty, "movement_type": "FULFILLMENT", "source_line_ref": str(len(movements) + 1),
            })
        result = await self.ledger.post({"posting_type": "T-12", "transaction_date": payload.get("transaction_date") or utc_now().date().isoformat(), "external_ref": payload.get("external_ref"), "source_doc_type": "ORDER_FULFILLMENT", "idempotency_key": idempotency_key, "movements": movements})
        backordered = QTY_ZERO
        for commitment in commitments:
            fulfilled = quantize(payload.get("lines") and next((entry.get("qty") for entry in lines if entry.get("commitment_id") == str(commitment["id"])), 0) or 0)
            await self._exec("UPDATE inventory_ledger.inventory_commitment SET qty_fulfilled = qty_fulfilled + :qty, status = CASE WHEN qty_fulfilled + :qty >= qty_committed THEN 'FULFILLED' ELSE 'PARTIALLY_FULFILLED' END, updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=commitment["id"], qty=fulfilled)
        await self._emit("inventory.fulfilled", aggregate_id=result["transaction_id"], payload={"transaction_id": result["transaction_id"], "backordered_qty": _jsonable(backordered)})
        return {"transaction_id": result["transaction_id"], "fulfilled_qty": _jsonable(sum((quantize(line.get("qty")) for line in lines), QTY_ZERO)), "backordered_qty": _jsonable(backordered)}

    async def post_return(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        lines = payload.get("lines") or []
        status_id = await self._default_status_id()
        movements = [{
            "item_id": _as_uuid(line.get("item_id"), field="item_id"),
            "location_id": _as_uuid(line.get("location_id"), field="location_id"),
            "inventory_status_id": _as_uuid(line.get("inventory_status_id") or status_id, field="inventory_status_id"),
            "ownership": str(line.get("ownership") or "OWNED"), "bin_id": line.get("bin_id"), "lot_id": line.get("lot_id"),
            "qty": quantize(line.get("qty")), "movement_type": "RETURN", "source_line_ref": str(index),
        } for index, line in enumerate(lines, start=1)]
        result = await self.ledger.post({"posting_type": "T-13", "transaction_date": payload.get("transaction_date") or utc_now().date().isoformat(), "external_ref": payload.get("external_ref"), "source_doc_type": "ORDER_RETURN", "idempotency_key": idempotency_key, "movements": movements})
        return {"transaction_id": result["transaction_id"]}

    # -- distributions ------------------------------------------------------------------------
    async def create_distribution(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        distribution_id = uuid.uuid4()
        await self._exec(
            "INSERT INTO inventory_ledger.inventory_distribution (id, tenant_id, document_number, status, transaction_date, memo, distribution_mode, bootstrap_mode, created_at, updated_at, version) "
            "VALUES (:id, :tenant_id, :number, 'DRAFT', :date, :memo, :mode, :bootstrap, now(), now(), 1)",
            id=distribution_id, number=str(payload.get("document_number") or f"DIST-{uuid.uuid4().hex[:12].upper()}"),
            date=_as_date(payload["transaction_date"]) if payload.get("transaction_date") else None, memo=payload.get("memo"),
            mode=str(payload.get("distribution_mode") or "SIMPLE"), bootstrap=bool(payload.get("bootstrap_mode", True)),
        )
        for index, line in enumerate(payload.get("lines") or [], start=1):
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_distribution_line (id, tenant_id, inventory_distribution_id, line_number, item_id, to_location_id, inventory_status_id, bin_id, lot_id, serial_id, qty, unit_cost, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :header, :line, :item, :to_location, :status, :bin, :lot, :serial, :qty, :cost, now(), now(), 1)",
                id=uuid.uuid4(), header=distribution_id, line=index, item=_as_uuid(line.get("item_id"), field="item_id"),
                to_location=_as_uuid(line.get("to_location_id"), field="to_location_id"),
                status=_as_uuid(line.get("inventory_status_id") or await self._default_status_id(), field="inventory_status_id"),
                bin=_as_uuid(line["bin_id"], field="bin_id") if line.get("bin_id") else None,
                lot=_as_uuid(line["lot_id"], field="lot_id") if line.get("lot_id") else None,
                serial=_as_uuid(line["serial_id"], field="serial_id") if line.get("serial_id") else None,
                qty=quantize(line.get("qty")), cost=quantize(line["unit_cost"]) if line.get("unit_cost") is not None else None,
            )
        return await self.get_document("inventory_distribution", distribution_id)

    async def post_distribution(self, document_id: uuid.UUID | str, *, idempotency_key: str) -> dict[str, Any]:
        document_uuid = _as_uuid(document_id, field="distribution_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_distribution WHERE tenant_id = :tenant_id AND id = :id FOR UPDATE", id=document_uuid)
        if header is None:
            raise TransactionNotFound("distribution not found")
        sentinel = await self._one("SELECT id FROM traceability.location WHERE tenant_id = :tenant_id AND is_unallocated LIMIT 1")
        if sentinel is None:
            raise ValidationFailed("UNALLOCATED sentinel location not configured")
        lines = await self._doc_lines("inventory_distribution_line", "inventory_distribution_id", document_uuid)
        movements = []
        for line in lines:
            movements.append({"item_id": line["item_id"], "location_id": sentinel["id"], "inventory_status_id": line["inventory_status_id"], "qty": -Decimal(str(line["qty"])), "movement_type": "BOOTSTRAP_DISTRIBUTION", "source_line_ref": str(line["line_number"])})
            movements.append({"item_id": line["item_id"], "location_id": line["to_location_id"], "inventory_status_id": line["inventory_status_id"], "qty": Decimal(str(line["qty"])), "movement_type": "BOOTSTRAP_DISTRIBUTION", "source_line_ref": str(line["line_number"])})
        result = await self.ledger.post({"posting_type": "T-09", "transaction_date": header["transaction_date"] or utc_now().date().isoformat(), "source_doc_type": "INVENTORY_DISTRIBUTION", "source_doc_id": document_uuid, "idempotency_key": idempotency_key, "allow_unallocated": True, "movements": movements})
        await self._exec("UPDATE inventory_ledger.inventory_distribution SET status = 'POSTED', posted_transaction_id = :txn, posted_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=document_uuid, txn=result["transaction_id"])
        await self._emit("inventory.distributed", aggregate_id=document_uuid, payload={"distribution_id": str(document_uuid), "transaction_id": result["transaction_id"]})
        return result

    # -- imports ------------------------------------------------------------------------------
    async def create_import(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        batch_id = uuid.uuid4()
        rows = payload.get("rows") or []
        await self._exec(
            "INSERT INTO inventory_ledger.import_batch (id, tenant_id, import_type, status, filename, storage_key, row_count, idempotency_key, options, created_at, updated_at, version) "
            "VALUES (:id, :tenant_id, :type, 'UPLOADED', :filename, :storage_key, :row_count, :key, :options, now(), now(), 1)",
            id=batch_id, type=str(payload.get("import_type") or "ADJUSTMENT"), filename=payload.get("filename"),
            storage_key=payload.get("storage_key"), row_count=len(rows), key=idempotency_key,
            options=json.dumps(payload.get("options") or {}),
        )
        for index, row in enumerate(rows, start=1):
            await self._exec(
                "INSERT INTO inventory_ledger.import_batch_row (id, tenant_id, import_batch_id, row_number, raw_row, status, created_at, updated_at, version) VALUES (:id, :tenant_id, :batch, :row, :raw, 'PENDING', now(), now(), 1)",
                id=uuid.uuid4(), batch=batch_id, row=index, raw=json.dumps(_jsonable(row)),
            )
        return await self.get_import(batch_id)

    async def get_import(self, batch_id: uuid.UUID | str) -> dict[str, Any]:
        batch_uuid = _as_uuid(batch_id, field="import_batch_id")
        batch = await self._one("SELECT * FROM inventory_ledger.import_batch WHERE tenant_id = :tenant_id AND id = :id", id=batch_uuid)
        if batch is None:
            raise TransactionNotFound("import batch not found")
        return _jsonable(batch)

    async def validate_import(self, batch_id: uuid.UUID | str) -> dict[str, Any]:
        batch_uuid = _as_uuid(batch_id, field="import_batch_id")
        rows = await self._doc_lines("import_batch_row", "import_batch_id", batch_uuid)
        valid = invalid = 0
        for row in rows:
            raw = row["raw_row"] or {}
            if raw.get("item_id") and raw.get("location_id") and raw.get("qty"):
                await self._exec("UPDATE inventory_ledger.import_batch_row SET status = 'VALID', updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=row["id"])
                valid += 1
            else:
                await self._exec("UPDATE inventory_ledger.import_batch_row SET status = 'INVALID', error_code = 'ROW_INVALID', error_detail = 'item_id/location_id/qty required', updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=row["id"])
                invalid += 1
        status = "VALIDATED" if invalid == 0 else "PARTIAL"
        await self._exec("UPDATE inventory_ledger.import_batch SET status = :status, error_count = :invalid, updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=batch_uuid, status=status, invalid=invalid)
        return {"status": status, "valid_count": valid, "error_count": invalid}

    async def post_import(self, batch_id: uuid.UUID | str, *, idempotency_key: str) -> dict[str, Any]:
        batch_uuid = _as_uuid(batch_id, field="import_batch_id")
        batch = await self.get_import(batch_uuid)
        rows = await self._all("SELECT * FROM inventory_ledger.import_batch_row WHERE tenant_id = :tenant_id AND import_batch_id = :batch AND status IN ('VALID','PENDING') ORDER BY row_number", batch=batch_uuid)
        posted = failed = 0
        for row in rows:
            raw = row["raw_row"] or {}
            try:
                if batch["import_type"] == "OPENING_BALANCE":
                    await self.create_adjustment({"transaction_date": raw.get("transaction_date"), "lines": [{"item_id": raw["item_id"], "location_id": raw["location_id"], "inventory_status_id": raw.get("inventory_status_id"), "qty_delta": raw["qty"], "unit_cost": raw.get("unit_cost")}]}, idempotency_key=f"{idempotency_key}:{row['row_number']}")
                else:
                    await self.create_adjustment({"memo": f"import {row['row_number']}", "lines": [{"item_id": raw["item_id"], "location_id": raw["location_id"], "inventory_status_id": raw.get("inventory_status_id"), "qty_delta": raw["qty"]}]}, idempotency_key=f"{idempotency_key}:{row['row_number']}")
                await self._exec("UPDATE inventory_ledger.import_batch_row SET status = 'POSTED', updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=row["id"])
                posted += 1
            except Exception:  # noqa: BLE001 - per-row isolation (import contract)
                await self._exec("UPDATE inventory_ledger.import_batch_row SET status = 'FAILED', error_code = 'POSTING_FAILED', updated_at = now() WHERE tenant_id = :tenant_id AND id = :id", id=row["id"])
                failed += 1
        status = "POSTED" if failed == 0 else "PARTIAL"
        await self._exec("UPDATE inventory_ledger.import_batch SET status = :status, success_count = :posted, error_count = error_count + :failed, finished_at = now(), updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=batch_uuid, status=status, posted=posted, failed=failed)
        await self._emit("import.posted" if failed == 0 else "import.failed", aggregate_id=batch_uuid, payload={"import_batch_id": str(batch_uuid), "posted": posted, "failed": failed, "status": status})
        return {"status": status, "posted_count": posted, "failed_count": failed}

    async def import_rows(self, batch_id: uuid.UUID | str, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        batch_uuid = _as_uuid(batch_id, field="import_batch_id")
        clauses = ["tenant_id = :tenant_id", "import_batch_id = :batch"]
        params: dict[str, Any] = {"batch": batch_uuid, "limit": max(1, min(int(limit), 500))}
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        rows = await self._all("SELECT * FROM inventory_ledger.import_batch_row WHERE " + " AND ".join(clauses) + " ORDER BY row_number LIMIT :limit", **params)
        return {"items": [_jsonable(row) for row in rows], "next_cursor": None}

    # -- reason codes -------------------------------------------------------------------------
    async def list_reason_codes(self, *, reason_type: str | None = None, active: bool | None = None) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if reason_type:
            clauses.append("reason_type = :type")
            params["type"] = reason_type.upper()
        if active is not None:
            clauses.append("is_active = :active")
            params["active"] = active
        rows = await self._all("SELECT * FROM inventory_ledger.inventory_reason_code WHERE " + " AND ".join(clauses) + " ORDER BY reason_type, code", **params)
        return {"items": [_jsonable(row) for row in rows]}

    async def create_reason_code(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        reason_id = uuid.uuid4()
        try:
            await self._exec(
                "INSERT INTO inventory_ledger.inventory_reason_code (id, tenant_id, code, name, reason_type, account_ref, is_system, is_active, created_at, updated_at, version) "
                "VALUES (:id, :tenant_id, :code, :name, :type, :account, false, true, now(), now(), 1)",
                id=reason_id, code=str(payload.get("code") or "").upper(), name=str(payload.get("name") or payload.get("code") or ""),
                type=str(payload.get("reason_type") or "ADJUSTMENT").upper(), account=payload.get("account_ref"),
            )
        except IntegrityError as exc:
            raise ledger_error("reason code already exists", code="INVALID_STATE_TRANSITION", http_status=409) from exc
        return {"id": str(reason_id), "code": str(payload.get("code") or "").upper(), "reason_type": str(payload.get("reason_type") or "ADJUSTMENT").upper(), "is_active": True}

    async def update_reason_code(self, reason_id: uuid.UUID | str, payload: Mapping[str, Any]) -> dict[str, Any]:
        reason_uuid = _as_uuid(reason_id, field="reason_code_id")
        values = {key: payload[key] for key in ("name", "account_ref", "is_active") if key in payload}
        if values:
            await self._exec("UPDATE inventory_ledger.inventory_reason_code SET " + ", ".join(f"{key} = :{key}" for key in values) + ", updated_at = now(), version = version + 1 WHERE tenant_id = :tenant_id AND id = :id", id=reason_uuid, **values)
        row = await self._one("SELECT * FROM inventory_ledger.inventory_reason_code WHERE tenant_id = :tenant_id AND id = :id", id=reason_uuid)
        if row is None:
            raise TransactionNotFound("reason code not found")
        return _jsonable(row)


__all__ = ["LedgerDocuments"]
