"""Invariant harness base + generic ledger invariants behave deterministically."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tests.utils.invariants import (
    InvariantFailure,
    InvariantHarness,
    assert_balance_equals_sum_lines,
    assert_commitment_le_on_hand,
    assert_qty_conservation,
    assert_serial_projection,
    assert_value_conservation,
)


def test_harness_runs_registered_checks_in_order() -> None:
    harness = InvariantHarness("demo")
    calls: list[str] = []

    @harness.check("INV-01")
    def _first() -> None:
        calls.append("first")

    @harness.check("INV-02")
    def _second() -> None:
        calls.append("second")

    report = harness.run()
    assert report.passed
    assert calls == ["first", "second"]
    assert harness.names == ("INV-01", "INV-02")
    assert report.covered == {"INV-01", "INV-02"}


def test_harness_records_failures_and_assert_ok_raises() -> None:
    harness = InvariantHarness("demo")

    @harness.check("INV-01")
    def _ok() -> None:
        pass

    @harness.check("INV-18")
    def _bad() -> None:
        raise AssertionError("serial mismatch")

    report = harness.run()
    assert not report.passed
    assert [result.name for result in report.failures] == ["INV-18"]
    assert "serial mismatch" in report.failures[0].detail
    with pytest.raises(InvariantFailure):
        harness.assert_ok()


def test_harness_fail_fast_raises_immediately() -> None:
    harness = InvariantHarness("demo", fail_fast=True)
    harness.register("INV-01", lambda: (_ for _ in ()).throw(AssertionError("boom")))
    with pytest.raises(InvariantFailure):
        harness.run()


def test_harness_requires_plan_invariants() -> None:
    harness = InvariantHarness("demo", required=("INV-01", "INV-18"))
    harness.register("INV-01", lambda: None)
    with pytest.raises(InvariantFailure):
        harness.run()
    harness.register("INV-18", lambda: None)
    assert harness.run().passed


def test_duplicate_check_name_rejected() -> None:
    harness = InvariantHarness("demo")
    harness.register("INV-01", lambda: None)
    with pytest.raises(ValueError):
        harness.register("INV-01", lambda: None)


def test_qty_and_value_conservation() -> None:
    internal_move = [{"qty_base": "5"}, {"qty_base": "-5"}]
    assert_qty_conservation(internal_move)
    receipt = [{"qty_base": "5"}]
    assert_qty_conservation(receipt, expected_net="5")
    with pytest.raises(AssertionError):
        assert_qty_conservation([{"qty_base": "5"}])

    assert_value_conservation([{"value_base": "25"}, {"value_base": "-25"}])
    assert_value_conservation([{"value_base": "-25"}], expected_net="-25")
    with pytest.raises(AssertionError):
        assert_value_conservation([{"value_base": "1"}], expected_net="2")


def test_balance_equals_sum_lines() -> None:
    lines = [
        {"item_id": "i", "location_id": "l", "inventory_status_id": "s", "qty_base": "3"},
        {"item_id": "i", "location_id": "l", "inventory_status_id": "s", "qty_base": "-1"},
    ]
    key = ("i", "l", "s", "OWNED", "ON_HAND", None, None)
    assert_balance_equals_sum_lines({key: Decimal("2")}, lines)
    with pytest.raises(AssertionError):
        assert_balance_equals_sum_lines({key: Decimal("3")}, lines)


def test_commitment_le_on_hand() -> None:
    key = ("i", "l", "s")
    assert_commitment_le_on_hand({key: 2}, {key: 5})
    assert_commitment_le_on_hand({key: 7}, {key: 5}, allow_backorder=True)
    with pytest.raises(AssertionError):
        assert_commitment_le_on_hand({key: 7}, {key: 5})


def test_serial_projection_invariant() -> None:
    history = [
        {"serial_id": "s1", "occurred_at": "2026-01-01T00:00:00Z", "event": "RECEIVED"},
        {"serial_id": "s1", "occurred_at": "2026-01-02T00:00:00Z", "event": "MOVED"},
    ]
    serials = {"s1": {"state": "AVAILABLE"}}
    detail = [{"serial_id": "s1", "qty_base": "1"}]
    assert_serial_projection(serials, history, detail)
    with pytest.raises(AssertionError):
        assert_serial_projection({"s2": {"state": "AVAILABLE"}}, history, detail)
    with pytest.raises(AssertionError):
        assert_serial_projection(serials, [], [])


def test_report_contains_plan_invariant_ids() -> None:
    harness = InvariantHarness("FS-04 invariants")
    for invariant_id in ("INV-01", "INV-02", "INV-03"):
        harness.register(invariant_id, lambda: None)
    report = harness.run()
    rendered = {result.name: result.to_dict() for result in report.results}
    assert set(rendered) == {"INV-01", "INV-02", "INV-03"}
    assert all(result["passed"] for result in rendered.values())
