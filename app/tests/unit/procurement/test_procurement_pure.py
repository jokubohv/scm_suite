"""Pure FS-08 helpers: state machines, tolerances, open-qty math, facade metadata (no DB)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.procurement import tables as tables
from app.modules.procurement.facade import EVENT_TYPES, JOB_NAMES, PROCUREMENT_CONTRACT_VERSION
from app.modules.procurement.service import ProcurementService, _num
from app.modules.procurement.state_machines import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    can_transition,
    require_transition,
)


def test_metadata_table_groups_and_names() -> None:
    assert len(tables.ALL_TABLES) == 29
    assert (len(tables.REVISION_0015_TABLES), len(tables.REVISION_0016_TABLES), len(tables.REVISION_0017_TABLES)) == (6, 11, 12)
    names = {table.name for table in tables.ALL_TABLES}
    for expected in ("vendor", "item_vendor", "purchase_order", "item_receipt", "vendor_bill", "vendor_credit", "vendor_return_authorization", "special_order_link"):
        assert expected in names


def test_tolerance_boundaries() -> None:
    within = ProcurementService._within_tolerance
    assert within(None, Decimal("0.005"), Decimal("100"), Decimal("0.01"), Decimal("1.0")) is True
    assert within(None, Decimal("0.5"), Decimal("100"), Decimal("0.01"), Decimal("1.0")) is True  # 0.5% <= 1%
    assert within(None, Decimal("2"), Decimal("100"), Decimal("0.01"), Decimal("1.0")) is False  # 2% > 1%
    assert within(None, Decimal("-2"), Decimal("100"), Decimal("0.01"), Decimal("1.0")) is False


def test_open_qty_number_formatting() -> None:
    assert _num(Decimal("6.000000000000")) == "6"
    assert _num(Decimal("0.500000000000")) == "0.5"
    assert _num(Decimal("0")) == "0"


def test_state_machine_tables_cover_all_aggregates() -> None:
    assert len(ALLOWED_TRANSITIONS) == 13
    for aggregate, states in ALLOWED_TRANSITIONS.items():
        for terminal in TERMINAL_STATES.get(aggregate, frozenset()):
            assert terminal in states
    assert can_transition("purchase_order", "APPROVED", "PENDING_RECEIPT")
    assert not can_transition("purchase_order", "DRAFT", "RECEIVED")
    with pytest.raises(TransitionError):
        require_transition("vendor_bill", "DRAFT", "OPEN")
    require_transition("vendor_bill", "APPROVED", "OPEN")


def test_contract_constants() -> None:
    assert PROCUREMENT_CONTRACT_VERSION == "1.0.0"
    assert len(EVENT_TYPES) == 15
    assert len(JOB_NAMES) == 4
