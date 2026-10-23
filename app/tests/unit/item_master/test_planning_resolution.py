"""Item-master planning-value validation unit tests (pure service helpers)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.item_master.errors import PlanningValuesInvalid
from app.modules.item_master.service import ItemMasterService

service = ItemMasterService.__new__(ItemMasterService)


def test_planning_payload_accepts_non_negative_values() -> None:
    payload = service._planning_payload(
        {"reorder_point": "10", "preferred_stock_level": Decimal("20"), "safety_stock": 0, "lead_time_days": 3}
    )
    assert payload["reorder_point"] == Decimal("10")
    assert payload["preferred_stock_level"] == Decimal("20")
    assert payload["lead_time_days"] == 3


def test_planning_payload_rejects_negative() -> None:
    with pytest.raises(PlanningValuesInvalid):
        service._planning_payload({"reorder_point": "-1"})
    with pytest.raises(PlanningValuesInvalid):
        service._planning_payload({"lead_time_days": -5})


def test_planning_payload_ignores_unknown_keys() -> None:
    assert service._planning_payload({"unknown": 1}) == {}
