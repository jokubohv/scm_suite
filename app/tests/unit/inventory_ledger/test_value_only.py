"""FS-04 unit: value-only posting types accept qty=0/value!=0 and reject invalid combinations."""

from __future__ import annotations

import uuid

import pytest

from app.modules.inventory_ledger.errors import ValidationFailed
from app.modules.inventory_ledger.service import VALUE_ONLY_POSTING_TYPES, validate_command

ITEM = str(uuid.uuid4())
LOC = str(uuid.uuid4())
STATUS = str(uuid.uuid4())


def _command(posting_type: str, *, qty: object = "0", value: object = "25", movement_type: str = "VENDOR_BILL"):
    return {
        "posting_type": posting_type,
        "transaction_date": "2026-10-23",
        "movements": [{
            "item_id": ITEM, "location_id": LOC, "inventory_status_id": STATUS,
            "qty": qty, "value_base": value, "movement_type": movement_type,
        }],
    }


def test_documented_value_only_types() -> None:
    assert VALUE_ONLY_POSTING_TYPES == frozenset({"T-04", "T-05", "T-06", "T-19", "T-24", "T-25", "T-26", "T-27", "T-28"})


@pytest.mark.parametrize("posting_type", sorted(VALUE_ONLY_POSTING_TYPES))
def test_value_only_types_validate_qty_zero_nonzero_value(posting_type: str) -> None:
    parsed = validate_command(_command(posting_type))
    assert parsed["value_only"] is True
    movement = parsed["movements"][0]
    assert movement["qty_base"] == 0 and movement["value_base"] == 25 and movement["value_only"] is True


def test_value_only_requires_zero_qty() -> None:
    with pytest.raises(ValidationFailed, match="value-only"):
        validate_command(_command("T-04", qty="1", value="25"))


def test_value_only_requires_nonzero_value() -> None:
    with pytest.raises(ValidationFailed, match="non-zero value"):
        validate_command(_command("T-28", qty="0", value="0"))


def test_value_only_qty_may_be_omitted() -> None:
    command = _command("T-06", value="12")
    command["movements"][0].pop("qty")
    parsed = validate_command(command)
    assert parsed["movements"][0]["qty_base"] == 0


def test_normal_posting_still_rejects_zero_qty() -> None:
    with pytest.raises(ValidationFailed, match="non-zero"):
        validate_command(_command("T-07", qty="0", value="10", movement_type="ADJUSTMENT"))
