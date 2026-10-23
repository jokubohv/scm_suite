"""Provider contract facade for `traceability.numbered@1.0.0` (FS-06).

Consumers (FS-04 posting path, FS-07 counts, FS-08 procurement, FS-09 manufacturing, FS-11 WMS, FS-15
reporting) depend only on this module: pure validation helpers, the deterministic FEFO allocation hook,
the numbering policy reader, reconciliation evidence and the serial/ledger event mapping. The facade
never writes another context's tables; DB-backed helpers accept an ``AsyncSession`` supplied by the
caller and run inside the caller's transaction.

Additive-only within major version 1 (framework §5): operation ids/JSON shapes here are covered by
``app/tests/contracts/test_fs06_provider_contract.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "FEFO_ALLOCATION_SCHEMA",
    "FEFO_TIEBREAK",
    "NumberedViolation",
    "NumberedFacade",
    "NumberingPolicy",
    "SERIAL_LIFECYCLE_EVENT_SCHEMA",
    "SerialLifecycleEvent",
    "SERIAL_TRANSITIONS",
    "SUPPORTED_TRANSACTION_TYPES",
    "fefo_order_key",
    "lot_allocation_schema",
    "map_ledger_event",
    "serial_lifecycle_event_schema",
    "validate_numbered_details",
]

CONTRACT_NAME = "traceability.numbered"
CONTRACT_VERSION = "1.0.0"

#: Posting transaction types that may carry lot/serial detail (FS-04 T-matrix subset relevant to FS-06).
SUPPORTED_TRANSACTION_TYPES = frozenset(
    {
        "T-01",
        "T-02",
        "T-03",
        "T-07",
        "T-08",
        "T-09",
        "T-10",
        "T-11",
        "T-12",
        "T-13",
        "T-14",
        "T-15",
        "T-16",
        "T-17",
        "T-18",
        "T-19",
        "T-20",
        "T-21",
        "T-22",
        "T-23",
        "T-29",
        "T-30",
    }
)

FEFO_TIEBREAK = "expiry_date ASC NULLS LAST, lot_number ASC, lot_id ASC"

SERIAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "UNKNOWN": frozenset({"AVAILABLE", "SCRAPPED"}),
    "AVAILABLE": frozenset({"COMMITTED", "IN_TRANSIT", "FULFILLED", "RETURNED", "SCRAPPED", "AVAILABLE"}),
    "COMMITTED": frozenset({"AVAILABLE", "FULFILLED", "IN_TRANSIT"}),
    "IN_TRANSIT": frozenset({"AVAILABLE", "FULFILLED"}),
    "RETURNED": frozenset({"AVAILABLE", "SCRAPPED"}),
    "FULFILLED": frozenset({"RETURNED"}),
    "SCRAPPED": frozenset(),
}

_SERIAL_EVENT_BY_MOVEMENT = {
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


def map_ledger_event(movement_type: str) -> str:
    """Map an FS-04 movement type to the FS-06 serial lifecycle event (deterministic)."""
    return _SERIAL_EVENT_BY_MOVEMENT.get(str(movement_type or "").upper(), "MOVED")


@dataclass(frozen=True, slots=True)
class NumberedViolation:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class NumberingPolicy:
    item_id: str
    item_type_code: str | None
    requires_numbering: bool
    requires_lot: bool
    requires_serial: bool
    allow_expired: bool = False
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_type_code": self.item_type_code,
            "requires_numbering": self.requires_numbering,
            "requires_lot": self.requires_lot,
            "requires_serial": self.requires_serial,
            "allow_expired": self.allow_expired,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class SerialLifecycleEvent:
    """One serial state-change event emitted by the posting path (contract payload shape)."""

    event: str
    serial_id: str
    item_id: str
    occurred_at: datetime | str
    from_state: str | None = None
    to_state: str | None = None
    from_location_id: str | None = None
    to_location_id: str | None = None
    inventory_transaction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        occurred = self.occurred_at
        return {
            "event": self.event,
            "serial_id": self.serial_id,
            "item_id": self.item_id,
            "occurred_at": occurred.isoformat() if isinstance(occurred, datetime) else str(occurred),
            "from_state": self.from_state,
            "to_state": self.to_state,
            "from_location_id": self.from_location_id,
            "to_location_id": self.to_location_id,
            "inventory_transaction_id": self.inventory_transaction_id,
        }


def fefo_order_key(row: Mapping[str, Any]) -> tuple:
    expiry = row.get("expiry_date")
    return (expiry is None, expiry or date.max, str(row.get("lot_number") or ""), str(row.get("lot_id") or ""))


def validate_numbered_details(
    details: Sequence[Mapping[str, Any]],
    policy: NumberingPolicy,
    *,
    qty_base: Decimal | str | int | None = None,
    as_of_date: date | None = None,
) -> list[NumberedViolation]:
    """Pure dry-run validation for posting callers (mirrors the service/DB guard rules)."""
    violations: list[NumberedViolation] = []
    as_of = as_of_date or date.today()
    if policy.requires_serial and not details:
        violations.append(
            NumberedViolation("line.details", "SERIAL_REQUIRED", "serialized items require serial details")
        )
    if policy.requires_lot and not details:
        violations.append(NumberedViolation("line.details", "LOT_REQUIRED", "lot-numbered items require lot details"))
    seen: set[str] = set()
    for index, detail in enumerate(details):
        serial = str(detail.get("serial_number") or detail.get("serial_id") or "")
        if policy.requires_serial:
            if not serial:
                violations.append(
                    NumberedViolation(f"line.details[{index}].serial", "SERIAL_REQUIRED", "serial reference required")
                )
            elif serial in seen:
                violations.append(
                    NumberedViolation(
                        f"line.details[{index}].serial", "DUPLICATE_SERIAL", "serial listed twice in one posting"
                    )
                )
            seen.add(serial)
            if abs(Decimal(str(detail.get("qty_base") or 0))) != 1:
                violations.append(
                    NumberedViolation(
                        f"line.details[{index}].qty_base", "SERIAL_QTY_MISMATCH", "serialized detail qty must be +/-1"
                    )
                )
        if policy.requires_lot and not policy.requires_serial:
            if not (detail.get("lot_id") or detail.get("lot_number")):
                violations.append(
                    NumberedViolation(f"line.details[{index}].lot", "LOT_REQUIRED", "lot reference required")
                )
            expiry = detail.get("expiry_date")
            if expiry is not None and not policy.allow_expired:
                try:
                    expiry_date = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
                except ValueError:
                    expiry_date = None
                if expiry_date is not None and expiry_date < as_of:
                    violations.append(
                        NumberedViolation(f"line.details[{index}].lot", "LOT_EXPIRED", "expired lots cannot be issued")
                    )
    if qty_base is not None and details:
        total = sum(Decimal(str(detail.get("qty_base") or 0)) for detail in details)
        if total != Decimal(str(qty_base)):
            violations.append(
                NumberedViolation(
                    "line.qty_base", "DETAIL_QTY_SUM_MISMATCH", "detail quantities must sum to the line quantity"
                )
            )
    return violations


class NumberedFacade:
    """DB-backed provider facade; runs entirely inside the caller's transaction."""

    def __init__(self, session: Any) -> None:
        from app.modules.traceability_numbered.service import NumberedService

        self._service = NumberedService(session)

    async def get_numbering_policy(self, tenant_id: Any, item_id: Any) -> NumberingPolicy:
        policy = await self._service.numbering_policy(tenant_id, item_id)
        return NumberingPolicy(**policy)

    async def validate_posting(
        self,
        *,
        tenant_id: Any,
        item_id: Any,
        transaction_type: str,
        line: Mapping[str, Any],
    ) -> list[NumberedViolation]:
        report = await self._service.validate_posting(
            tenant_id=tenant_id, item_id=item_id, transaction_type=transaction_type, line=line
        )
        return [NumberedViolation(**item) for item in report.get("violations", [])]

    async def plan_fefo_allocation(
        self,
        *,
        tenant_id: Any,
        item_id: Any,
        location_id: Any,
        qty: Decimal | str,
        as_of_date: date,
        inventory_status_id: Any = None,
        ownership: str | None = None,
    ) -> dict[str, Any]:
        return await self._service.plan_fefo(
            tenant_id=tenant_id,
            item_id=item_id,
            location_id=location_id,
            qty=qty,
            as_of_date=as_of_date,
            inventory_status_id=inventory_status_id,
            ownership=ownership,
        )

    async def reconcile_numbered(self, tenant_id: Any) -> dict[str, Any]:
        return await self._service.reconcile(tenant_id)

    async def deferred_fk_status(self) -> dict[str, Any]:
        return await self._service.deferred_fk_status()

    async def validate_count_position(
        self, *, tenant_id: Any, item_id: Any, lot_id: Any = None, serial_id: Any = None
    ) -> list[NumberedViolation]:
        policy = await self.get_numbering_policy(tenant_id, item_id)
        violations: list[NumberedViolation] = []
        if policy.requires_lot and lot_id is None:
            violations.append(
                NumberedViolation("line.lot", "LOT_REQUIRED", "lot-numbered item requires a lot position")
            )
        if policy.requires_serial and serial_id is None:
            violations.append(
                NumberedViolation("line.serial", "SERIAL_REQUIRED", "serialized item requires a serial position")
            )
        return violations


def lot_allocation_schema() -> dict[str, Any]:
    return {
        "$id": "https://inventory-platform/contracts/traceability.numbered/lot-allocation.schema.json",
        "type": "object",
        "required": ["lot_id", "expiry_date", "location_id", "bin_id", "qty"],
        "additionalProperties": False,
        "properties": {
            "lot_id": {"type": ["string", "null"], "format": "uuid"},
            "lot_number": {"type": ["string", "null"]},
            "expiry_date": {"type": ["string", "null"], "format": "date"},
            "location_id": {"type": ["string", "null"], "format": "uuid"},
            "bin_id": {"type": ["string", "null"], "format": "uuid"},
            "qty": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
        },
    }


def serial_lifecycle_event_schema() -> dict[str, Any]:
    return {
        "$id": "https://inventory-platform/contracts/traceability.numbered/serial-lifecycle-event.schema.json",
        "type": "object",
        "required": ["event", "serial_id", "item_id", "occurred_at"],
        "additionalProperties": True,
        "properties": {
            "event": {
                "enum": ["RECEIVED", "MOVED", "COMMITTED", "RELEASED", "FULFILLED", "RETURNED", "ADJUSTED", "SCRAPPED"]
            },
            "serial_id": {"type": "string", "format": "uuid"},
            "item_id": {"type": "string", "format": "uuid"},
            "from_location_id": {"type": ["string", "null"], "format": "uuid"},
            "to_location_id": {"type": ["string", "null"], "format": "uuid"},
            "inventory_transaction_id": {"type": ["string", "null"], "format": "uuid"},
            "occurred_at": {"type": "string", "format": "date-time"},
        },
    }


FEFO_ALLOCATION_SCHEMA = lot_allocation_schema()
SERIAL_LIFECYCLE_EVENT_SCHEMA = serial_lifecycle_event_schema()
