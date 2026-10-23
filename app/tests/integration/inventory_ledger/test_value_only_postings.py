"""FS-04 integration: value-only vendor bill/credit/variance/landed/drop-ship post qty0+value, no qty change."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.models.platform import outbox_event
from app.modules.inventory_ledger.service import VALUE_ONLY_POSTING_TYPES, LedgerService

pytestmark = pytest.mark.integration


def _movement(world, *, qty="0", value="25", movement_type="VENDOR_BILL"):
    return {
        "item_id": str(world["item_id"]), "location_id": str(world["location_id"]),
        "inventory_status_id": str(world["status_id"]), "qty": qty, "value_base": value,
        "movement_type": movement_type,
    }


async def _post(service, world, posting_type: str, key: str, **movement):
    return await service.post({
        "posting_type": posting_type, "transaction_date": "2026-10-23",
        "idempotency_key": key, "movements": [_movement(world, **movement)],
    })


async def test_all_value_only_types_post_without_inventory_movement(db_session, world) -> None:
    service = LedgerService(db_session, world["tenant_id"])
    for index, posting_type in enumerate(sorted(VALUE_ONLY_POSTING_TYPES), start=1):
        result = await _post(service, world, posting_type, f"value-only-{index}", value="25")
        assert result["value_only"] is True, posting_type
        assert result["total_qty_base"] in {"0", "0.000000000000"}
        assert Decimal(str(result["total_value_base"])) == Decimal("25")
    balances = (
        await db_session.execute(
            select(outbox_event.c.event_type).where(outbox_event.c.tenant_id == world["tenant_id"])
        )
    ).scalars().all()
    assert "inventory.value.posted" in balances
    transaction = await service.get_transaction(result["transaction_id"])
    lines = await service.transaction_lines(result["transaction_id"])
    assert lines and all(Decimal(str(line["qty_base"])) == 0 for line in lines)
    assert Decimal(str(lines[0]["value_base"])) == Decimal("25")
    # no inventory balance rows were created by value-only postings
    from sqlalchemy import text

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM inventory_ledger.inventory_balance WHERE tenant_id = :tenant"),
            {"tenant": world["tenant_id"]},
        )
    ).scalar_one()
    assert int(count) == 0
    assert transaction["total_qty_base"] in {"0", "0.000000000000"}


async def test_value_only_does_not_change_existing_quantity(db_session, world) -> None:
    service = LedgerService(db_session, world["tenant_id"])
    await service.post({
        "posting_type": "T-07", "transaction_date": "2026-10-23", "idempotency_key": "qty-seed",
        "movements": [_movement(world, qty="5", value="50", movement_type="ADJUSTMENT")],
    })
    before = await service.availability_summary(item_id=str(world["item_id"]), location_id=str(world["location_id"]))
    assert Decimal(before["on_hand"]) == Decimal("5")
    await _post(service, world, "T-04", "bill-qty", value="25")
    after = await service.availability_summary(item_id=str(world["item_id"]), location_id=str(world["location_id"]))
    assert Decimal(after["on_hand"]) == Decimal("5")
    assert Decimal(after["available"]) == Decimal("5")


async def test_value_only_invalid_combinations_are_422(ledger_client, world) -> None:
    invalid_qty = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": "T-04", "transaction_date": "2026-10-23",
              "movements": [_movement(world, qty="1", value="10")]},
        headers=world["headers"] | {"Idempotency-Key": "value-invalid-1"},
    )
    assert invalid_qty.status_code == 422, invalid_qty.text
    invalid_value = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": "T-28", "transaction_date": "2026-10-23",
              "movements": [_movement(world, qty="0", value="0")]},
        headers=world["headers"] | {"Idempotency-Key": "value-invalid-2"},
    )
    assert invalid_value.status_code == 422, invalid_value.text
    normal_zero = await ledger_client.post(
        "/api/v1/inventory-transactions",
        json={"posting_type": "T-07", "transaction_date": "2026-10-23",
              "movements": [_movement(world, qty="0", value="10", movement_type="ADJUSTMENT")]},
        headers=world["headers"] | {"Idempotency-Key": "value-invalid-3"},
    )
    assert normal_zero.status_code == 422


async def test_value_only_reversal_keeps_qty_zero(db_session, world) -> None:
    service = LedgerService(db_session, world["tenant_id"])
    posted = await _post(service, world, "T-05", "credit-rev", value="12")
    reversal = await service.reverse(posted["transaction_id"], reason="credit reversal", idempotency_key="credit-rev-r")
    lines = await service.transaction_lines(reversal["transaction_id"])
    assert all(Decimal(str(line["qty_base"])) == 0 for line in lines)
    assert Decimal(str(lines[0]["value_base"])) == Decimal("-12")
