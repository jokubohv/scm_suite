"""Pure FS-13 helpers (no DB)."""

from __future__ import annotations

from decimal import Decimal

from app.modules.consigned import tables as t
from app.modules.consigned.service import EVENT_TYPES, JOB_NAMES, _date, _num


def test_metadata_tables() -> None:
    assert len(t.ALL_TABLES) == 5
    assert {table.name for table in t.ALL_TABLES} == {
        "consignment_agreement",
        "consignment_agreement_item",
        "consignment_ownership_transfer",
        "consignment_ownership_transfer_line",
        "consignment_consumption",
    }


def test_numbers_and_event_constants() -> None:
    assert _num(Decimal("5.000000000000")) == "5"
    assert _num(Decimal("0.500000000000")) == "0.5"
    assert str(_date("2026-10-23")) == "2026-10-23"
    assert len(EVENT_TYPES) == 11 and len(JOB_NAMES) == 4
