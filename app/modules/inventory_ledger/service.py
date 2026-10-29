"""FS-04 atomic inventory ledger engine (async SQLAlchemy Core).

All posting paths go through :meth:`LedgerService.post`: validate → idempotency → ordered balance locks →
append-only header/lines/details → synchronous balance/bin/lot projections → outbox events, in the
caller's transaction. Commitments and owned documents (adjustment, worksheet, transfer, TO, distribution,
import) use the same transaction/idempotency discipline. No HTTP imports; no writes to other contexts.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import emit
from app.modules.inventory_ledger.errors import (
    IdempotencyConflict,
    NegativeInventoryBlocked,
    TransactionNotFound,
    UnsupportedPostingType,
    ValidationFailed,
    ledger_error,
)

POSTING_TYPES: tuple[str, ...] = tuple(f"T-{number:02d}" for number in range(1, 31))
#: Posting types whose movements are value-only (qty 0, non-zero value; no inventory position effect).
#: ARCHITECTURE posting table: T-04 vendor bill, T-05 vendor credit, T-06 bill variance, T-19 WIP close,
#: T-24 standard revaluation, T-25 COGS adjustment, T-26 landed cost, T-27 estimated landed cost,
#: T-28 drop-ship (COGS/AP value only, never on-hand).
VALUE_ONLY_POSTING_TYPES: frozenset[str] = frozenset(
    {"T-04", "T-05", "T-06", "T-19", "T-24", "T-25", "T-26", "T-27", "T-28"}
)
#: Canonical ledger `movement_type` written for each value-only posting type (the DB check allows the
#: documented value-only movement classes; callers may pass their source-document label which we normalise).
VALUE_ONLY_MOVEMENT_TYPE = {
    "T-04": "COST_ONLY",
    "T-05": "COST_ONLY",
    "T-06": "VARIANCE",
    "T-19": "COST_ONLY",
    "T-24": "REVALUATION",
    "T-25": "COGS_ADJUST",
    "T-26": "LANDED_COST",
    "T-27": "LANDED_COST",
    "T-28": "DROP_SHIP",
}
SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
QTY_SCALE = Decimal("0.000000000001")
QTY_ZERO = Decimal("0")
EVENT_BY_TYPE = {
    "T-01": ("inventory.receipt.posted",),
    "T-02": ("inventory.receipt.reversed", "inventory.reversal.posted"),
    "T-03": ("inventory.issued",),
    "T-07": ("inventory.adjusted",),
    "T-08": ("inventory.adjusted",),
    "T-09": ("inventory.transferred",),
    "T-10": ("to.fulfilled", "inventory.in_transit.changed"),
    "T-11": ("to.received", "inventory.in_transit.changed"),
    "T-12": ("inventory.fulfilled", "inventory.issued"),
    "T-13": ("inventory.returned",),
    "T-14": ("inventory.fulfilled", "inventory.issued"),
    "T-20": ("inventory.adjusted",),
    "T-29": ("bin.transferred",),
    "T-30": ("inventory.status_changed", "inventory.availability.changed"),
    "T-04": ("inventory.value.posted",),
    "T-05": ("inventory.value.posted",),
    "T-06": ("inventory.value.posted",),
    "T-19": ("inventory.value.posted",),
    "T-24": ("inventory.value.posted",),
    "T-25": ("inventory.value.posted",),
    "T-26": ("inventory.value.posted", "landed_cost.applied"),
    "T-27": ("inventory.value.posted", "landed_cost.estimated"),
    "T-28": ("inventory.value.posted", "dropship.fulfilled"),
}
REVERSAL_TYPE = {"T-01": "T-02", "T-02": "T-01"}


class NegativePolicy(StrEnum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    ALLOW = "ALLOW"


def utc_now() -> datetime:
    return datetime.now(UTC)


def quantize(value: Any) -> Decimal:
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"invalid decimal quantity {value!r}") from exc
    if not decimal.is_finite():
        raise ValidationFailed("quantity must be finite")
    return decimal.quantize(QTY_SCALE, rounding=ROUND_HALF_UP)


def availability(on_hand: Decimal, committed: Decimal) -> Decimal:
    return max(on_hand - committed, QTY_ZERO)


def _as_uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"invalid {field}") from exc


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationFailed(f"invalid transaction_date {value!r}") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        text_value = format(value, "f")
        if "." in text_value:
            text_value = text_value.rstrip("0").rstrip(".")
        return text_value or "0"
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    return value


def validate_command(command: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise/validate a PostingCommand; returns canonical dict with Decimal movements."""
    posting_type = str(command.get("posting_type") or command.get("transaction_type") or "").upper()
    if posting_type not in POSTING_TYPES:
        raise UnsupportedPostingType(f"posting type {posting_type!r} is not a ledged movement type")
    raw_movements = command.get("movements") or command.get("lines")
    if not isinstance(raw_movements, list) or not raw_movements:
        raise ValidationFailed("at least one movement is required")
    movements: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_movements, start=1):
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"movement {index} must be an object")
        raw_qty = raw.get("qty", raw.get("qty_base"))
        value_only_type = posting_type in VALUE_ONLY_POSTING_TYPES
        qty = QTY_ZERO if raw_qty is None and value_only_type else quantize(raw_qty)
        value_base = quantize(raw.get("value_base", raw.get("value_delta", 0)))
        value_txn = quantize(raw.get("value_txn", raw.get("value_delta", value_base)))
        if value_only_type:
            if qty != 0:
                raise ValidationFailed(
                    f"movement {index}: posting type {posting_type} is value-only and must have qty=0"
                )
            if value_base == 0:
                raise ValidationFailed(
                    f"movement {index}: value-only posting requires a non-zero value"
                )
        elif qty == 0:
            raise ValidationFailed(f"movement {index} quantity must be non-zero")
        line: dict[str, Any] = {
            "line_number": index,
            "item_id": _as_uuid(raw.get("item_id"), field="item_id"),
            "location_id": _as_uuid(raw.get("location_id"), field="location_id"),
            "inventory_status_id": _as_uuid(raw.get("inventory_status_id"), field="inventory_status_id"),
            "ownership": str(raw.get("ownership") or "OWNED").upper(),
            "qty_class": str(raw.get("qty_class") or "ON_HAND").upper(),
            "lot_id": _as_uuid(raw["lot_id"], field="lot_id") if raw.get("lot_id") else None,
            "serial_id": _as_uuid(raw["serial_id"], field="serial_id") if raw.get("serial_id") else None,
            "uom_id": _as_uuid(raw["uom_id"], field="uom_id") if raw.get("uom_id") else None,
            "qty_uom": quantize(raw.get("qty_uom", qty)),
            "conversion_factor": quantize(raw.get("conversion_factor", 1)),
            "qty_base": qty,
            "value_base": value_base,
            "value_txn": value_txn,
            "value_only": value_only_type,
            "unit_cost": quantize(raw["unit_cost"]) if raw.get("unit_cost") is not None else None,
            "movement_type": (
                VALUE_ONLY_MOVEMENT_TYPE.get(posting_type, "COST_ONLY")
                if value_only_type
                else str(raw.get("movement_type") or "ADJUSTMENT").upper()
            ),
            "movement_direction": (
                "VALUE_ONLY" if value_only_type else str(raw.get("movement_direction") or ("IN" if qty > 0 else "OUT")).upper()
            ),
            "reason_code_id": _as_uuid(raw["reason_code_id"], field="reason_code_id") if raw.get("reason_code_id") else None,
            "source_line_ref": raw.get("source_line_ref"),
            "class_ref": raw.get("class_ref"),
            "department_ref": raw.get("department_ref"),
            "subsidiary_ref": raw.get("subsidiary_ref"),
            "cost_object_id": _as_uuid(raw["cost_object_id"], field="cost_object_id") if raw.get("cost_object_id") else None,
            "details": [],
        }
        if line["ownership"] not in {"OWNED", "CONSIGNED", "IN_TRANSIT"}:
            raise ValidationFailed("invalid ownership")
        if line["qty_class"] not in {"ON_HAND", "IN_TRANSIT"}:
            raise ValidationFailed("invalid qty_class")
        detail_rows = raw.get("details") or []
        if detail_rows:
            detail_total = QTY_ZERO
            for detail in detail_rows:
                if not isinstance(detail, Mapping):
                    raise ValidationFailed("details must be objects")
                detail_qty = quantize(detail.get("qty", detail.get("qty_base")))
                detail_total += detail_qty
                line["details"].append(
                    {
                        "bin_id": _as_uuid(detail["bin_id"], field="bin_id") if detail.get("bin_id") else None,
                        "lot_id": _as_uuid(detail["lot_id"], field="lot_id") if detail.get("lot_id") else None,
                        "serial_id": _as_uuid(detail["serial_id"], field="serial_id") if detail.get("serial_id") else None,
                        "inventory_status_id": _as_uuid(detail.get("inventory_status_id") or line["inventory_status_id"], field="inventory_status_id"),
                        "ownership": str(detail.get("ownership") or line["ownership"]).upper(),
                        "qty_base": detail_qty,
                        "value_base": quantize(detail.get("value_base", QTY_ZERO)),
                    }
                )
            if detail_total != qty:
                raise ValidationFailed(f"movement {index} details do not sum to the line quantity")
        else:
            line["details"].append(
                {
                    "bin_id": _as_uuid(raw["bin_id"], field="bin_id") if raw.get("bin_id") else None,
                    "lot_id": line["lot_id"],
                    "serial_id": line["serial_id"],
                    "inventory_status_id": line["inventory_status_id"],
                    "ownership": line["ownership"],
                    "qty_base": qty,
                    "value_base": line["value_base"],
                }
            )
        movements.append(line)
    return {
        "posting_type": posting_type,
        "transaction_date": _as_date(command.get("transaction_date") or utc_now().date()),
        "source_doc_type": command.get("source_doc_type"),
        "source_doc_id": command.get("source_doc_id"),
        "external_ref": command.get("external_ref"),
        "currency_code": str(command["currency_code"]).upper() if command.get("currency_code") else None,
        "exchange_rate": quantize(command.get("exchange_rate", 1)),
        "idempotency_key": command.get("idempotency_key"),
        "correlation_id": command.get("correlation_id"),
        "import_batch_id": command.get("import_batch_id"),
        "memo": command.get("memo"),
        "negative_policy_override": command.get("negative_policy_override"),
        "allow_unallocated": bool(command.get("allow_unallocated", False)),
        "movements": movements,
        "value_only": posting_type in VALUE_ONLY_POSTING_TYPES,
        "reversal_of_transaction_id": command.get("reversal_of_transaction_id"),
        "reversal_reason": command.get("reversal_reason"),
    }


class LedgerService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | str, *, costing: Any | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
        self._sequence_key = f"ledger_seq:{self.tenant_id}"
        self.costing = costing

    # -- low-level helpers --------------------------------------------------------------------
    async def _exec(self, sql: str, **params: Any):
        return await self.session.execute(text(sql), {"tenant_id": self.tenant_id, **params})

    async def _one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = (await self._exec(sql, **params)).mappings().first()
        return dict(row) if row else None

    async def _all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in (await self._exec(sql, **params)).mappings().all()]

    async def _scalar(self, sql: str, **params: Any) -> Any:
        return (await self._exec(sql, **params)).scalar_one_or_none()

    async def _emit(self, event_type: str, *, aggregate_id: uuid.UUID | str, payload: Mapping[str, Any], version: int = 1) -> str:
        row = await emit(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type="inventory_transaction",
            aggregate_id=str(aggregate_id),
            payload=_jsonable(dict(payload)),
            aggregate_version=max(1, int(version)),
        )
        return str(row["event_id"])

    async def _next_sequence(self) -> int:
        await self._exec("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))", key=self._sequence_key)
        current = await self._scalar("SELECT max(ledger_sequence) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id")
        return int(current or 0) + 1

    # -- pure/policy helpers ------------------------------------------------------------------
    async def _negative_policy(self, transaction_type: str) -> NegativePolicy:
        override = None
        row = await self._one(
            "SELECT value FROM platform.tenant_setting WHERE tenant_id = :tenant_id AND namespace = 'inventory' AND key = 'negative_inventory_policy'"
        )
        if row is not None:
            override = str(row["value"]).strip('"').upper()
        try:
            return NegativePolicy(override or "BLOCK")
        except ValueError:
            return NegativePolicy.BLOCK

    @staticmethod
    def _balance_delta(movements: Iterable[Mapping[str, Any]]) -> dict[tuple, Decimal]:
        """Aggregate balance deltas at the physical dimension carried by each detail.

        A movement can carry line-level quantity while its physical lot/serial/status/bin dimensions
        live on ``details`` (receipts, transfers, counts). Using the line's null ``lot_id`` here would
        create an aggregate balance row and hide lot-specific availability.
        """
        deltas: dict[tuple, Decimal] = {}
        for movement in movements:
            details = movement.get("details") or []
            if details:
                for detail in details:
                    key = (
                        movement["item_id"], movement["location_id"],
                        detail.get("inventory_status_id") or movement["inventory_status_id"],
                        detail.get("ownership") or movement["ownership"], movement["qty_class"],
                        detail.get("lot_id") if detail.get("lot_id") is not None else movement.get("lot_id"),
                        detail.get("serial_id") if detail.get("serial_id") is not None else movement.get("serial_id"),
                    )
                    deltas[key] = deltas.get(key, QTY_ZERO) + detail.get("qty_base", QTY_ZERO)
            else:
                key = (
                    movement["item_id"], movement["location_id"], movement["inventory_status_id"],
                    movement["ownership"], movement["qty_class"], movement.get("lot_id"), movement.get("serial_id"),
                )
                deltas[key] = deltas.get(key, QTY_ZERO) + movement["qty_base"]
        return deltas

    # -- posting ------------------------------------------------------------------------------
    async def _tenant_base_currency(self) -> str | None:
        value = await self._scalar(
            "SELECT base_currency_code FROM platform.tenant WHERE id = :tenant_id"
        )
        return str(value).upper() if value else None

    async def post(self, command: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        validated = validate_command(command)
        if not validated.get("currency_code"):
            # The ledger records amounts in the tenant's base currency. Posting paths that do not
            # carry a document currency must inherit it from the tenant instead of the DB default.
            validated["currency_code"] = await self._tenant_base_currency() or "USD"
        key = idempotency_key or validated.get("idempotency_key")
        if not key:
            raise ledger_error("Idempotency-Key or idempotency_key is required", code="IDEMPOTENCY_KEY_REQUIRED", http_status=400)
        validated["idempotency_key"] = str(key)
        # Serialise concurrent retries for the same key before the second-line unique check.
        await self._exec("SELECT pg_advisory_xact_lock(hashtextextended(:ikey, 0))", ikey=f"idem:{self.tenant_id}:{key}")
        existing = await self._one(
            "SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id AND idempotency_key = :key",
            key=str(key),
        )
        if existing is not None:
            return await self._result(existing)
        value_only = bool(validated.get("value_only"))
        policy = NegativePolicy.ALLOW if value_only else await self._negative_policy(validated["posting_type"])
        deltas: dict[tuple, Decimal] = {} if value_only else self._balance_delta(validated["movements"])
        # Ordered row locks; BLOCK policy rejects a resulting negative ON_HAND position.
        for position in sorted(deltas, key=lambda item: tuple(str(part) for part in item)):
            item_id, location_id, status_id, ownership, qty_class, lot_id, serial_id = position
            row = await self._one(
                "SELECT id, qty_on_hand FROM inventory_ledger.inventory_balance "
                "WHERE tenant_id = :tenant_id AND item_id = :item AND location_id = :location AND inventory_status_id = :status "
                "AND ownership = :ownership AND qty_class = :qty_class AND lot_id IS NOT DISTINCT FROM :lot AND serial_id IS NOT DISTINCT FROM :serial FOR UPDATE",
                item=item_id, location=location_id, status=status_id, ownership=ownership, qty_class=qty_class, lot=lot_id, serial=serial_id,
            )
            current = Decimal(str(row["qty_on_hand"])) if row else QTY_ZERO
            resulting = current + deltas[position]
            if resulting < 0 and qty_class == "ON_HAND" and policy is NegativePolicy.BLOCK:
                raise NegativeInventoryBlocked(
                    "negative inventory is blocked for this position",
                    details={"item_id": str(item_id), "location_id": str(location_id), "qty_on_hand": format(resulting, "f")},
                )
        sequence = await self._next_sequence()
        transaction_id = uuid.uuid4()
        posting_at = utc_now()
        # FS-05 valuation (steps 8/9/13): compute/persist costing BEFORE the append-only line insert and
        # write the final value columns in the insert itself (the append-only trigger forbids line UPDATE).
        if self.costing is not None and not value_only:
            for movement in validated["movements"]:
                line_id = uuid.uuid4()
                movement["_line_id"] = line_id
                try:
                    costed = await self.costing.value_line(
                        item_id=movement["item_id"], location_id=movement["location_id"], qty=movement["qty_base"],
                        movement_type=movement["movement_type"], transaction_date=validated["transaction_date"],
                        transaction_id=transaction_id, line_id=line_id, lot_id=movement["lot_id"],
                        serial_id=movement["serial_id"], unit_cost=movement["unit_cost"],
                        value_delta=movement["value_base"] if movement["value_base"] != 0 else None,
                        reason=validated["memo"],
                    )
                except Exception as exc:  # noqa: BLE001 - typed costing errors abort the posting
                    raise ledger_error(f"costing valuation failed: {exc}", code="COSTING_ERROR", http_status=409) from exc
                value = costed.get("value_delta")
                if value is not None:
                    movement["value_base"] = Decimal(str(value))
                    movement["value_txn"] = Decimal(str(value))
                    movement["unit_cost"] = Decimal(str(costed.get("unit_cost"))) if costed.get("unit_cost") is not None else movement["unit_cost"]
                    movement["cost_estimated"] = bool(costed.get("estimated", False))
        total_qty = sum((movement["qty_base"] for movement in validated["movements"]), QTY_ZERO)
        total_value = sum((movement["value_base"] for movement in validated["movements"]), QTY_ZERO)
        try:
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_transaction
                    (id, tenant_id, transaction_type, transaction_date, posting_at, ledger_sequence, source_doc_type,
                     source_doc_id, external_ref, reversal_of_transaction_id, reversal_reason, currency_code,
                     exchange_rate, total_qty_base, total_value_base, idempotency_key, import_batch_id, correlation_id,
                     memo, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :type, :date, :posting_at, :sequence, :source_type, :source_id, :external,
                        :reversal_of, :reversal_reason, :currency, :exchange_rate, :total_qty, :total_value, :key,
                        :import_batch, :correlation, :memo, now(), now(), 1)
                """,
                id=transaction_id, type=validated["posting_type"], date=validated["transaction_date"], posting_at=posting_at,
                sequence=sequence, source_type=validated["source_doc_type"], source_id=validated["source_doc_id"],
                external=validated["external_ref"], reversal_of=validated["reversal_of_transaction_id"],
                reversal_reason=validated["reversal_reason"], currency=validated["currency_code"],
                exchange_rate=validated["exchange_rate"], total_qty=total_qty, total_value=total_value, key=str(key),
                import_batch=validated["import_batch_id"], correlation=validated["correlation_id"], memo=validated["memo"],
            )
        except IntegrityError as exc:
            # Concurrent same-key insert: replay the winner.
            winner = await self._one(
                "SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id AND idempotency_key = :key",
                key=str(key),
            )
            if winner is not None:
                return await self._result(winner)
            raise IdempotencyConflict("posting key conflict") from exc
        for line in validated["movements"]:
            line_id = line.get("_line_id") or uuid.uuid4()
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_transaction_line
                    (id, tenant_id, inventory_transaction_id, line_number, item_id, location_id, inventory_status_id,
                     ownership, qty_class, lot_id, serial_id, uom_id, qty_uom, conversion_factor, qty_base, value_base,
                     value_txn, unit_cost, movement_type, movement_direction, reason_code_id, source_line_ref, class_ref,
                     department_ref, subsidiary_ref, cost_object_id, cost_estimated, transaction_date, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :txn, :line, :item, :location, :status, :ownership, :qty_class, :lot, :serial,
                        :uom, :qty_uom, :factor, :qty_base, :value_base, :value_txn, :unit_cost, :movement_type,
                        :direction, :reason, :source_ref, :class_ref, :department_ref, :subsidiary_ref, :cost_object,
                        false, :date, now(), now(), 1)
                """,
                id=line_id, txn=transaction_id, line=line["line_number"], item=line["item_id"], location=line["location_id"],
                status=line["inventory_status_id"], ownership=line["ownership"], qty_class=line["qty_class"], lot=line["lot_id"],
                serial=line["serial_id"], uom=line["uom_id"], qty_uom=line["qty_uom"], factor=line["conversion_factor"],
                qty_base=line["qty_base"], value_base=line["value_base"], value_txn=line["value_txn"], unit_cost=line["unit_cost"],
                movement_type=line["movement_type"], direction=line["movement_direction"], reason=line["reason_code_id"],
                source_ref=line["source_line_ref"], class_ref=line["class_ref"], department_ref=line["department_ref"],
                subsidiary_ref=line["subsidiary_ref"], cost_object=line["cost_object_id"], date=validated["transaction_date"],
            )
            for detail in line["details"]:
                # Procurement's value-only calls may carry an empty physical
                # placeholder. The financial value is on the ledger line; this
                # zero/identity-free row represents no allocation to persist.
                # Real physical and nonempty details keep their numbered guards.
                if (
                    value_only
                    and detail["qty_base"] == 0
                    and detail["value_base"] == 0
                    and not any(detail.get(key) for key in ("bin_id", "lot_id", "serial_id"))
                ):
                    continue
                await self._exec(
                    """
                    INSERT INTO inventory_ledger.inventory_transaction_detail
                        (id, tenant_id, inventory_transaction_line_id, inventory_transaction_id, transaction_date, bin_id,
                         lot_id, serial_id, inventory_status_id, ownership, qty_base, value_base, created_at, updated_at, version)
                    VALUES (:id, :tenant_id, :line_id, :txn, :date, :bin, :lot, :serial, :status, :ownership, :qty, :value,
                            now(), now(), 1)
                    """,
                    id=uuid.uuid4(), line_id=line_id, txn=transaction_id, date=validated["transaction_date"],
                    bin=detail["bin_id"], lot=detail["lot_id"], serial=detail["serial_id"], status=detail["inventory_status_id"],
                    ownership=detail["ownership"], qty=detail["qty_base"], value=detail["value_base"],
                )
        if not value_only:
            await self._apply_balance_deltas(validated["movements"], sequence=sequence, posting_at=posting_at)
        event_ids: list[str] = []
        payload = {
            "transaction_id": str(transaction_id), "ledger_sequence": sequence, "transaction_type": validated["posting_type"],
            "total_qty_base": format(total_qty, "f"), "total_value_base": format(total_value, "f"),
            "value_only": value_only,
        }
        event_ids.append(await self._emit("inventory.balance.changed", aggregate_id=transaction_id, payload=payload, version=sequence))
        for event_type in EVENT_BY_TYPE.get(validated["posting_type"], ()):
            event_ids.append(await self._emit(event_type, aggregate_id=transaction_id, payload=payload, version=sequence))
        return {
            "transaction_id": str(transaction_id),
            "ledger_sequence": sequence,
            "transaction_type": validated["posting_type"],
            "status": "POSTED",
            "posting_at": posting_at.isoformat(),
            "source_doc_type": validated["source_doc_type"],
            "source_doc_id": str(validated["source_doc_id"]) if validated["source_doc_id"] else None,
            "total_qty_base": _jsonable(total_qty),
            "total_value_base": _jsonable(total_value),
            "value_only": value_only,
            "event_ids": event_ids,
            "warnings": [] if policy is not NegativePolicy.WARN else ["NEGATIVE_INVENTORY_WARN_POLICY"],
        }

    async def _apply_balance_deltas(self, movements: Sequence[Mapping[str, Any]], *, sequence: int, posting_at: datetime) -> None:
        bin_deltas: dict[tuple, Decimal] = {}
        lot_deltas: dict[tuple, tuple[Decimal, Decimal]] = {}
        for movement in movements:
            for detail in movement["details"]:
                if detail["bin_id"] is not None and movement["qty_class"] == "ON_HAND":
                    key = (
                        movement["item_id"], movement["location_id"], detail["bin_id"], detail["inventory_status_id"],
                        detail["ownership"], detail["lot_id"], detail["serial_id"],
                    )
                    bin_deltas[key] = bin_deltas.get(key, QTY_ZERO) + detail["qty_base"]
                if detail["lot_id"] is not None:
                    lot_key = (
                        movement["item_id"], movement["location_id"], detail["lot_id"], detail["inventory_status_id"],
                        detail["ownership"],
                    )
                    on_hand, committed = lot_deltas.get(lot_key, (QTY_ZERO, QTY_ZERO))
                    lot_deltas[lot_key] = (on_hand + detail["qty_base"], committed)
        for key, delta in self._balance_delta(movements).items():
            item_id, location_id, status_id, ownership, qty_class, lot_id, serial_id = key
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_balance
                    (id, tenant_id, item_id, location_id, inventory_status_id, ownership, qty_class, lot_id, serial_id,
                     qty_on_hand, qty_committed, qty_on_order, qty_backordered, value_base, last_ledger_sequence,
                     last_posting_at, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :item, :location, :status, :ownership, :qty_class, :lot, :serial, :qty, 0, 0, 0,
                        0, :sequence, :posting_at, now(), now(), 1)
                ON CONFLICT ON CONSTRAINT uq_inventory_balance_key
                DO UPDATE SET qty_on_hand = inventory_ledger.inventory_balance.qty_on_hand + EXCLUDED.qty_on_hand,
                              last_ledger_sequence = EXCLUDED.last_ledger_sequence,
                              last_posting_at = EXCLUDED.last_posting_at,
                              updated_at = now(), version = inventory_ledger.inventory_balance.version + 1
                """,
                id=uuid.uuid4(), item=item_id, location=location_id, status=status_id, ownership=ownership,
                qty_class=qty_class, lot=lot_id, serial=serial_id, qty=delta, sequence=sequence, posting_at=posting_at,
            )
        for key, delta in bin_deltas.items():
            item_id, location_id, bin_id, status_id, ownership, lot_id, serial_id = key
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_balance_bin
                    (id, tenant_id, item_id, location_id, bin_id, inventory_status_id, ownership, lot_id, serial_id,
                     qty_on_hand, last_ledger_sequence, created_at, updated_at, version)
                VALUES (:id, :tenant_id, :item, :location, :bin, :status, :ownership, :lot, :serial, :qty, :sequence, now(), now(), 1)
                ON CONFLICT ON CONSTRAINT uq_inventory_balance_bin_key
                DO UPDATE SET qty_on_hand = inventory_ledger.inventory_balance_bin.qty_on_hand + EXCLUDED.qty_on_hand,
                              last_ledger_sequence = EXCLUDED.last_ledger_sequence, updated_at = now(),
                              version = inventory_ledger.inventory_balance_bin.version + 1
                """,
                id=uuid.uuid4(), item=item_id, location=location_id, bin=bin_id, status=status_id, ownership=ownership,
                lot=lot_id, serial=serial_id, qty=delta, sequence=sequence,
            )
        for lot_key, (on_hand_delta, _committed_delta) in lot_deltas.items():
            item_id, location_id, lot_id, status_id, ownership = lot_key
            await self._exec(
                """
                INSERT INTO inventory_ledger.inventory_balance_lot
                    (id, tenant_id, item_id, location_id, lot_id, inventory_status_id, ownership, qty_on_hand, qty_committed,
                     created_at, updated_at, version)
                VALUES (:id, :tenant_id, :item, :location, :lot, :status, :ownership, :qty, 0, now(), now(), 1)
                ON CONFLICT ON CONSTRAINT uq_inventory_balance_lot_key
                DO UPDATE SET qty_on_hand = inventory_ledger.inventory_balance_lot.qty_on_hand + EXCLUDED.qty_on_hand,
                              updated_at = now(), version = inventory_ledger.inventory_balance_lot.version + 1
                """,
                id=uuid.uuid4(), item=item_id, location=location_id, lot=lot_id, status=status_id, ownership=ownership,
                qty=on_hand_delta,
            )

    async def _result(self, header: Mapping[str, Any]) -> dict[str, Any]:
        events = await self._all(
            "SELECT event_type FROM platform.outbox_event WHERE tenant_id = :tenant_id AND aggregate_id = :id",
            id=header["id"],
        )
        return {
            "transaction_id": str(header["id"]),
            "ledger_sequence": int(header["ledger_sequence"]),
            "transaction_type": str(header["transaction_type"]),
            "status": "POSTED",
            "posting_at": _jsonable(header["posting_at"]),
            "source_doc_type": header.get("source_doc_type"),
            "source_doc_id": str(header["source_doc_id"]) if header.get("source_doc_id") else None,
            "total_qty_base": _jsonable(header["total_qty_base"]),
            "total_value_base": _jsonable(header["total_value_base"]),
            "value_only": str(header["transaction_type"]) in VALUE_ONLY_POSTING_TYPES,
            "event_ids": [],
            "warnings": [],
            "replayed": True,
            "event_types": [row["event_type"] for row in events],
        }

    # -- reversal -----------------------------------------------------------------------------
    async def reverse(self, transaction_id: uuid.UUID | str, *, reason: str, idempotency_key: str) -> dict[str, Any]:
        txn_uuid = _as_uuid(transaction_id, field="transaction_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id AND id = :id", id=txn_uuid)
        if header is None:
            raise TransactionNotFound("transaction not found")
        existing = await self._one("SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id AND reversal_of_transaction_id = :id", id=txn_uuid)
        if existing is not None:
            return await self._result(existing)
        lines = await self._all("SELECT * FROM inventory_ledger.inventory_transaction_line WHERE tenant_id = :tenant_id AND inventory_transaction_id = :id ORDER BY line_number", id=txn_uuid)
        detail_rows = await self._all(
            "SELECT * FROM inventory_ledger.inventory_transaction_detail WHERE tenant_id = :tenant_id AND inventory_transaction_id = :id",
            id=txn_uuid,
        )
        details_by_line: dict[Any, list[dict[str, Any]]] = {}
        for detail in detail_rows:
            details_by_line.setdefault(detail["inventory_transaction_line_id"], []).append(detail)
        movements = []
        for line in lines:
            line_details = details_by_line.get(line["id"], [])
            movement = {
                "item_id": line["item_id"], "location_id": line["location_id"], "inventory_status_id": line["inventory_status_id"],
                "ownership": line["ownership"], "qty_class": line["qty_class"], "lot_id": line["lot_id"], "serial_id": line["serial_id"],
                "uom_id": line["uom_id"], "qty": -Decimal(str(line["qty_base"])), "qty_uom": -Decimal(str(line["qty_uom"])),
                "conversion_factor": Decimal(str(line["conversion_factor"])), "value_base": -Decimal(str(line["value_base"])),
                "value_txn": -Decimal(str(line["value_txn"])), "unit_cost": line["unit_cost"],
                "movement_type": "REVERSAL", "movement_direction": "IN" if Decimal(str(line["qty_base"])) < 0 else "OUT",
                "reason_code_id": line["reason_code_id"], "source_line_ref": line["source_line_ref"],
                "cost_object_id": line["cost_object_id"],
            }
            if line_details:
                movement["details"] = [
                    {
                        "bin_id": detail["bin_id"], "lot_id": detail["lot_id"], "serial_id": detail["serial_id"],
                        "inventory_status_id": detail["inventory_status_id"], "ownership": detail["ownership"],
                        "qty": -Decimal(str(detail["qty_base"])), "value_base": -Decimal(str(detail["value_base"])),
                    }
                    for detail in line_details
                ]
            movements.append(movement)
        command = {
            "posting_type": REVERSAL_TYPE.get(str(header["transaction_type"]), str(header["transaction_type"])),
            "transaction_date": utc_now().date().isoformat(),
            "source_doc_type": header.get("source_doc_type"),
            "source_doc_id": header.get("source_doc_id"),
            "reversal_of_transaction_id": txn_uuid,
            "reversal_reason": reason,
            "idempotency_key": idempotency_key,
            "movements": movements,
        }
        result = await self.post(command)
        return result | {"reversal_of_transaction_id": str(txn_uuid)}

    # -- reads --------------------------------------------------------------------------------
    async def get_transaction(self, transaction_id: uuid.UUID | str) -> dict[str, Any]:
        txn_uuid = _as_uuid(transaction_id, field="transaction_id")
        header = await self._one("SELECT * FROM inventory_ledger.inventory_transaction WHERE tenant_id = :tenant_id AND id = :id", id=txn_uuid)
        if header is None:
            raise TransactionNotFound("transaction not found")
        return _jsonable(header)

    async def list_transactions(self, *, item_id: str | None = None, location_id: str | None = None, transaction_type: str | None = None,
                                source_doc_type: str | None = None, source_doc_id: str | None = None,
                                date_from: str | None = None, date_to: str | None = None, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        clauses = ["t.tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if item_id:
            clauses.append("EXISTS (SELECT 1 FROM inventory_ledger.inventory_transaction_line l WHERE l.tenant_id = t.tenant_id AND l.inventory_transaction_id = t.id AND l.item_id = :item)")
            params["item"] = _as_uuid(item_id, field="item_id")
        if location_id:
            clauses.append("EXISTS (SELECT 1 FROM inventory_ledger.inventory_transaction_line l WHERE l.tenant_id = t.tenant_id AND l.inventory_transaction_id = t.id AND l.location_id = :location)")
            params["location"] = _as_uuid(location_id, field="location_id")
        if transaction_type:
            clauses.append("t.transaction_type = :type")
            params["type"] = str(transaction_type).upper()
        if source_doc_type:
            clauses.append("t.source_doc_type = :source_type")
            params["source_type"] = source_doc_type
        if source_doc_id:
            clauses.append("t.source_doc_id = :source_id")
            params["source_id"] = _as_uuid(source_doc_id, field="source_doc_id")
        if date_from:
            clauses.append("t.transaction_date >= :date_from")
            params["date_from"] = _as_date(date_from)
        if date_to:
            clauses.append("t.transaction_date <= :date_to")
            params["date_to"] = _as_date(date_to)
        if cursor:
            params["cursor"] = int(cursor)
            clauses.append("t.ledger_sequence > :cursor")
        rows = await self._all(
            "SELECT t.* FROM inventory_ledger.inventory_transaction t WHERE " + " AND ".join(clauses) +
            " ORDER BY t.ledger_sequence LIMIT :limit", **params,
        )
        items = [_jsonable(row) for row in rows]
        next_cursor = str(items[-1]["ledger_sequence"]) if len(items) == params["limit"] else None
        return {"items": items, "next_cursor": next_cursor}

    async def transaction_lines(self, transaction_id: uuid.UUID | str, *, item_id: str | None = None) -> list[dict[str, Any]]:
        txn_uuid = _as_uuid(transaction_id, field="transaction_id")
        rows = await self._all(
            "SELECT * FROM inventory_ledger.inventory_transaction_line WHERE tenant_id = :tenant_id AND inventory_transaction_id = :id "
            + ("AND item_id = :item " if item_id else "") + "ORDER BY line_number",
            id=txn_uuid, **({"item": _as_uuid(item_id, field="item_id")} if item_id else {}),
        )
        return [_jsonable(row) for row in rows]

    async def transaction_details(self, transaction_id: uuid.UUID | str, *, line_id: str | None = None) -> list[dict[str, Any]]:
        txn_uuid = _as_uuid(transaction_id, field="transaction_id")
        rows = await self._all(
            "SELECT * FROM inventory_ledger.inventory_transaction_detail WHERE tenant_id = :tenant_id AND inventory_transaction_id = :id "
            + ("AND inventory_transaction_line_id = :line " if line_id else "") + "ORDER BY created_at",
            id=txn_uuid, **({"line": _as_uuid(line_id, field="line_id")} if line_id else {}),
        )
        return [_jsonable(row) for row in rows]

    async def balances(self, *, item_id: str | None = None, location_id: str | None = None, inventory_status_id: str | None = None,
                       ownership: str | None = None, qty_class: str | None = None, lot_id: str | None = None,
                       serial_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        for column, value in (("item_id", item_id), ("location_id", location_id), ("inventory_status_id", inventory_status_id),
                              ("lot_id", lot_id), ("serial_id", serial_id)):
            if value:
                clauses.append(f"{column} = :{column}")
                params[column] = _as_uuid(value, field=column)
        if ownership:
            clauses.append("ownership = :ownership")
            params["ownership"] = ownership.upper()
        if qty_class:
            clauses.append("qty_class = :qty_class")
            params["qty_class"] = qty_class.upper()
        rows = await self._all(
            "SELECT * FROM inventory_ledger.inventory_balance WHERE " + " AND ".join(clauses) +
            " ORDER BY item_id, location_id, inventory_status_id LIMIT :limit", **params,
        )
        items = [_jsonable(row) for row in rows]
        return {"items": items, "next_cursor": None}

    async def availability_summary(self, *, item_id: str, location_id: str, inventory_status_id: str | None = None) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id", "item_id = :item", "location_id = :location", "qty_class = 'ON_HAND'"]
        params: dict[str, Any] = {"item": _as_uuid(item_id, field="item_id"), "location": _as_uuid(location_id, field="location_id")}
        if inventory_status_id:
            clauses.append("inventory_status_id = :status")
            params["status"] = _as_uuid(inventory_status_id, field="inventory_status_id")
        rows = await self._all("SELECT qty_on_hand, qty_committed, qty_on_order, qty_backordered FROM inventory_ledger.inventory_balance WHERE " + " AND ".join(clauses), **params)
        on_hand = sum((Decimal(str(row["qty_on_hand"])) for row in rows), QTY_ZERO)
        committed = sum((Decimal(str(row["qty_committed"])) for row in rows), QTY_ZERO)
        on_order = sum((Decimal(str(row["qty_on_order"])) for row in rows), QTY_ZERO)
        backordered = sum((Decimal(str(row["qty_backordered"])) for row in rows), QTY_ZERO)
        in_transit = await self._scalar(
            "SELECT coalesce(sum(qty_in_transit), 0) FROM inventory_ledger.inventory_in_transit WHERE tenant_id = :tenant_id AND item_id = :item AND to_location_id = :location",
            item=_as_uuid(item_id, field="item_id"), location=_as_uuid(location_id, field="location_id"),
        )
        return {
            "item_id": str(item_id), "location_id": str(location_id),
            "on_hand": _jsonable(on_hand), "committed": _jsonable(committed),
            "available": _jsonable(availability(on_hand, committed)), "on_order": _jsonable(on_order),
            "backordered": _jsonable(backordered), "in_transit": _jsonable(Decimal(str(in_transit or 0))),
        }

    async def balance_by_bin(self, *, item_id: str | None = None, location_id: str | None = None, bin_id: str | None = None) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        for column, value in (("item_id", item_id), ("location_id", location_id), ("bin_id", bin_id)):
            if value:
                clauses.append(f"{column} = :{column}")
                params[column] = _as_uuid(value, field=column)
        rows = await self._all("SELECT * FROM inventory_ledger.inventory_balance_bin WHERE " + " AND ".join(clauses) + " ORDER BY item_id, bin_id", **params)
        return {"items": [_jsonable(row) for row in rows]}

    async def balance_by_lot(self, *, item_id: str | None = None, location_id: str | None = None, lot_id: str | None = None) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        for column, value in (("item_id", item_id), ("location_id", location_id), ("lot_id", lot_id)):
            if value:
                clauses.append(f"{column} = :{column}")
                params[column] = _as_uuid(value, field=column)
        rows = await self._all("SELECT * FROM inventory_ledger.inventory_balance_lot WHERE " + " AND ".join(clauses) + " ORDER BY item_id, lot_id", **params)
        return {"items": [_jsonable(row) for row in rows]}

    async def in_transit(self, *, item_id: str | None = None, to_location_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {}
        if item_id:
            clauses.append("item_id = :item")
            params["item"] = _as_uuid(item_id, field="item_id")
        if to_location_id:
            clauses.append("to_location_id = :location")
            params["location"] = _as_uuid(to_location_id, field="to_location_id")
        if status:
            clauses.append("status = :status")
            params["status"] = status.upper()
        rows = await self._all("SELECT * FROM inventory_ledger.inventory_in_transit WHERE " + " AND ".join(clauses) + " ORDER BY created_at", **params)
        return {"items": [_jsonable(row) for row in rows]}

    __all__ = ["LedgerService", "NegativePolicy", "POSTING_TYPES", "availability", "quantize", "validate_command", "utc_now"]
