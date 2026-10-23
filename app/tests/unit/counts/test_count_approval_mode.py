"""FS-07 unit tests: declarative-approval mode resolution (AUTO / REQUEST / DIRECT)."""

from __future__ import annotations

from decimal import Decimal

from app.modules.counts.approvals import resolve_approval_mode
from app.modules.counts.engine import CountSettings, ToleranceDecision


def _settings(*, auto: bool = False, tolerance_qty: str = "0", tolerance_percent: str = "0") -> CountSettings:
    return CountSettings(
        auto_approve_within_tolerance=auto,
        tolerance_qty=Decimal(tolerance_qty),
        tolerance_percent=Decimal(tolerance_percent),
    )


def test_required_approval_uses_request_when_no_tolerance() -> None:
    decision = ToleranceDecision(eligible=False, reasons=("auto_approval_disabled",))
    assert resolve_approval_mode(settings=_settings(), tolerance=decision, approval_required=True) == "REQUEST"


def test_auto_approval_wins_when_tolerance_eligible() -> None:
    decision = ToleranceDecision(eligible=True, within_tolerance=True)
    settings = _settings(auto=True, tolerance_qty="100")
    assert resolve_approval_mode(settings=settings, tolerance=decision, approval_required=True) == "AUTO"


def test_direct_flow_when_approval_not_required() -> None:
    decision = ToleranceDecision(eligible=False)
    assert resolve_approval_mode(settings=_settings(), tolerance=decision, approval_required=False) == "DIRECT"


def test_auto_enabled_but_tolerance_not_eligible_falls_back_to_request() -> None:
    decision = ToleranceDecision(eligible=False, reasons=("qty_tolerance_exceeded:line",))
    settings = _settings(auto=True, tolerance_qty="0.5")
    assert resolve_approval_mode(settings=settings, tolerance=decision, approval_required=True) == "REQUEST"
