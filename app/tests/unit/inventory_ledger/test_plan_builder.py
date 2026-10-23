"""FS-04 unit: posting-command validation, signed quantities and the 30 movement types."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.inventory_ledger.errors import UnsupportedPostingType, ValidationFailed
from app.modules.inventory_ledger.service import POSTING_TYPES, availability, validate_command

ITEM = str(uuid.uuid4())
LOC = str(uuid.uuid4())
STATUS = str(uuid.uuid4())


def _command(**overrides):
    command = {
        "posting_type": "T-07", "transaction_date": "2026-10-23",
        "movements": [{"item_id": ITEM, "location_id": LOC, "inventory_status_id": STATUS, "qty": "-2.5"}],
    }
    command.update(overrides)
    return command


def test_30_signed_movement_types_are_supported() -> None:
    assert POSTING_TYPES == tuple(f"T-{number:02d}" for number in range(1, 31))
    parsed = validate_command(_command())
    assert parsed["movements"][0]["qty_base"] == Decimal("-2.5")
    assert parsed["movements"][0]["details"][0]["qty_base"] == Decimal("-2.5")


def test_t31_batch_wrapper_and_reversal_are_not_direct_header_types() -> None:
    with pytest.raises(UnsupportedPostingType):
        validate_command(_command(posting_type="T-31"))
    with pytest.raises(UnsupportedPostingType):
        validate_command(_command(posting_type="T-99"))


def test_zero_quantity_and_detail_mismatch_are_rejected() -> None:
    with pytest.raises(ValidationFailed):
        validate_command(_command(movements=[{"item_id": ITEM, "location_id": LOC, "inventory_status_id": STATUS, "qty": "0"}]))
    with pytest.raises(ValidationFailed):
        validate_command(
            _command(
                movements=[{
                    "item_id": ITEM, "location_id": LOC, "inventory_status_id": STATUS, "qty": "2",
                    "details": [{"qty": "1"}],
                }]
            )
        )


def test_availability_never_negative() -> None:
    assert availability(Decimal("10"), Decimal("4")) == Decimal("6")
    assert availability(Decimal("2"), Decimal("5")) == Decimal("0")
