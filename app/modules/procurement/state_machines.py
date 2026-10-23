"""FS-08 procurement document state machines (DATA-MODEL §19; DM-GLOBAL-08).

Pure transition tables shared by the service and the exit-gate invariant test
``app/tests/invariants/test_state_machines_procurement.py``. A transition not listed here is rejected
with ``409 INVALID_STATE_TRANSITION``; terminal states have no outgoing edges.

The service keeps some additional business guards (open quantities, approvals, dependent documents);
this module is the *declarative skeleton* those guards refine.
"""

from __future__ import annotations

from collections.abc import Mapping

ALLOWED_TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "purchase_requisition": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "REJECTED", "CANCELLED"}),
        "APPROVED": frozenset({"ORDERED", "CANCELLED"}),
        "REJECTED": frozenset({"CANCELLED"}),
        "ORDERED": frozenset(),
        "CANCELLED": frozenset(),
    },
    "purchase_request": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
        "APPROVED": frozenset({"ORDERED", "CANCELLED"}),
        "ORDERED": frozenset(),
        "CANCELLED": frozenset(),
    },
    "rfq": {
        "DRAFT": frozenset({"OPEN", "CLOSED", "CANCELLED"}),
        "OPEN": frozenset({"CLOSED", "AWARDED", "CANCELLED"}),
        "CLOSED": frozenset({"AWARDED", "CANCELLED"}),
        "AWARDED": frozenset(),
        "CANCELLED": frozenset(),
    },
    "rfq_response": {
        "RECEIVED": frozenset({"UNDER_REVIEW", "AWARDED", "REJECTED"}),
        "UNDER_REVIEW": frozenset({"AWARDED", "REJECTED"}),
        "AWARDED": frozenset(),
        "REJECTED": frozenset(),
    },
    "vendor_contract": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
        "APPROVED": frozenset({"ACTIVE", "CLOSED", "CANCELLED"}),
        "ACTIVE": frozenset({"EXPIRED", "CLOSED"}),
        "EXPIRED": frozenset({"CLOSED"}),
        "CLOSED": frozenset(),
        "CANCELLED": frozenset(),
    },
    "purchase_order": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "DRAFT", "CANCELLED"}),
        "APPROVED": frozenset({"PENDING_RECEIPT", "PARTIALLY_RECEIVED", "CANCELLED"}),
        "PENDING_RECEIPT": frozenset({"PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"}),
        "PARTIALLY_RECEIVED": frozenset({"PARTIALLY_RECEIVED", "RECEIVED", "BILLED", "CLOSED"}),
        "RECEIVED": frozenset({"PARTIALLY_RECEIVED", "RECEIVED", "BILLED", "CLOSED"}),
        "BILLED": frozenset({"PARTIALLY_RECEIVED", "RECEIVED", "BILLED", "CLOSED"}),
        "CLOSED": frozenset(),
        "CANCELLED": frozenset({"VOID"}),
        "VOID": frozenset(),
    },
    "purchase_order_release": {
        "DRAFT": frozenset({"RELEASED", "CLOSED"}),
        "RELEASED": frozenset({"CLOSED"}),
        "CLOSED": frozenset(),
    },
    "item_receipt": {
        "DRAFT": frozenset({"PENDING_POSTING", "POSTED", "VOID"}),
        "PENDING_POSTING": frozenset({"POSTED", "VOID"}),
        "POSTED": frozenset({"REVERSED"}),
        "REVERSED": frozenset(),
        "VOID": frozenset(),
    },
    "vendor_bill": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
        "APPROVED": frozenset({"OPEN", "CANCELLED"}),
        "OPEN": frozenset({"PAID"}),
        "PAID": frozenset(),
        "CANCELLED": frozenset(),
        "VOID": frozenset(),
    },
    "vendor_credit": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "OPEN", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
        "APPROVED": frozenset({"OPEN", "CANCELLED"}),
        "OPEN": frozenset({"APPLIED"}),
        "APPLIED": frozenset(),
        "CANCELLED": frozenset(),
        "VOID": frozenset(),
    },
    "vendor_bill_match": {
        "PENDING": frozenset({"MATCHED", "EXCEPTION"}),
        "MATCHED": frozenset({"PENDING"}),
        "EXCEPTION": frozenset({"OVERRIDDEN", "EXCEPTION", "PENDING"}),
        "OVERRIDDEN": frozenset({"PENDING"}),
    },
    "vendor_return_authorization": {
        "DRAFT": frozenset({"PENDING_APPROVAL", "APPROVED", "CANCELLED"}),
        "PENDING_APPROVAL": frozenset({"APPROVED", "CANCELLED"}),
        "APPROVED": frozenset({"SHIPPED", "CANCELLED"}),
        "SHIPPED": frozenset({"CREDITED", "CLOSED"}),
        "CREDITED": frozenset({"CLOSED"}),
        "CLOSED": frozenset(),
        "CANCELLED": frozenset(),
    },
    "special_order_link": {
        "OPEN": frozenset({"FULFILLED", "CANCELLED"}),
        "FULFILLED": frozenset(),
        "CANCELLED": frozenset(),
    },
}

TERMINAL_STATES: Mapping[str, frozenset[str]] = {
    "purchase_order": frozenset({"CLOSED", "VOID"}),
    "item_receipt": frozenset({"REVERSED", "VOID"}),
    "vendor_bill": frozenset({"PAID", "CANCELLED", "VOID"}),
    "vendor_credit": frozenset({"APPLIED", "CANCELLED", "VOID"}),
    "vendor_return_authorization": frozenset({"CLOSED", "CANCELLED"}),
    "special_order_link": frozenset({"FULFILLED", "CANCELLED"}),
}


class TransitionError(ValueError):
    code = "INVALID_STATE_TRANSITION"


def can_transition(aggregate: str, current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(aggregate, {}).get(current, frozenset())


def require_transition(aggregate: str, current: str, target: str) -> None:
    if target == current:
        return
    if not can_transition(aggregate, current, target):
        raise TransitionError(f"{aggregate} cannot transition {current} -> {target}")


def next_states(aggregate: str, current: str) -> frozenset[str]:
    return ALLOWED_TRANSITIONS.get(aggregate, {}).get(current, frozenset())


def is_terminal(aggregate: str, status: str) -> bool:
    return status in TERMINAL_STATES.get(aggregate, frozenset())


__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "TransitionError",
    "can_transition",
    "is_terminal",
    "next_states",
    "require_transition",
]
