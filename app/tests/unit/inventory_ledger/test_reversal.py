"""FS-04 unit: reversal mapping and command sign rules."""

from __future__ import annotations

from app.modules.inventory_ledger.service import REVERSAL_TYPE, validate_command


def test_receipt_reversal_uses_t02() -> None:
    assert REVERSAL_TYPE["T-01"] == "T-02"
    assert REVERSAL_TYPE["T-02"] == "T-01"
    command = validate_command({
        "posting_type": "T-02", "transaction_date": "2026-10-23",
        "movements": [{
            "item_id": "11111111-1111-1111-1111-111111111111",
            "location_id": "22222222-2222-2222-2222-222222222222",
            "inventory_status_id": "33333333-3333-3333-3333-333333333333",
            "qty": "-1", "movement_type": "REVERSAL",
        }],
    })
    assert command["movements"][0]["qty_base"] == -1
