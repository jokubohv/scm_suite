"""FS-07 DM-CNT-03 invariant: count variance equals counted − snapshot and T-20 deltas match it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.counts.engine import (
    build_t20_movements,
    derive_counted_snapshot,
    line_variance,
    variance_totals,
)


def _line(snapshot: str, counted: str | None = None, **extra):
    row = {
        "id": str(uuid4()),
        "inventory_count_id": str(uuid4()),
        "item_id": str(uuid4()),
        "location_id": str(uuid4()),
        "inventory_status_id": str(uuid4()),
        "uom_id": str(uuid4()),
        "ownership": "OWNED",
        "snapshot_qty": Decimal(snapshot),
        "snapshot_value_base": Decimal("40"),
        "counted_qty": None if counted is None else Decimal(counted),
    }
    if counted is not None:
        row["variance_qty"] = line_variance(counted, snapshot)
    row.update(extra)
    return row


def _count() -> dict:
    return {
        "id": uuid4(),
        "count_number": "CNT-1",
        "count_date": date(2026, 10, 23),
        "status": "APPROVED",
        "count_type": "MANUAL",
    }


def test_variance_matches_counted_minus_snapshot_for_every_rounding_case() -> None:
    cases = [("10", "7"), ("0.333333333333", "0.333333333334"), ("10", "10"), ("10", "0"), ("1.5", "2.25")]
    for snapshot, counted in cases:
        line = _line(snapshot, counted)
        assert line["variance_qty"] == Decimal(counted) - Decimal(snapshot)
        assert line_variance(counted, snapshot) == Decimal(counted) - Decimal(snapshot)


def test_t20_movements_delta_equals_counted_minus_snapshot() -> None:
    lines = [_line("10", "7"), _line("5", "8"), _line("3", "3")]
    movements = build_t20_movements(count=_count(), lines=lines)
    assert len(movements) == 2  # zero variance posts nothing
    assert movements[0]["qty"] == Decimal("-3.000000000000")
    assert movements[1]["qty"] == Decimal("3.000000000000")
    for movement, line in zip(movements, [lines[0], lines[1]], strict=False):
        assert movement["qty"] == line_variance(line["counted_qty"], line["snapshot_qty"])
        assert movement["movement_type"] == "COUNT_VARIANCE"


def test_variance_totals_are_signed_sums() -> None:
    lines = [_line("10", "7"), _line("5", "8"), _line("3", None)]
    totals = variance_totals(lines)
    assert totals["variance_qty_total"] == Decimal("0.000000000000")
    assert totals["counted_lines"] == 2


def test_derived_counted_snapshot_applies_latest_recount() -> None:
    line_a = _line("10", None)
    line_a["id"] = str(uuid4())
    recount = [
        {"inventory_count_line_id": line_a["id"], "attempt": 1, "counted_qty": Decimal("8"), "variance_qty": Decimal("-2")},
        {"inventory_count_line_id": line_a["id"], "attempt": 2, "counted_qty": Decimal("9"), "variance_qty": Decimal("-1")},
    ]
    merged = derive_counted_snapshot([line_a], recount)
    assert merged[0]["counted_qty"] == Decimal("9")
    assert line_variance(merged[0]["counted_qty"], merged[0]["snapshot_qty"]) == Decimal("-1")


def test_variance_requires_counted_quantity_for_posting() -> None:
    line = _line("10", None)
    assert line_variance(None, "10") is None
    movements = build_t20_movements(count=_count(), lines=[line])
    assert movements == []
