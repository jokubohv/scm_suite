"""FS-10 D7/DM-PLAN-07: versioned AIM formula fixtures are deterministic and recorded."""

from __future__ import annotations

from decimal import Decimal

from app.modules.replenishment.engine import AIM_DEFAULT_V1, compute_aim, q12


def _run(d: str, s: str, lead: int, safety: str, available: str = "100", auto: bool = True) -> dict:
    daily = [Decimal("0")] * 30
    total = q12(Decimal(d) * 30)
    daily[-1] = total
    return compute_aim(
        demand_by_day={__import__("datetime").date(2026, 9, 1) + __import__("datetime").timedelta(days=index): qty for index, qty in enumerate(daily)},
        lead_time_days=lead, safety_stock=Decimal(safety), quantity_available=Decimal(available),
        policy=AIM_DEFAULT_V1, auto_calculate_planning=auto, seasonal_factor_value=Decimal(s),
    )


def test_aim_fixtures_match_policy_contract() -> None:
    fixtures = [
        ("10.000000000000", "1.000000000000", 10, "20.000000000000", "120.000000000000", "190.000000000000", "90.000000000000", "APPLY"),
        ("10.000000000000", "1.200000000000", 10, "20.000000000000", "140.000000000000", "224.000000000000", "124.000000000000", "APPLY"),
        ("3.500000000000", "1.000000000000", 5, "0.000000000000", "17.500000000000", "42.000000000000", "0.000000000000", "APPLY"),
        ("2.000000000000", "10.000000000000", 4, "1.000000000000", "81.000000000000", "221.000000000000", "121.000000000000", "APPLY"),
        ("0.000000000000", "1.000000000000", 10, "5.000000000000", "5.000000000000", "5.000000000000", "0.000000000000", "APPLY"),
    ]
    for d, s, lead, safety, expected_rop, expected_ps, expected_order, expected_action in fixtures:
        result = _run(d, s, lead, safety)
        assert result["formula_version"] == "aim-default-v1/1"
        assert result["reorder_point_calculated"] == Decimal(expected_rop)
        assert result["preferred_stock_calculated"] == Decimal(expected_ps)
        assert result["qty_to_order"] == Decimal(expected_order)
        assert result["action"] == expected_action


def test_aim_disabled_auto_calculation_skips_writes() -> None:
    result = _run("10", "1", 10, "20", auto=False)
    assert result["action"] == "SKIP_MANUAL"
    assert result["formula_version"] == "aim-default-v1/1"


def test_aim_deterministic_repeat() -> None:
    assert _run("10.000000000000", "1.200000000000", 10, "20") == _run("10.000000000000", "1.200000000000", 10, "20")
