"""FS-06 numbered-inventory use-case service: lot/serial identity, FEFO, validation, reconciliation.

Serial current state/history is derived: projection and history rows are written by the 0013 posting
triggers (``DM-TRACE-04/08``). This service owns identity registration/mutation, the deterministic FEFO
selection hook, dry-run posting validation for FS-04 callers, expiry scanning and the numbered
reconciliation evidence used by the FS-06 exit gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.traceability_numbered import tables as t
from app.modules.traceability_numbered.errors import (
    DomainDisabled,
    IdentityImmutable,
    ItemNotNumbered,
    LotConflict,
    LotInUse,
    LotNotFound,
    SerialConflict,
    SerialNotFound,
    ValidationError,
    VersionConflict,
)
from app.modules.traceability_numbered.repository import NumberedRepository, utc_now

__all__ = [
    "CONTRACT_VERSION",
    "DOMAIN_FLAG",
    "FEFO_TIEBREAK",
    "SERIAL_TRANSITIONS",
    "NumberedService",
    "fefo_order_key",
    "map_ledger_event",
    "validate_date_pair",
    "validate_lot_number",
    "validate_serial_number",
]

CONTRACT_VERSION = "1.0.0"
DOMAIN_FLAG = "inventory.numbered.enabled"
LOT_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,63}$")
SERIAL_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,95}$")

#: Deterministic FEFO tie-break (FS-06 §5 + ALLOC-FIX-04): expiry ASC, then lot number, then lot id.
FEFO_TIEBREAK = "expiry_date ASC NULLS LAST, lot_number ASC, lot_id ASC"

#: Movement type → serial lifecycle event.
SERIAL_EVENT_BY_MOVEMENT = {
    "RECEIPT": "RECEIVED",
    "VENDOR_RETURN": "MOVED",
    "ADJUSTMENT": "ADJUSTED",
    "WORKSHEET": "ADJUSTED",
    "TRANSFER": "MOVED",
    "TO_FULFILLMENT": "MOVED",
    "TO_RECEIPT": "MOVED",
    "FULFILLMENT": "FULFILLED",
    "RETURN": "RETURNED",
    "KIT_FULFILLMENT": "FULFILLED",
    "ASSEMBLY_BUILD": "RECEIVED",
    "ASSEMBLY_UNBUILD": "RETURNED",
    "WIP_ISSUE": "MOVED",
    "WIP_COMPLETION": "RECEIVED",
    "COUNT_VARIANCE": "ADJUSTED",
    "CONSIGNED_RECEIPT": "RECEIVED",
    "CONSIGNED_OWNERSHIP": "MOVED",
    "CONSIGNED_ADJUST": "ADJUSTED",
    "BIN_TRANSFER": "MOVED",
    "STATUS_CHANGE": "MOVED",
    "REVERSAL": "RETURNED",
}

#: Allowed serial lifecycle transitions (state-machine guard used by validation/reconcile).
SERIAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "UNKNOWN": frozenset({"AVAILABLE", "SCRAPPED"}),
    "AVAILABLE": frozenset({"COMMITTED", "IN_TRANSIT", "FULFILLED", "RETURNED", "SCRAPPED", "AVAILABLE"}),
    "COMMITTED": frozenset({"AVAILABLE", "FULFILLED", "IN_TRANSIT"}),
    "IN_TRANSIT": frozenset({"AVAILABLE", "FULFILLED"}),
    "RETURNED": frozenset({"AVAILABLE", "SCRAPPED"}),
    "FULFILLED": frozenset({"RETURNED"}),
    "SCRAPPED": frozenset(),
}


def fefo_order_key(row: Mapping[str, Any]) -> tuple:
    """Deterministic in-memory FEFO key: expiry (nulls last), lot number, lot id."""
    expiry = row.get("expiry_date")
    return (expiry is None, expiry or date.max, str(row.get("lot_number") or ""), str(row.get("lot_id") or ""))


def map_ledger_event(movement_type: str) -> str:
    return SERIAL_EVENT_BY_MOVEMENT.get(str(movement_type or "").upper(), "MOVED")


def validate_lot_number(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("lot_number is required", details={"field": "lot_number"})
    normalised = value.strip()
    if not LOT_NUMBER_RE.match(normalised):
        raise ValidationError("lot_number must be 1-64 chars of letters/digits/._-/", details={"field": "lot_number"})
    return normalised


def validate_serial_number(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("serial_number is required", details={"field": "serial_number"})
    normalised = value.strip()
    if not SERIAL_NUMBER_RE.match(normalised):
        raise ValidationError(
            "serial_number must be 1-96 chars of letters/digits/._-/", details={"field": "serial_number"}
        )
    return normalised


def validate_date_pair(manufacture_date: date | None, expiry_date: date | None) -> tuple[date | None, date | None]:
    """Raise when expiry precedes manufacture (both null-safe)."""
    if manufacture_date is not None and expiry_date is not None and expiry_date < manufacture_date:
        raise ValidationError("expiry_date must not precede manufacture_date", details={"field": "expiry_date"})
    return manufacture_date, expiry_date


class NumberedService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = NumberedRepository(session)

    # ------------------------------------------------------------------ gates ---------------
    async def require_enabled(self, tenant_id: UUID | str) -> None:
        if not await self.repository.domain_enabled(tenant_id):
            raise DomainDisabled(
                "inventory.numbered.enabled is off for this tenant",
                details={"flag": DOMAIN_FLAG},
            )

    async def require_numbered_item(
        self, tenant_id: UUID | str, item_id: UUID | str, *, serial: bool
    ) -> dict[str, Any]:
        item = await self.repository.item_numbering(tenant_id, item_id)
        if item is None:
            raise ItemNotNumbered("item not found", details={"item_id": str(item_id)})
        if not item.get("requires_numbering"):
            raise ItemNotNumbered(
                "item type is not numbered (lot/serial)", details={"item_type_code": item.get("item_type_code")}
            )
        if not item.get("is_active"):
            raise ValidationError("item is inactive", details={"item_id": str(item_id)})
        return item

    # ------------------------------------------------------------------ lots -----------------
    async def register_lot(
        self,
        *,
        tenant_id: UUID | str,
        item_id: UUID | str,
        lot_number: str,
        manufacture_date: date | None = None,
        expiry_date: date | None = None,
        cost_basis: Decimal | str | None = None,
        notes: str | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        await self.require_numbered_item(tenant_id, item_id, serial=False)
        number = validate_lot_number(lot_number)
        validate_date_pair(manufacture_date, expiry_date)
        if cost_basis is not None and Decimal(str(cost_basis)) < 0:
            raise ValidationError("cost_basis must be >= 0", details={"field": "cost_basis"})
        existing = await self.repository.get_lot_by_number(tenant_id, item_id, number)
        if existing is not None:
            raise LotConflict("lot_number already exists for this item", details={"lot_id": existing["id"]})
        try:
            lot = await self.repository.insert_lot(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "item_id": item_id,
                    "lot_number": number,
                    "manufacture_date": manufacture_date,
                    "expiry_date": expiry_date,
                    "cost_basis": cost_basis,
                    "notes": notes,
                    "is_active": True,
                }
            )
        except IntegrityError as exc:
            raise LotConflict("lot_number already exists for this item") from exc
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.lot.created",
            resource_type="traceability.lot",
            resource_id=lot["id"],
            diff={"lot_number": number},
        )
        await self.repository.emit_event(
            event_type="traceability.lot.created",
            tenant_id=tenant_id,
            aggregate_id=lot["id"],
            payload={
                "lot_id": lot["id"],
                "item_id": str(item_id),
                "lot_number": number,
                "expiry_date": expiry_date,
                "manufacture_date": manufacture_date,
            },
        )
        return lot

    async def get_lot(
        self, tenant_id: UUID | str, lot_id: UUID | str, *, include_positions: bool = False
    ) -> dict[str, Any]:
        lot = await self.repository.get_lot(tenant_id, lot_id)
        if lot is None:
            raise LotNotFound("lot not found", details={"lot_id": str(lot_id)})
        lot["on_hand"] = str(await self.repository.lot_on_hand(tenant_id, lot_id))
        lot["committed"] = str(await self.repository.lot_committed(tenant_id, lot_id))
        if include_positions:
            lot["positions"] = await self.repository.lot_positions(tenant_id, lot_id)
        return lot

    async def list_lots(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_lots(tenant_id, **filters)

    async def update_lot(
        self,
        *,
        tenant_id: UUID | str,
        lot_id: UUID | str,
        values: Mapping[str, Any],
        actor_id: UUID | str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        lot = await self.repository.get_lot(tenant_id, lot_id)
        if lot is None:
            raise LotNotFound("lot not found", details={"lot_id": str(lot_id)})
        allowed = {"manufacture_date", "expiry_date", "cost_basis", "notes"}
        unknown = set(values) - allowed
        if unknown:
            raise ValidationError("unsupported lot fields", details={"fields": sorted(unknown)})
        has_movements = await self.repository.lot_has_movements(tenant_id, lot_id)
        if has_movements and any(field in values for field in ("manufacture_date", "expiry_date", "cost_basis")):
            raise IdentityImmutable(
                "lot dates/cost basis are immutable once stock movements exist",
                details={"lot_id": str(lot_id)},
            )
        validate_date_pair(
            values.get("manufacture_date", lot.get("manufacture_date")),
            values.get("expiry_date", lot.get("expiry_date")),
        )
        if "cost_basis" in values and values["cost_basis"] is not None and Decimal(str(values["cost_basis"])) < 0:
            raise ValidationError("cost_basis must be >= 0", details={"field": "cost_basis"})
        updated = await self.repository.update_lot(tenant_id, lot_id, values, expected_version=expected_version)
        if updated is None:
            raise VersionConflict("lot version does not match If-Match", details={"lot_id": str(lot_id)})
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.lot.updated",
            resource_type="traceability.lot",
            resource_id=lot_id,
            diff={key: str(value) for key, value in values.items()},
        )
        return updated

    async def inactivate_lot(
        self,
        *,
        tenant_id: UUID | str,
        lot_id: UUID | str,
        reason: str | None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        lot = await self.repository.get_lot(tenant_id, lot_id)
        if lot is None:
            raise LotNotFound("lot not found", details={"lot_id": str(lot_id)})
        if not lot["is_active"]:
            return lot
        on_hand = await self.repository.lot_on_hand(tenant_id, lot_id)
        committed = await self.repository.lot_committed(tenant_id, lot_id)
        if on_hand > 0 or committed > 0:
            raise LotInUse(
                "lot still carries stock or commitments", details={"on_hand": str(on_hand), "committed": str(committed)}
            )
        updated = await self.repository.update_lot(
            tenant_id,
            lot_id,
            {"is_active": False, "inactive_at": utc_now(), "inactive_reason": reason},
        )
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.lot.inactivated",
            resource_type="traceability.lot",
            resource_id=lot_id,
            diff={"reason": reason},
        )
        await self.repository.emit_event(
            event_type="traceability.lot.inactivated",
            tenant_id=tenant_id,
            aggregate_id=lot_id,
            payload={"lot_id": str(lot_id), "reason": reason},
        )
        assert updated is not None
        return updated

    # ------------------------------------------------------------------ serials --------------
    async def register_serial(
        self,
        *,
        tenant_id: UUID | str,
        item_id: UUID | str,
        serial_number: str,
        specific_cost: Decimal | str | None = None,
        ownership: str = "OWNED",
        customer_ref: str | None = None,
        actor_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self.require_enabled(tenant_id)
        await self.require_numbered_item(tenant_id, item_id, serial=True)
        number = validate_serial_number(serial_number)
        if ownership not in t.OWNERSHIP_VALUES:
            raise ValidationError("invalid ownership", details={"ownership": ownership})
        if specific_cost is not None and Decimal(str(specific_cost)) < 0:
            raise ValidationError("specific_cost must be >= 0", details={"field": "specific_cost"})
        existing = await self.repository.get_serial_by_number(tenant_id, item_id, number)
        if existing is not None:
            raise SerialConflict("serial_number already exists for this item", details={"serial_id": existing["id"]})
        try:
            serial = await self.repository.insert_serial(
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "item_id": item_id,
                    "serial_number": number,
                    "state": "UNKNOWN",
                    "ownership": ownership,
                    "specific_cost": specific_cost,
                    "customer_ref": customer_ref,
                }
            )
        except IntegrityError as exc:
            raise SerialConflict("serial_number already exists for this item") from exc
        await self.repository.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.serial.registered",
            resource_type="traceability.serial_number",
            resource_id=serial["id"],
            diff={"serial_number": number},
        )
        await self.repository.emit_event(
            event_type="traceability.serial.created",
            tenant_id=tenant_id,
            aggregate_id=serial["id"],
            payload={"serial_id": serial["id"], "item_id": str(item_id), "serial_number": number},
        )
        return serial

    async def get_serial(
        self, tenant_id: UUID | str, serial_id: UUID | str, *, include_history: bool = False
    ) -> dict[str, Any]:
        serial = await self.repository.get_serial(tenant_id, serial_id)
        if serial is None:
            raise SerialNotFound("serial not found", details={"serial_id": str(serial_id)})
        if include_history:
            serial["history"] = await self.repository.list_serial_history(tenant_id, serial_id)
        return serial

    async def list_serials(self, tenant_id: UUID | str, **filters: Any) -> list[dict[str, Any]]:
        return await self.repository.list_serials(tenant_id, **filters)

    async def serial_history(
        self, tenant_id: UUID | str, serial_id: UUID | str, **filters: Any
    ) -> list[dict[str, Any]]:
        serial = await self.repository.get_serial(tenant_id, serial_id)
        if serial is None:
            raise SerialNotFound("serial not found", details={"serial_id": str(serial_id)})
        return await self.repository.list_serial_history(tenant_id, serial_id, **filters)

    # ------------------------------------------------------------------ posting hooks --------
    async def numbering_policy(self, tenant_id: UUID | str, item_id: UUID | str) -> dict[str, Any]:
        item = await self.repository.item_numbering(tenant_id, item_id)
        if item is None:
            return {
                "item_id": str(item_id),
                "item_type_code": None,
                "requires_numbering": False,
                "requires_lot": False,
                "requires_serial": False,
                "allow_expired": False,
                "enabled": await self.repository.domain_enabled(tenant_id),
            }
        code = item.get("item_type_code")
        return {
            "item_id": str(item_id),
            "item_type_code": code,
            "requires_numbering": bool(item.get("requires_numbering")),
            "requires_lot": code == "LOT_NUMBERED",
            "requires_serial": code == "SERIALIZED",
            "allow_expired": False,
            "enabled": await self.repository.domain_enabled(tenant_id),
        }

    async def validate_posting(
        self,
        *,
        tenant_id: UUID | str,
        item_id: UUID | str,
        transaction_type: str,
        line: Mapping[str, Any],
    ) -> dict[str, Any]:
        violations: list[dict[str, Any]] = []
        policy = await self.numbering_policy(tenant_id, item_id)
        details = list(line.get("details") or [])
        qty = Decimal(str(line.get("qty_base") or 0))
        serials = [detail for detail in details if detail.get("serial_number") or detail.get("serial_id")]
        if policy["requires_serial"] and not details:
            violations.append(
                {
                    "path": "line.details",
                    "code": "SERIAL_REQUIRED",
                    "message": "serialized items require serial details",
                }
            )
        if policy["requires_lot"] and not details:
            violations.append(
                {"path": "line.details", "code": "LOT_REQUIRED", "message": "lot-numbered items require lot details"}
            )
        if len(serials) > 1:
            violations.append(
                {"path": "line.details", "code": "MULTIPLE_SERIALS", "message": "one serial per detail line is allowed"}
            )
        seen: set[str] = set()
        for index, detail in enumerate(details):
            if policy["requires_serial"]:
                key = str(detail.get("serial_number") or detail.get("serial_id"))
                if key in seen:
                    violations.append(
                        {
                            "path": f"line.details[{index}].serial_number",
                            "code": "DUPLICATE_SERIAL",
                            "message": "serial listed twice in the same posting",
                        }
                    )
                seen.add(key)
                if abs(Decimal(str(detail.get("qty_base") or 0))) != 1:
                    violations.append(
                        {
                            "path": f"line.details[{index}].qty_base",
                            "code": "SERIAL_QTY_MISMATCH",
                            "message": "serialized detail qty must be +/-1",
                        }
                    )
            if policy["requires_lot"] and not policy["requires_serial"]:
                lot_id = detail.get("lot_id")
                lot_number = detail.get("lot_number")
                if lot_id:
                    lot = await self.repository.get_lot(tenant_id, lot_id)
                    if lot is None:
                        violations.append(
                            {
                                "path": f"line.details[{index}].lot_id",
                                "code": "LOT_NOT_FOUND",
                                "message": "lot not found",
                            }
                        )
                    else:
                        expiry = lot.get("expiry_date")
                        if expiry is not None and date.fromisoformat(str(expiry)[:10]) < date.today():
                            violations.append(
                                {
                                    "path": f"line.details[{index}].lot_id",
                                    "code": "LOT_EXPIRED",
                                    "message": "expired lots cannot be issued",
                                }
                            )
                elif not lot_number:
                    violations.append(
                        {"path": f"line.details[{index}]", "code": "LOT_REQUIRED", "message": "lot reference required"}
                    )
        if sum(Decimal(str(detail.get("qty_base") or 0)) for detail in details) != qty:
            violations.append(
                {
                    "path": "line.qty_base",
                    "code": "DETAIL_QTY_SUM_MISMATCH",
                    "message": "detail quantities must sum to the line quantity",
                }
            )
        return {"valid": not violations, "transaction_type": transaction_type, "violations": violations}

    async def plan_fefo(
        self,
        *,
        tenant_id: UUID | str,
        item_id: UUID | str,
        location_id: UUID | str | None,
        qty: Decimal | str,
        as_of_date: date,
        inventory_status_id: UUID | str | None = None,
        ownership: str | None = None,
    ) -> dict[str, Any]:
        policy = await self.numbering_policy(tenant_id, item_id)
        required = Decimal(str(qty))
        if required <= 0:
            raise ValidationError("qty must be > 0", details={"field": "qty"})
        if not policy["requires_lot"]:
            return {
                "allocations": [
                    {
                        "lot_id": None,
                        "expiry_date": None,
                        "location_id": str(location_id) if location_id else None,
                        "bin_id": None,
                        "qty": str(required),
                        "lot_number": None,
                    }
                ],
                "unfilled_qty": "0",
                "policy": policy,
            }
        rows = await self.repository.fefo_lot_rows(
            tenant_id,
            item_id=item_id,
            location_id=location_id,
            as_of=as_of_date,
            inventory_status_id=inventory_status_id,
            ownership=ownership,
        )
        allocations: list[dict[str, Any]] = []
        remaining = required
        for row in sorted(rows, key=fefo_order_key):
            available = Decimal(str(row.get("qty_on_hand") or 0)) - Decimal(str(row.get("qty_committed") or 0))
            if available <= 0:
                continue
            take = min(available, remaining)
            allocations.append(
                {
                    "lot_id": row["lot_id"],
                    "lot_number": row["lot_number"],
                    "expiry_date": row["expiry_date"],
                    "location_id": row["location_id"],
                    "bin_id": None,
                    "qty": str(take),
                }
            )
            remaining -= take
            if remaining <= 0:
                break
        return {"allocations": allocations, "unfilled_qty": str(max(remaining, Decimal("0"))), "policy": policy}

    async def search(
        self,
        tenant_id: UUID | str,
        *,
        q: str | None = None,
        item_id: UUID | str | None = None,
        location_id: UUID | str | None = None,
        state: str | None = None,
        expiry_before: date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        serials = await self.repository.list_serials(
            tenant_id,
            item_id=item_id,
            serial_number=q,
            state=state,
            current_location_id=location_id,
            limit=limit,
            offset=offset,
        )
        lots = await self.repository.list_lots(
            tenant_id,
            item_id=item_id,
            lot_number_prefix=q,
            expiry_before=expiry_before,
            limit=limit,
            offset=offset,
        )
        return {"serials": serials, "lots": lots, "next_cursor": None}

    async def deferred_fk_status(self) -> dict[str, Any]:
        counts = await self.repository.deferred_fk_counts()
        return {
            "validated": sum(counts.values()) == 0,
            "orphan_counts": counts,
            "last_checked_at": utc_now().isoformat(),
        }

    async def reconcile(self, tenant_id: UUID | str) -> dict[str, Any]:
        """Projection/history reconciliation evidence for INV-18 (zero drift expected)."""
        serials = await self.repository.list_serials(tenant_id, limit=10_000)
        drift: list[dict[str, Any]] = []
        for serial in serials:
            latest = await self.repository.latest_serial_history(tenant_id, serial["id"])
            if latest is None:
                drift.append({"serial_id": serial["id"], "reason": "missing history"})
                continue
            if latest.get("to_location_id") != serial.get("current_location_id"):
                drift.append({"serial_id": serial["id"], "reason": "location projection differs from latest history"})
            if latest.get("to_status_id") != serial.get("current_status_id"):
                drift.append({"serial_id": serial["id"], "reason": "status projection differs from latest history"})
        return {
            "tenant_id": str(tenant_id),
            "serials_checked": len(serials),
            "drift_count": len(drift),
            "drift": drift,
            "reconciled_at": utc_now().isoformat(),
        }

    async def expiry_scan(
        self, tenant_id: UUID | str, *, horizons: Sequence[int] = (30, 60, 90)
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        today = datetime.now(UTC).date()
        for horizon in horizons:
            cutoff = today + timedelta(days=horizon)
            lots = await self.repository.list_lots(tenant_id, expiry_before=cutoff, is_active=True, limit=500)
            for lot in lots:
                if lot.get("expiry_date") is None:
                    continue
                expiry = date.fromisoformat(str(lot["expiry_date"])[:10])
                if expiry < today:
                    continue
                on_hand = Decimal(str(lot.get("on_hand") or 0))
                if on_hand <= 0:
                    continue
                warnings.append(
                    {
                        "tenant_id": str(tenant_id),
                        "item_id": lot["item_id"],
                        "lot_id": lot["id"],
                        "expiry_date": lot["expiry_date"],
                        "days_to_expiry": (expiry - today).days,
                        "on_hand": str(on_hand),
                        "horizon_days": horizon,
                    }
                )
        return warnings
