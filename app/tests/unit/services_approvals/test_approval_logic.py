"""Pure approval transition/step/SoD helpers (no DB; task-47)."""

from __future__ import annotations

import pytest

from app.core.approvals import (
    StepSpec,
    is_terminal,
    match_condition,
    next_position,
    normalize_steps,
    request_hash,
    required_approvals_for,
    step_decision_state,
    step_is_authorized,
)


def test_normalize_steps_sequential_and_parallel() -> None:
    steps = normalize_steps([
        {"step": 2, "name": "Second"},
        {"step": 1, "name": "First", "roles": ["admin"]},
        {"step": 3, "mode": "PARALLEL", "user_ids": ["u1", "u2"]},
    ])
    assert [step.step for step in steps] == [1, 2, 3]
    assert steps[0].roles == frozenset({"admin"})
    assert required_approvals_for(steps[2]) == 2
    assert normalize_steps({"steps": [{"roles": ["approver"]}]})[0].step == 1
    assert normalize_steps(None) == ()


def test_normalize_steps_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_steps("nope")
    with pytest.raises(ValueError):
        normalize_steps([{"mode": "ROUND_ROBIN"}])


def test_condition_matching() -> None:
    assert match_condition(None, {"amount_base": 10})
    assert match_condition({}, {})
    assert match_condition({"amount_base": {"gte": 1000}}, {"amount_base": 1500})
    assert not match_condition({"amount_base": {"gte": 1000}}, {"amount_base": 999})
    assert match_condition({"doc_type": "PO"}, {"doc_type": "PO"})
    assert not match_condition({"doc_type": "PO"}, {"doc_type": "SO"})
    assert not match_condition({"missing": 1}, {})


def test_step_authorization() -> None:
    role_step = StepSpec(step=1, roles=frozenset({"approver"}))
    user_step = StepSpec(step=1, user_ids=frozenset({"u1"}))
    open_step = StepSpec(step=1)
    assert step_is_authorized(role_step, "u1", ["approver"])
    assert not step_is_authorized(role_step, "u1", ["viewer"])
    assert step_is_authorized(user_step, "u1", [])
    assert not step_is_authorized(user_step, "u2", [])
    assert step_is_authorized(open_step, "anyone", [])


def test_step_decision_states() -> None:
    seq = StepSpec(step=1)
    parallel = StepSpec(step=1, mode="PARALLEL", user_ids=frozenset({"u1", "u2"}))
    assert step_decision_state(seq, approvals=0, rejects=0, delegated=False, already_decided=False) == "PENDING"
    assert step_decision_state(seq, approvals=1, rejects=0, delegated=False, already_decided=True) == "APPROVED"
    assert step_decision_state(parallel, approvals=1, rejects=0, delegated=False, already_decided=True) == "PENDING"
    assert step_decision_state(parallel, approvals=2, rejects=0, delegated=False, already_decided=True) == "APPROVED"
    assert step_decision_state(seq, approvals=0, rejects=1, delegated=False, already_decided=True) == "REJECTED"
    assert step_decision_state(seq, approvals=0, rejects=0, delegated=True, already_decided=True) == "DELEGATED"


def test_terminal_and_next_position() -> None:
    assert is_terminal("APPROVED") and is_terminal("REJECTED") and is_terminal("CANCELLED")
    assert not is_terminal("PENDING")
    assert next_position(2, 1) == 2
    assert next_position(2, 2) is None


def test_request_hash_stable_and_body_sensitive() -> None:
    body = {"decision": "APPROVE", "actor_id": "u1"}
    assert request_hash(body) == request_hash(dict(reversed(list(body.items()))))
    assert request_hash(body) != request_hash({**body, "decision": "REJECT"})
