"""Pure FS-06 rules: lot/serial validation, FEFO ordering, serial lifecycle mapping (no DB)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.contracts.traceability_numbered.contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    SERIAL_TRANSITIONS,
    NumberingPolicy,
    SerialLifecycleEvent,
    fefo_order_key,
    lot_allocation_schema,
    map_ledger_event,
    serial_lifecycle_event_schema,
    validate_numbered_details,
)
from app.modules.traceability_numbered.errors import ValidationError
from app.modules.traceability_numbered.service import (
    validate_date_pair,
    validate_lot_number,
    validate_serial_number,
)


def test_contract_identity() -> None:
    assert CONTRACT_NAME == "traceability.numbered"
    assert CONTRACT_VERSION == "1.0.0"
    assert lot_allocation_schema()["type"] == "object"
    assert serial_lifecycle_event_schema()["properties"]["event"]["enum"]


def test_lot_and_serial_number_validation() -> None:
    assert validate_lot_number(" LOT-A1 ") == "LOT-A1"
    assert validate_serial_number("SN_0001-2") == "SN_0001-2"
    for bad in ("", " ", "has space", "x" * 65):
        with pytest.raises(ValidationError):
            validate_lot_number(bad)
    for bad in ("", "bad serial", "x" * 97):
        with pytest.raises(ValidationError):
            validate_serial_number(bad)
    with pytest.raises(ValidationError):
        validate_date_pair(date(2026, 1, 1), date(2025, 1, 1))


def test_fefo_order_is_deterministic_expiry_first() -> None:
    rows = [
        {"lot_id": "b", "lot_number": "LOT-B", "expiry_date": date(2026, 12, 1)},
        {"lot_id": "a", "lot_number": "LOT-A", "expiry_date": date(2026, 11, 1)},
        {"lot_id": "c", "lot_number": "LOT-C", "expiry_date": None},
        {"lot_id": "a2", "lot_number": "LOT-A", "expiry_date": date(2026, 11, 1)},
    ]
    ordered = sorted(rows, key=fefo_order_key)
    assert [row["lot_id"] for row in ordered] == ["a", "a2", "b", "c"]


def test_ledger_event_mapping_and_serial_transitions() -> None:
    assert map_ledger_event("RECEIPT") == "RECEIVED"
    assert map_ledger_event("FULFILLMENT") == "FULFILLED"
    assert map_ledger_event("ASSEMBLY_BUILD") == "RECEIVED"
    assert map_ledger_event("UNKNOWN_MOVEMENT") == "MOVED"
    assert "COMMITTED" in SERIAL_TRANSITIONS["AVAILABLE"]
    assert SERIAL_TRANSITIONS["SCRAPPED"] == frozenset()
    event = SerialLifecycleEvent(event="RECEIVED", serial_id="s", item_id="i", occurred_at=datetime(2026, 10, 1))
    assert event.to_dict()["event"] == "RECEIVED"


def test_validate_numbered_details_violations() -> None:
    serial_policy = NumberingPolicy(
        item_id="i",
        item_type_code="SERIALIZED",
        requires_numbering=True,
        requires_lot=False,
        requires_serial=True,
    )
    violations = validate_numbered_details([{"serial_id": "s1", "qty_base": "2"}], serial_policy, qty_base="3")
    codes = {violation.code for violation in violations}
    assert {"SERIAL_QTY_MISMATCH", "DETAIL_QTY_SUM_MISMATCH"} <= codes

    lot_policy = NumberingPolicy(
        item_id="i",
        item_type_code="LOT_NUMBERED",
        requires_numbering=True,
        requires_lot=True,
        requires_serial=False,
    )
    expired = validate_numbered_details(
        [{"lot_id": "l1", "qty_base": "1", "expiry_date": date.today() - timedelta(days=1)}],
        lot_policy,
        qty_base="1",
    )
    assert [violation.code for violation in expired] == ["LOT_EXPIRED"]

    ok = validate_numbered_details(
        [{"lot_id": "l1", "qty_base": "1", "expiry_date": date.today() + timedelta(days=30)}],
        lot_policy,
        qty_base="1",
    )
    assert ok == []

    no_details = validate_numbered_details([], serial_policy, qty_base="1")
    assert {violation.code for violation in no_details} == {"SERIAL_REQUIRED"}
