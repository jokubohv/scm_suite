"""FS-11 state machines + task roll-up unit tests."""

from __future__ import annotations

import pytest

from app.modules.wms import state_machines
from app.modules.wms.errors import InvalidStateTransition


def test_valid_task_lifecycle_transitions() -> None:
    state_machines.validate_transition("putaway_task", "PENDING", "ASSIGNED")
    state_machines.validate_transition("putaway_task", "ASSIGNED", "IN_PROGRESS")
    state_machines.validate_transition("putaway_task", "IN_PROGRESS", "COMPLETED")
    assert state_machines.can_transition("pick_task", "IN_PROGRESS", "PARTIAL")
    assert not state_machines.can_transition("pick_task", "COMPLETED", "IN_PROGRESS")


def test_terminal_states_are_closed() -> None:
    for machine, terminal in (
        ("inbound_shipment", "RECEIVED"),
        ("wave", "COMPLETED"),
        ("shipment", "DELIVERED"),
        ("putaway_task", "CANCELLED"),
        ("replenishment_plan", "CLOSED"),
    ):
        target = sorted(state_machines.STATE_MACHINES[machine].get(terminal, ()))
        assert target == [], (machine, terminal)


@pytest.mark.parametrize(
    "machine,current,target",
    [
        ("wave", "COMPLETED", "RELEASED"),
        ("shipment", "DRAFT", "DELIVERED"),
        ("pack_task", "PENDING", "PACKED"),
        ("inbound_shipment", "RECEIVED", "ARRIVED"),
    ],
)
def test_invalid_transitions_raise(machine: str, current: str, target: str) -> None:
    with pytest.raises(InvalidStateTransition):
        state_machines.validate_transition(machine, current, target)


def test_pick_rollup() -> None:
    assert state_machines.pick_rollup(["PICKED", "PICKED"]) == "PICKED"
    assert state_machines.pick_rollup(["PICKED", "PARTIAL"]) == "PARTIAL"
    assert state_machines.pick_rollup(["ZERO", "CANCELLED"]) == "SHORT"
    assert state_machines.pick_rollup(["PICKED", "ZERO"]) == "PARTIAL"


def test_unknown_machine_rejected() -> None:
    with pytest.raises(InvalidStateTransition):
        state_machines.validate_transition("unknown", "PENDING", "DONE")
