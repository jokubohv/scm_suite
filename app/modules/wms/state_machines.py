"""Pure WMS state machines + task-engine transition guards (FS-11 §5; DATA-MODEL §19).

No database imports: the service calls :func:`validate_transition` before persisting a status change and
unit tests exercise the closed transition tables directly.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.wms.errors import InvalidStateTransition
from app.modules.wms.tables import (
    INBOUND_SHIPMENT_STATUSES,
    PACK_TASK_STATUSES,
    PICK_TASK_STATUSES,
    RECEIVING_ORDER_STATUSES,
    REPLENISHMENT_PLAN_STATUSES,
    SHIP_MANIFEST_STATUSES,
    SHIPMENT_STATUSES,
    TASK_STATUSES,
    WAVE_STATUSES,
)

TASK_TERMINAL = frozenset({"COMPLETED", "EXCEPTION", "CANCELLED", "REVERSED"})

STATE_MACHINES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "inbound_shipment": {
        "DRAFT": ("IN_TRANSIT", "ARRIVED", "CANCELLED"),
        "IN_TRANSIT": ("ARRIVED", "CANCELLED"),
        "ARRIVED": ("RECEIVING", "CANCELLED"),
        "RECEIVING": ("RECEIVED",),
        "RECEIVED": (),
        "CANCELLED": (),
    },
    "receiving_order": {
        "CREATED": ("IN_PROGRESS", "COMPLETED", "CANCELLED"),
        "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
        "COMPLETED": (),
        "CANCELLED": (),
    },
    "putaway_task": {
        "PENDING": ("ASSIGNED", "CANCELLED", "EXCEPTION"),
        "ASSIGNED": ("IN_PROGRESS", "PENDING", "CANCELLED", "EXCEPTION"),
        "IN_PROGRESS": ("COMPLETED", "CANCELLED", "EXCEPTION"),
        "COMPLETED": (),
        "EXCEPTION": ("PENDING", "CANCELLED"),
        "CANCELLED": (),
        "REVERSED": (),
    },
    "pick_task": {
        "PENDING": ("ASSIGNED", "CANCELLED"),
        "ASSIGNED": ("IN_PROGRESS", "PENDING", "CANCELLED"),
        "IN_PROGRESS": ("PICKED", "SHORT", "PARTIAL", "CANCELLED"),
        "PICKED": ("REVERSED",),
        "SHORT": ("REVERSED",),
        "PARTIAL": ("REVERSED",),
        "CANCELLED": (),
        "REVERSED": (),
    },
    "wave": {
        "CREATED": ("RELEASED", "CANCELLED"),
        "RELEASED": ("IN_PROGRESS", "COMPLETED", "CANCELLED"),
        "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
        "COMPLETED": (),
        "CANCELLED": (),
    },
    "pack_task": {
        "PENDING": ("IN_PROGRESS", "CANCELLED"),
        "IN_PROGRESS": ("PACKED", "CANCELLED"),
        "PACKED": (),
        "CANCELLED": (),
    },
    "shipment": {
        "DRAFT": ("READY", "SHIPPED", "CANCELLED"),
        "READY": ("SHIPPED", "CANCELLED"),
        "SHIPPED": ("DELIVERED",),
        "DELIVERED": (),
        "CANCELLED": (),
    },
    "ship_manifest": {
        "CREATED": ("SENT", "ERROR"),
        "SENT": ("CONFIRMED", "ERROR"),
        "CONFIRMED": (),
        "ERROR": (),
    },
    "replenishment_plan": {
        "DRAFT": ("APPROVED",),
        "APPROVED": ("GENERATING", "GENERATED"),
        "GENERATING": ("GENERATED",),
        "GENERATED": ("CLOSED",),
        "CLOSED": (),
    },
    "replenishment_task": {
        "PENDING": ("ASSIGNED", "COMPLETED", "CANCELLED"),
        "ASSIGNED": ("IN_PROGRESS", "PENDING", "COMPLETED", "CANCELLED"),
        "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
        "COMPLETED": (),
        "EXCEPTION": ("PENDING", "CANCELLED"),
        "CANCELLED": (),
        "REVERSED": (),
    },
    "status_change_task": {
        "PENDING": ("ASSIGNED", "COMPLETED", "CANCELLED"),
        "ASSIGNED": ("IN_PROGRESS", "PENDING", "COMPLETED", "CANCELLED"),
        "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
        "COMPLETED": (),
        "EXCEPTION": ("PENDING", "CANCELLED"),
        "CANCELLED": (),
        "REVERSED": (),
    },
}

#: Statuses in which a document is still writable/actionable by WMS.
OPEN_TASK_STATUSES = frozenset({"PENDING", "ASSIGNED", "IN_PROGRESS"})

ENUM_BY_MACHINE: Mapping[str, tuple[str, ...]] = {
    "inbound_shipment": INBOUND_SHIPMENT_STATUSES,
    "receiving_order": RECEIVING_ORDER_STATUSES,
    "putaway_task": TASK_STATUSES,
    "pick_task": PICK_TASK_STATUSES,
    "wave": WAVE_STATUSES,
    "pack_task": PACK_TASK_STATUSES,
    "shipment": SHIPMENT_STATUSES,
    "ship_manifest": SHIP_MANIFEST_STATUSES,
    "replenishment_plan": REPLENISHMENT_PLAN_STATUSES,
    "replenishment_task": TASK_STATUSES,
    "status_change_task": TASK_STATUSES,
}


def validate_transition(machine: str, current: str, target: str) -> str:
    """Validate one state transition; returns ``target`` or raises ``INVALID_STATE_TRANSITION`` (409)."""
    transitions = STATE_MACHINES.get(machine)
    if transitions is None:
        raise InvalidStateTransition(f"unknown state machine {machine!r}")
    current = str(current or "").upper()
    target = str(target or "").upper()
    if current not in transitions:
        raise InvalidStateTransition(f"{machine} has unknown status {current!r}")
    if target not in transitions[current]:
        raise InvalidStateTransition(
            f"{machine} cannot transition {current} -> {target}",
            details={"machine": machine, "current": current, "target": target},
        )
    return target


def can_transition(machine: str, current: str, target: str) -> bool:
    try:
        validate_transition(machine, current, target)
    except InvalidStateTransition:
        return False
    return True


def pick_rollup(completed_lines: list[str]) -> str:
    """Roll up pick-task line statuses to the task status (DM-WMS-05)."""
    statuses = {str(status).upper() for status in completed_lines}
    if not statuses:
        return "PENDING"
    if statuses <= {"PICKED"}:
        return "PICKED"
    if statuses <= {"ZERO", "CANCELLED"}:
        return "SHORT"
    return "PARTIAL"


__all__ = [
    "ENUM_BY_MACHINE",
    "OPEN_TASK_STATUSES",
    "STATE_MACHINES",
    "TASK_TERMINAL",
    "can_transition",
    "pick_rollup",
    "validate_transition",
]
