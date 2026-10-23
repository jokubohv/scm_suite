"""FS-10 INV-22 / DM-PLAN-05: ATP projection is clamped at zero with persisted shortage columns."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.modules.replenishment.engine import AtpEvent, build_atp_projection

HORIZON_START = date(2026, 10, 20)


def test_atp_projection_clamps_and_derives_shortage() -> None:
    result = build_atp_projection(
        on_hand=Decimal("0"), committed=Decimal("0"),
        events=[AtpEvent(date(2026, 10, 21), demand_qty=Decimal("10")), AtpEvent(date(2026, 10, 22), supply_qty=Decimal("5"))],
        horizon_start=HORIZON_START, horizon_end=date(2026, 10, 24), method="DISCRETE_ATP",
    )
    rows = {row["bucket_date"]: row for row in result["rows"]}
    assert rows[date(2026, 10, 20)]["projected_available"] == Decimal("0.000000000000")
    assert rows[date(2026, 10, 21)]["projected_available"] == Decimal("0.000000000000")
    assert rows[date(2026, 10, 21)]["shortage_qty"] == Decimal("10.000000000000")
    assert rows[date(2026, 10, 21)]["shortage_date"] == date(2026, 10, 21)
    assert rows[date(2026, 10, 22)]["projected_available"] == Decimal("5.000000000000")
    assert all(row["projected_available"] >= 0 for row in result["rows"])


def test_atp_earliest_full_date_fixture() -> None:
    result = build_atp_projection(
        on_hand=Decimal("100"), committed=Decimal("20"),
        events=[AtpEvent(date(2026, 10, 25), supply_qty=Decimal("50")), AtpEvent(date(2026, 10, 30), demand_qty=Decimal("100"))],
        horizon_start=HORIZON_START, horizon_end=date(2026, 10, 30),
        method="DISCRETE_ATP", required_qty=Decimal("120"),
    )
    assert result["earliest_full_date"] == date(2026, 10, 25)
    assert result["method"] == "DISCRETE_ATP"
    assert result["policy_version"] == "atp-earliest-v1/1"


def test_atp_projection_schema_persists_shortage_and_policy() -> None:
    from app.modules.replenishment import tables

    columns = {column.name for column in tables.atp_projection.c}
    assert {"shortage_qty", "shortage_date", "policy_version", "projected_available"} <= columns
    assert tables.ATP_METHODS == ("DISCRETE_ATP", "CUMULATIVE_ATP_WITH_LOOK_AHEAD")
