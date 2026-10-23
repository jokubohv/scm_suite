"""FS-09 pure unit tests: state machines, component quantity math and serialization helpers."""

from __future__ import annotations

import pytest

from app.modules.manufacturing import state_machines
from app.modules.manufacturing.errors import (
    InvalidStateTransition,
    ManufacturingError,
    ValidationFailed,
)
from app.modules.manufacturing.service import component_requirement, component_requirement_rounded, qty, serialize


def test_work_order_state_machine_allows_documented_transitions() -> None:
    for current, target in (
        ("PLANNED", "FIRM_PLANNED"),
        ("PLANNED", "RELEASED"),
        ("FIRM_PLANNED", "RELEASED"),
        ("RELEASED", "IN_PROCESS"),
        ("IN_PROCESS", "BUILT"),
        ("BUILT", "CLOSED"),
        ("CLOSED", "RELEASED"),
    ):
        state_machines.require_transition("work_order", current, target)
    for current, target in (
        ("PLANNED", "BUILT"),
        ("RELEASED", "PLANNED"),
        ("VOID", "RELEASED"),
        ("CANCELLED", "RELEASED"),
    ):
        with pytest.raises(InvalidStateTransition):
            state_machines.require_transition("work_order", current, target)


def test_wip_build_unbuild_eco_transitions() -> None:
    state_machines.require_transition("wip_transaction", "DRAFT", "POSTED")
    state_machines.require_transition("wip_transaction", "POSTED", "REVERSED")
    with pytest.raises(InvalidStateTransition):
        state_machines.require_transition("wip_transaction", "REVERSED", "POSTED")
    state_machines.require_transition("assembly_build", "DRAFT", "POSTED")
    state_machines.require_transition("assembly_unbuild", "POSTED", "REVERSED")
    state_machines.require_transition("eco", "DRAFT", "PENDING_APPROVAL")
    state_machines.require_transition("eco", "PENDING_APPROVAL", "APPROVED")
    state_machines.require_transition("eco", "APPROVED", "IMPLEMENTED")
    state_machines.require_transition("compat_rule_set", "DRAFT", "ACTIVE")
    state_machines.require_transition("compat_rule_set", "ACTIVE", "RETIRED")
    with pytest.raises(InvalidStateTransition):
        state_machines.require_transition("compat_rule_set", "RETIRED", "ACTIVE")


def test_wip_reopen_only_for_typed_non_wip_work_orders() -> None:
    state_machines.require_reopen_allowed("STANDARD")
    with pytest.raises(InvalidStateTransition):
        state_machines.require_reopen_allowed("WIP")


def test_component_requirement_yield_roundup() -> None:
    assert component_requirement("1", "10") == pytest.approx(10)
    assert component_requirement("1", "10", yield_percent="80") == pytest.approx(12.5)
    assert component_requirement_rounded("1", "10", yield_percent="80") == pytest.approx(13)
    assert component_requirement("2", "5", yield_percent="100") == pytest.approx(10)
    with pytest.raises(ValidationFailed):
        component_requirement("1", "1", yield_percent="0")


def test_qty_and_serialize_helpers() -> None:
    assert qty("2.5") == qty(2.5)
    payload = serialize(
        {"id": __import__("uuid").uuid4(), "amount": __import__("decimal").Decimal("1.5"), "nested": {"x": 1}}
    )
    assert "__uuid__" not in str(payload) and isinstance(payload["id"], str) and payload["amount"] == "1.5"


def test_errors_map_to_problem_json() -> None:
    problem = ManufacturingError("boom", details={"k": "v"}).to_problem()
    assert problem["status"] == 400 and problem["code"] == "MANUFACTURING_ERROR" and problem["details"] == {"k": "v"}
    assert InvalidStateTransition("bad").http_status == 409
