"""FS-11 strategy engine unit tests (putaway rule matching, pick ordering, expiry/block filters)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.modules.wms import strategies
from app.modules.wms.errors import ValidationFailed


def test_putaway_rule_matching_prefers_first_matching_sequence() -> None:
    rules = [
        {"sequence": 10, "zone_id": None, "item_process_group_id": "group-a", "bin_type": "STORAGE",
         "max_qty_per_bin": None, "is_active": True},
        {"sequence": 20, "zone_id": None, "item_process_group_id": None, "bin_type": "PICKING",
         "max_qty_per_bin": None, "is_active": True},
    ]
    bins = [
        {"bin_id": "bin-1", "bin_code": "A-01", "zone_id": "zone-1", "bin_type": "PICKING"},
        {"bin_id": "bin-2", "bin_code": "A-02", "zone_id": "zone-1", "bin_type": "STORAGE"},
    ]
    resolved = strategies.resolve_putaway_bin(
        rules, bins, item_process_group_id="group-a", qty=Decimal("5"),
    )
    assert resolved is not None and resolved["bin_id"] == "bin-2"


def test_putaway_max_qty_per_bin_rejects_full_bins() -> None:
    rules = [{"sequence": 1, "zone_id": None, "item_process_group_id": None, "bin_type": "STORAGE",
              "max_qty_per_bin": Decimal("10"), "is_active": True}]
    bins = [
        {"bin_id": "bin-1", "bin_code": "A-01", "zone_id": "z", "bin_type": "STORAGE"},
        {"bin_id": "bin-2", "bin_code": "A-02", "zone_id": "z", "bin_type": "STORAGE"},
    ]
    resolved = strategies.resolve_putaway_bin(
        rules, bins, item_process_group_id=None, qty=Decimal("6"),
        on_hand_by_bin={"bin-1": Decimal("8"), "bin-2": Decimal("1")},
    )
    assert resolved is not None and resolved["bin_id"] == "bin-2"


def test_putaway_no_candidate_returns_none() -> None:
    rules = [{"sequence": 1, "zone_id": None, "item_process_group_id": "other", "bin_type": "STORAGE",
              "max_qty_per_bin": None, "is_active": True}]
    bins = [{"bin_id": "bin-1", "bin_code": "A-01", "zone_id": "z", "bin_type": "STORAGE"}]
    assert strategies.resolve_putaway_bin(rules, bins, item_process_group_id="group-x", qty=Decimal("1")) is None


def test_fefo_order_and_expiry_guard() -> None:
    positions = [
        {"id": "p1", "bin_id": "b1", "bin_code": "B", "lot_id": "lot-old", "expiry_date": date(2027, 1, 1)},
        {"id": "p2", "bin_id": "b2", "bin_code": "A", "lot_id": "lot-new", "expiry_date": date(2028, 1, 1)},
        {"id": "p3", "bin_id": "b3", "bin_code": "C", "lot_id": "lot-expired", "expiry_date": date(2026, 1, 1)},
    ]
    pickable, expired = strategies.filter_expired(positions, as_of=date(2026, 10, 23), guard_enabled=True)
    assert [row["lot_id"] for row in expired] == ["lot-expired"]
    ordered = strategies.sort_positions(pickable, direction="FEFO")
    assert [row["lot_id"] for row in ordered] == ["lot-old", "lot-new"]


def test_blocked_bins_filtered_from_candidates() -> None:
    positions = [{"bin_id": "b1", "bin_code": "A"}, {"bin_id": "b2", "bin_code": "B"}]
    filtered = strategies.filter_blocked(positions, ["b1"])
    assert [row["bin_id"] for row in filtered] == ["b2"]


def test_fifo_lifo_bin_sequence_ordering() -> None:
    positions = [
        {"id": "p2", "bin_id": "b2", "bin_code": "B", "lot_created_at": "2026-01-02"},
        {"id": "p1", "bin_id": "b1", "bin_code": "A", "lot_created_at": "2026-01-01"},
    ]
    assert [row["bin_id"] for row in strategies.sort_positions(positions, direction="FIFO")] == ["b1", "b2"]
    assert [row["bin_id"] for row in strategies.sort_positions(positions, direction="LIFO")] == ["b2", "b1"]
    assert [row["bin_code"] for row in strategies.sort_positions(positions, direction="BIN_SEQUENCE")] == ["A", "B"]
    with pytest.raises(ValidationFailed):
        strategies.sort_positions(positions, direction="RANDOM")


def test_pick_quantities_reject_over_pick() -> None:
    picked, short = strategies.pick_quantities(Decimal("5"), Decimal("3"))
    assert (picked, short) == (Decimal("3"), Decimal("2"))
    with pytest.raises(ValidationFailed):
        strategies.pick_quantities(Decimal("5"), Decimal("6"))
    with pytest.raises(ValidationFailed):
        strategies.pick_quantities(Decimal("5"), Decimal("-1"))
    with pytest.raises(ValidationFailed):
        strategies.pick_quantities(Decimal("0"), Decimal("0"))


def test_wave_decomposition_groups_by_source_and_zone() -> None:
    lines = [
        {"source_type": "SALES_ORDER", "source_ref": "SO-1", "item_id": "i1"},
        {"source_type": "SALES_ORDER", "source_ref": "SO-1", "item_id": "i2"},
        {"source_type": "SALES_ORDER", "source_ref": "SO-2", "item_id": "i1"},
    ]
    tasks = strategies.decompose_pick_tasks(lines)
    assert len(tasks) == 2
    zone_lines = [dict(line, zone_id="zone-1") for line in lines]
    zone_tasks = strategies.decompose_pick_tasks(zone_lines)
    assert len(zone_tasks) == 1
    assert len(zone_tasks[0]["lines"]) == 3


def test_assignment_strategies() -> None:
    candidates = [{"id": "u1"}, {"id": "u2"}]
    assert strategies.assign_worker("ROUND_ROBIN", candidates=candidates) == {"id": "u1"}
    least = strategies.assign_worker(
        "LEAST_OPEN", candidates=candidates, open_counts={"u1": 4, "u2": 1},
    )
    assert least == {"id": "u2"}
    assert strategies.assign_worker("MANUAL", candidates=candidates) is None
