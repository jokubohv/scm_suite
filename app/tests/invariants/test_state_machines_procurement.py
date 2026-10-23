"""Invariant DM-GLOBAL-08 (FS-08 exit gate): procurement documents only use declared transitions."""

from __future__ import annotations

import pytest

from app.modules.procurement.state_machines import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    can_transition,
    is_terminal,
    next_states,
    require_transition,
)
from app.modules.procurement.testing import seed_procurement_world

pytestmark = pytest.mark.integration


def test_transition_tables_are_consistent() -> None:
    aggregates = set(ALLOWED_TRANSITIONS)
    assert aggregates == {
        "purchase_requisition", "purchase_request", "rfq", "rfq_response", "vendor_contract",
        "purchase_order", "purchase_order_release", "item_receipt", "vendor_bill", "vendor_credit",
        "vendor_bill_match", "vendor_return_authorization", "special_order_link",
    }
    for aggregate, states in ALLOWED_TRANSITIONS.items():
        known = set(states)
        for _source, targets in states.items():
            assert targets <= known, f"{aggregate}: transition target outside declared states"
        for terminal in TERMINAL_STATES.get(aggregate, frozenset()):
            assert terminal in known, (aggregate, terminal)
            assert next_states(aggregate, terminal) == frozenset(), f"{aggregate} terminal {terminal} has outgoing edges"


def test_require_transition_rejects_invalid() -> None:
    assert can_transition("purchase_order", "DRAFT", "PENDING_APPROVAL")
    assert not can_transition("purchase_order", "DRAFT", "CLOSED")
    with pytest.raises(TransitionError):
        require_transition("purchase_order", "DRAFT", "CLOSED")
    require_transition("purchase_order", "RECEIVED", "CLOSED")
    assert is_terminal("purchase_order", "CLOSED") and not is_terminal("purchase_order", "APPROVED")


async def test_service_rejects_invalid_document_transitions(db_session) -> None:
    world = await seed_procurement_world(db_session, code_prefix="INVSM")
    service = world["service"]
    po = await service.create_purchase_order({
        "vendor_id": str(world["vendor_id"]), "po_type": "STANDARD", "transaction_date": "2026-10-23",
        "lines": [{"item_id": str(world["item_id"]), "qty_ordered": 2, "unit_price": "5"}],
    }, actor_id=str(world["app_user_id"]))
    # DRAFT -> APPROVED via approve is allowed by the declarative table; DRAFT -> CLOSED is not.
    with pytest.raises(Exception) as exc:
        await service.reject_purchase_order(po["id"], "not pending", actor_id=str(world["app_user_id"]))
    assert getattr(exc.value, "code", "") == "INVALID_STATE_TRANSITION"
    with pytest.raises(Exception) as exc2:
        await service.close_purchase_order(po["id"], actor_id=str(world["app_user_id"]))
    assert getattr(exc2.value, "code", "") == "DEPENDENT_DOCUMENTS"
