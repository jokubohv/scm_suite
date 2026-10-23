"""Pure FS-09 state machines (DATA-MODEL §19): work orders, WIP transactions, builds/unbuilds,
outsourced operations, ECOs, scheduling runs and compatibility rule sets.

No I/O: the service layer loads the current status, applies :func:`require_transition`, then persists.
"""

from __future__ import annotations

from app.modules.manufacturing.errors import InvalidStateTransition

WORK_ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    "PLANNED": frozenset({"FIRM_PLANNED", "RELEASED", "CANCELLED", "VOID"}),
    "FIRM_PLANNED": frozenset({"RELEASED", "CANCELLED", "VOID"}),
    "RELEASED": frozenset({"IN_PROCESS", "BUILT", "CLOSED", "VOID"}),
    "IN_PROCESS": frozenset({"BUILT", "RELEASED", "VOID"}),
    "BUILT": frozenset({"CLOSED", "RELEASED"}),
    "CLOSED": frozenset({"RELEASED"}),  # documented reopen for non-WIP only (service guard)
    "CANCELLED": frozenset(),
    "VOID": frozenset(),
}
WIP_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"POSTED", "VOID"}),
    "POSTED": frozenset({"REVERSED"}),
    "REVERSED": frozenset(),
    "VOID": frozenset(),
}
BUILD_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"POSTED", "VOID"}),
    "POSTED": frozenset({"REVERSED"}),
    "REVERSED": frozenset(),
    "VOID": frozenset(),
}
UNBUILD_TRANSITIONS: dict[str, frozenset[str]] = {**BUILD_TRANSITIONS}
OUTSOURCED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PLANNED": frozenset({"ORDERED", "CANCELLED"}),
    "ORDERED": frozenset({"IN_PROCESS", "CANCELLED"}),
    "IN_PROCESS": frozenset({"RECEIVED", "CANCELLED"}),
    "RECEIVED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
    "CANCELLED": frozenset(),
}
ECO_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"PENDING_APPROVAL", "CANCELLED"}),
    "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
    "APPROVED": frozenset({"IMPLEMENTED", "CANCELLED"}),
    "IMPLEMENTED": frozenset(),
    "CANCELLED": frozenset(),
}
SCHEDULING_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"RUNNING", "CANCELLED"}),
    "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
    "SUCCEEDED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}
COMPAT_RULE_SET_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"ACTIVE", "RETIRED"}),
    "ACTIVE": frozenset({"RETIRED"}),
    "RETIRED": frozenset(),
}

#: Reopen is only legal for non-WIP work orders (DATA-MODEL DM-MFG-07 / §19).
REOPEN_ALLOWED_WO_TYPES = frozenset({"STANDARD", "SPECIAL_ORDER", "OUTSOURCED"})

MACHINES: dict[str, dict[str, frozenset[str]]] = {
    "work_order": WORK_ORDER_TRANSITIONS,
    "wip_transaction": WIP_TRANSITIONS,
    "assembly_build": BUILD_TRANSITIONS,
    "assembly_unbuild": UNBUILD_TRANSITIONS,
    "outsourced_operation": OUTSOURCED_TRANSITIONS,
    "eco": ECO_TRANSITIONS,
    "scheduling_run": SCHEDULING_RUN_TRANSITIONS,
    "compat_rule_set": COMPAT_RULE_SET_TRANSITIONS,
}


def require_transition(machine: str, current: str, target: str) -> None:
    transitions = MACHINES[machine]
    allowed = transitions.get(current)
    if allowed is None:
        raise InvalidStateTransition(f"unknown {machine} state {current!r}")
    if target not in allowed:
        raise InvalidStateTransition(
            f"{machine} transition {current} -> {target} is not permitted",
            details={"current": current, "target": target, "allowed": sorted(allowed)},
        )


def is_same_state(current: str, target: str) -> bool:
    return str(current).upper() == str(target).upper()


def require_reopen_allowed(wo_type: str) -> None:
    if str(wo_type).upper() not in REOPEN_ALLOWED_WO_TYPES:
        raise InvalidStateTransition("only non-WIP work orders can be reopened", details={"wo_type": wo_type})


__all__ = [
    "MACHINES",
    "REOPEN_ALLOWED_WO_TYPES",
    "is_same_state",
    "require_reopen_allowed",
    "require_transition",
]
