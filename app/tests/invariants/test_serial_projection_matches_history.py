"""FS-06 serial projection invariants (INV-18, DM-TRACE-04/05/08).

Serial current state is a strict function of the append-only history, which is itself derived from the
FS-04 posting tables inside the posting transaction. These tests drive the real posting tables (no FS-04
service shortcut) and assert: projection == latest history, history immutability, DM-TRACE-08 projection
guard, +/-1 serial detail guard, numbered-detail requirement and expired-lot rejection, plus serial
commitment reserve/release projection transitions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.inventory_ledger import tables as ledger
from app.modules.traceability_numbered import tables as numbered
from app.tests.integration.traceability.numbered.conftest import (
    _enable_domain,
    _item,
    _item_types,
    _registry,
    _tenant,
)

pytestmark = pytest.mark.integration

SEQUENCE = {"next": 1000}


def _next_sequence() -> int:
    SEQUENCE["next"] += 1
    return SEQUENCE["next"]


async def _seed(tenant_id):
    await _tenant_seed(tenant_id)


async def _tenant_seed(tenant_id):
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401  (typing only)

    return None


async def _seed_all(session, tenant_id):
    await _tenant(session, tenant_id, f"INV{tenant_id.hex[:6].upper()}")
    await _enable_domain(session, tenant_id)
    types = await _item_types(session, tenant_id)
    serial_item, lot_item = uuid4(), uuid4()
    await _item(session, tenant_id, serial_item, f"SER-{tenant_id.hex[:6]}", types["SERIALIZED"])
    await _item(session, tenant_id, lot_item, f"LOT-{tenant_id.hex[:6]}", types["LOT_NUMBERED"])
    await _registry(session, tenant_id)
    return serial_item, lot_item


async def _register_serial(session, tenant_id, item_id, number: str):
    serial_id = uuid4()
    await session.execute(
        insert(numbered.serial_number).values(
            id=serial_id,
            tenant_id=tenant_id,
            item_id=item_id,
            serial_number=number,
            state="UNKNOWN",
            ownership="OWNED",
        )
    )
    return serial_id


async def _register_lot(session, tenant_id, item_id, number: str, expiry: date | None):
    lot_id = uuid4()
    await session.execute(
        insert(numbered.lot).values(
            id=lot_id,
            tenant_id=tenant_id,
            item_id=item_id,
            lot_number=number,
            expiry_date=expiry,
            is_active=True,
        )
    )
    return lot_id


async def _post(
    session,
    *,
    tenant_id,
    item_id,
    location_id,
    status_id,
    movement_type: str,
    direction: str,
    qty: Decimal,
    transaction_date: date,
    lot_id=None,
    serial_id=None,
):
    transaction_id, line_id = uuid4(), uuid4()
    await session.execute(
        insert(ledger.inventory_transaction).values(
            id=transaction_id,
            tenant_id=tenant_id,
            transaction_type="T-01" if direction == "IN" else "T-12",
            transaction_date=transaction_date,
            ledger_sequence=_next_sequence(),
        )
    )
    await session.execute(
        insert(ledger.inventory_transaction_line).values(
            id=line_id,
            tenant_id=tenant_id,
            inventory_transaction_id=transaction_id,
            line_number=1,
            item_id=item_id,
            location_id=location_id,
            inventory_status_id=status_id,
            qty_uom=abs(qty),
            qty_base=qty,
            movement_type=movement_type,
            movement_direction=direction,
            transaction_date=transaction_date,
            lot_id=lot_id,
            serial_id=serial_id,
        )
    )
    if serial_id is not None or lot_id is not None:
        await session.execute(
            insert(ledger.inventory_transaction_detail).values(
                id=uuid4(),
                tenant_id=tenant_id,
                inventory_transaction_line_id=line_id,
                inventory_transaction_id=transaction_id,
                transaction_date=transaction_date,
                inventory_status_id=status_id,
                ownership="OWNED",
                qty_base=qty,
                lot_id=lot_id,
                serial_id=serial_id,
            )
        )
    else:
        await session.execute(
            insert(ledger.inventory_transaction_detail).values(
                id=uuid4(),
                tenant_id=tenant_id,
                inventory_transaction_line_id=line_id,
                inventory_transaction_id=transaction_id,
                transaction_date=transaction_date,
                inventory_status_id=status_id,
                ownership="OWNED",
                qty_base=qty,
            )
        )
    return transaction_id, line_id


async def _projection(session, tenant_id, serial_id):
    row = (
        (
            await session.execute(
                text(
                    "SELECT state, current_location_id, current_status_id FROM traceability.serial_number "
                    "WHERE tenant_id = :tenant AND id = :id"
                ),
                {"tenant": tenant_id, "id": serial_id},
            )
        )
        .mappings()
        .one()
    )
    history = (
        (
            await session.execute(
                text(
                    "SELECT event, to_location_id, to_status_id FROM traceability.serial_number_history "
                    "WHERE tenant_id = :tenant AND serial_id = :id ORDER BY occurred_at DESC, id DESC LIMIT 1"
                ),
                {"tenant": tenant_id, "id": serial_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row), (dict(history) if history else None)


async def test_projection_equals_latest_history_across_receipt_and_fulfillment(db_session) -> None:
    tenant_id = uuid4()
    serial_item, _ = await _seed_all(db_session, tenant_id)
    serial_id = await _register_serial(db_session, tenant_id, serial_item, "SN-100")
    from app.tests.integration.traceability.numbered.conftest import LOCATION_ID, STATUS_ID

    await _post(
        db_session,
        tenant_id=tenant_id,
        item_id=serial_item,
        location_id=LOCATION_ID,
        status_id=STATUS_ID,
        movement_type="RECEIPT",
        direction="IN",
        qty=Decimal("1"),
        transaction_date=date.today(),
        serial_id=serial_id,
    )
    projection, history = await _projection(db_session, tenant_id, serial_id)
    assert projection["state"] == "AVAILABLE"
    assert projection["current_location_id"] == LOCATION_ID
    assert history is not None and history["event"] == "RECEIVED"
    assert history["to_location_id"] == projection["current_location_id"]
    assert history["to_status_id"] == projection["current_status_id"]

    await _post(
        db_session,
        tenant_id=tenant_id,
        item_id=serial_item,
        location_id=LOCATION_ID,
        status_id=STATUS_ID,
        movement_type="FULFILLMENT",
        direction="OUT",
        qty=Decimal("-1"),
        transaction_date=date.today(),
        serial_id=serial_id,
    )
    projection, history = await _projection(db_session, tenant_id, serial_id)
    assert projection["state"] == "FULFILLED"
    assert history is not None and history["event"] == "FULFILLED"
    count = await db_session.scalar(
        text("SELECT count(*) FROM traceability.serial_number_history WHERE tenant_id = :tenant AND serial_id = :id"),
        {"tenant": tenant_id, "id": serial_id},
    )
    assert count == 2


async def test_history_is_append_only_and_projection_is_trigger_guarded(db_session) -> None:
    tenant_id = uuid4()
    serial_item, _ = await _seed_all(db_session, tenant_id)
    serial_id = await _register_serial(db_session, tenant_id, serial_item, "SN-200")
    from app.tests.integration.traceability.numbered.conftest import LOCATION_ID, STATUS_ID

    await _post(
        db_session,
        tenant_id=tenant_id,
        item_id=serial_item,
        location_id=LOCATION_ID,
        status_id=STATUS_ID,
        movement_type="RECEIPT",
        direction="IN",
        qty=Decimal("1"),
        transaction_date=date.today(),
        serial_id=serial_id,
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE traceability.serial_number_history SET event = 'MOVED' WHERE tenant_id = :t AND serial_id = :s"
                ),
                {"t": tenant_id, "s": serial_id},
            )
    with pytest.raises(DBAPIError, match="DM-TRACE-08|derived"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE traceability.serial_number SET state = 'SCRAPPED' WHERE tenant_id = :t AND id = :s"),
                {"t": tenant_id, "s": serial_id},
            )


async def test_serial_detail_qty_and_numbered_requirement_guards(db_session) -> None:
    tenant_id = uuid4()
    serial_item, lot_item = await _seed_all(db_session, tenant_id)
    serial_id = await _register_serial(db_session, tenant_id, serial_item, "SN-300")
    from app.tests.integration.traceability.numbered.conftest import LOCATION_ID, STATUS_ID

    with pytest.raises(DBAPIError, match="TN001"):
        async with db_session.begin_nested():
            await _post(
                db_session,
                tenant_id=tenant_id,
                item_id=serial_item,
                location_id=LOCATION_ID,
                status_id=STATUS_ID,
                movement_type="RECEIPT",
                direction="IN",
                qty=Decimal("2"),
                transaction_date=date.today(),
                serial_id=serial_id,
            )
    with pytest.raises(DBAPIError, match="TN003"):
        async with db_session.begin_nested():
            await _post(
                db_session,
                tenant_id=tenant_id,
                item_id=serial_item,
                location_id=LOCATION_ID,
                status_id=STATUS_ID,
                movement_type="RECEIPT",
                direction="IN",
                qty=Decimal("1"),
                transaction_date=date.today(),
            )


async def test_expired_lot_issue_is_rejected(db_session) -> None:
    tenant_id = uuid4()
    _, lot_item = await _seed_all(db_session, tenant_id)
    lot_id = await _register_lot(db_session, tenant_id, lot_item, "LOT-EXPIRED", date.today() - timedelta(days=1))
    from app.tests.integration.traceability.numbered.conftest import LOCATION_ID, STATUS_ID

    with pytest.raises(DBAPIError, match="TN004"):
        async with db_session.begin_nested():
            await _post(
                db_session,
                tenant_id=tenant_id,
                item_id=lot_item,
                location_id=LOCATION_ID,
                status_id=STATUS_ID,
                movement_type="ADJUSTMENT",
                direction="OUT",
                qty=Decimal("-1"),
                transaction_date=date.today(),
                lot_id=lot_id,
            )


async def test_serial_commitment_reserves_then_releases(db_session) -> None:
    tenant_id = uuid4()
    serial_item, _ = await _seed_all(db_session, tenant_id)
    serial_id = await _register_serial(db_session, tenant_id, serial_item, "SN-400")
    from app.tests.integration.traceability.numbered.conftest import LOCATION_ID, STATUS_ID

    commitment_id = uuid4()
    await db_session.execute(
        insert(ledger.inventory_commitment).values(
            id=commitment_id,
            tenant_id=tenant_id,
            source_type="OTHER",
            source_line_ref=f"line-{commitment_id.hex[:8]}",
            item_id=serial_item,
            location_id=LOCATION_ID,
            inventory_status_id=STATUS_ID,
            qty_committed=Decimal("1"),
        )
    )
    await db_session.execute(
        insert(ledger.inventory_commitment_allocation).values(
            tenant_id=tenant_id,
            commitment_id=commitment_id,
            serial_id=serial_id,
            qty=Decimal("1"),
            allocation_seq=1,
        )
    )
    state = await db_session.scalar(
        text("SELECT state FROM traceability.serial_number WHERE tenant_id = :t AND id = :s"),
        {"t": tenant_id, "s": serial_id},
    )
    assert state == "COMMITTED"

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(ledger.inventory_commitment_allocation).values(
                    tenant_id=tenant_id,
                    commitment_id=commitment_id,
                    serial_id=serial_id,
                    qty=Decimal("1"),
                    allocation_seq=2,
                )
            )

    await db_session.execute(
        update(ledger.inventory_commitment_allocation)
        .where(
            ledger.inventory_commitment_allocation.c.tenant_id == tenant_id,
            ledger.inventory_commitment_allocation.c.commitment_id == commitment_id,
        )
        .values(released_at=datetime.now(UTC))
    )
    state = await db_session.scalar(
        text("SELECT state FROM traceability.serial_number WHERE tenant_id = :t AND id = :s"),
        {"t": tenant_id, "s": serial_id},
    )
    assert state == "AVAILABLE"
    events = (
        (
            await db_session.execute(
                text(
                    "SELECT event FROM traceability.serial_number_history "
                    "WHERE tenant_id = :t AND serial_id = :s ORDER BY occurred_at, id"
                ),
                {"t": tenant_id, "s": serial_id},
            )
        )
        .scalars()
        .all()
    )
    assert events == ["COMMITTED", "RELEASED"]
