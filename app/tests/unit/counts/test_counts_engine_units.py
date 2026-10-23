"""FS-07 unit tests: plan filters, state machines, tolerance policy, blind shaping, schedule, T-20."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.counts.engine import (
    CountPosition,
    CountSettings,
    FilterSpec,
    assert_count_transition,
    assert_line_transition,
    assert_plan_transition,
    blind_line,
    build_t20_command,
    evaluate_filters,
    evaluate_tolerance,
    line_variance,
    next_count_date,
    select_positions,
    shape_lines,
)
from app.modules.counts.errors import CountStateError


def _position(**overrides) -> CountPosition:
    defaults = dict(
        item_id=str(uuid4()), location_id=str(uuid4()), inventory_status_id=str(uuid4()),
        uom_id=str(uuid4()), qty_on_hand=Decimal("5"), value_base=Decimal("20"),
    )
    defaults.update(overrides)
    return CountPosition(**defaults)


def test_filters_include_exclude_and_excludes_win() -> None:
    position = _position()
    assert evaluate_filters([], position) is True
    assert evaluate_filters([FilterSpec("ITEM", value_uuid=position.item_id)], position) is True
    assert evaluate_filters([FilterSpec("ITEM", "EXCLUDE", value_uuid=position.item_id)], position) is False
    assert evaluate_filters(
        [FilterSpec("ITEM", value_uuid=position.item_id), FilterSpec("ITEM", "EXCLUDE", value_uuid=position.item_id)],
        position,
    ) is False
    assert evaluate_filters([FilterSpec("ITEM", value_uuid=str(uuid4()))], position) is False


def test_select_positions_zero_policy_and_cap() -> None:
    zero = _position(qty_on_hand=Decimal("0"))
    nonzero = _position(qty_on_hand=Decimal("3"))
    assert select_positions([zero, nonzero]) == [nonzero]
    assert len(select_positions([zero, nonzero], include_zero_qty=True)) == 2
    assert len(select_positions([zero, nonzero], include_zero_qty=True, max_lines=1)) == 1


def test_state_machines_declared_only() -> None:
    assert_count_transition("DRAFT", "OPEN")
    assert_count_transition("APPROVED", "POSTED")
    with pytest.raises(CountStateError):
        assert_count_transition("POSTED", "APPROVED")
    assert_plan_transition("ACTIVE", "PAUSED")
    with pytest.raises(CountStateError):
        assert_plan_transition("ARCHIVED", "ACTIVE")
    assert_line_transition("PENDING", "COUNTED")
    with pytest.raises(CountStateError):
        assert_line_transition("APPROVED", "COUNTED")


def test_tolerance_policy() -> None:
    lines = [
        {"id": "1", "counted_qty": Decimal("9"), "snapshot_qty": Decimal("10"), "variance_qty": Decimal("-1"), "snapshot_value_base": Decimal("40")},
    ]
    disabled = CountSettings(auto_approve_within_tolerance=False)
    assert evaluate_tolerance(lines, disabled).eligible is False
    tolerant = CountSettings(tolerance_qty=Decimal("2"), auto_approve_within_tolerance=True)
    assert evaluate_tolerance(lines, tolerant).eligible is True
    strict = CountSettings(tolerance_qty=Decimal("0.5"), auto_approve_within_tolerance=True)
    assert evaluate_tolerance(lines, strict).eligible is False
    assert any("qty_tolerance" in reason for reason in evaluate_tolerance(lines, strict).reasons)


def test_blind_shaping_hides_snapshot_fields() -> None:
    line = {"id": "l", "snapshot_qty": Decimal("3"), "counted_qty": Decimal("2"), "variance_qty": Decimal("-1")}
    shaped = blind_line(line)
    assert "snapshot_qty" not in shaped and "variance_qty" not in shaped
    assert shaped["counted_qty"] == Decimal("2")
    assert shape_lines([line], blind=False)[0] is not line


def test_next_count_date_derivation() -> None:
    settings = CountSettings(default_interval_days=30, abc_class_intervals={"A": 7})
    assert next_count_date(last_count_date=None, frequency="WEEKLY", settings=settings, as_of=date(2026, 10, 1))[0] == date(2026, 10, 8)
    assert next_count_date(last_count_date=date(2026, 9, 1), settings=settings)[1] == 30
    dated, interval, source = next_count_date(last_count_date=date(2026, 9, 1), abc_class="A", settings=settings)
    assert interval == 7 and source == "TENANT_DEFAULT"


def test_t20_command_shape() -> None:
    count = {"id": uuid4(), "count_number": "CNT-1", "count_date": date(2026, 10, 23)}
    line = {
        "id": str(uuid4()), "item_id": str(uuid4()), "location_id": str(uuid4()),
        "inventory_status_id": str(uuid4()), "uom_id": str(uuid4()), "ownership": "OWNED",
        "snapshot_qty": Decimal("10"), "counted_qty": Decimal("7"), "snapshot_value_base": Decimal("40"),
    }
    command = build_t20_command(count=count, lines=[line], reason_code_id=str(uuid4()))
    assert command["posting_type"] == "T-20"
    assert command["movements"][0]["qty"] == Decimal("-3.000000000000")
    assert command["movements"][0]["movement_type"] == "COUNT_VARIANCE"
    assert line_variance("7", "10") == Decimal("-3.000000000000")
