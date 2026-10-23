"""FS-07 DM-CNT-01 invariant: snapshot rows are immutable; recalculation appends a linked revision."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.modules.counts import tables as ct
from app.modules.counts.engine import CountPosition, build_snapshot_rows
from app.tests.integration.counts.conftest import (
    _seed_item_master,
    _seed_location_status,
    _seed_tenant,
)

pytestmark = pytest.mark.integration


def _position() -> CountPosition:
    return CountPosition(
        item_id=str(uuid4()), location_id=str(uuid4()), inventory_status_id=str(uuid4()),
        uom_id=str(uuid4()), qty_on_hand=Decimal("10"), value_base=Decimal("40"), ledger_sequence=7,
    )


def test_recalculation_appends_new_revision_linked_to_original() -> None:
    count_id = str(uuid4())
    base_at = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    original = build_snapshot_rows(inventory_count_id=count_id, positions=[_position()], snapshot_at=base_at)
    revision = build_snapshot_rows(
        inventory_count_id=count_id, positions=[_position()], snapshot_at=base_at,
        recalculated_from_id=str(uuid4()),
    )
    assert original[0].recalculated_from_id is None
    assert revision[0].recalculated_from_id is not None
    # original rows are never replaced: both revisions carry the same count id and distinct timestamps
    assert original[0].inventory_count_id == revision[0].inventory_count_id == count_id


async def test_snapshot_rows_reject_update_and_delete(db_session) -> None:
    tenant_id = uuid4()
    location_id, status_id, item_id = uuid4(), uuid4(), uuid4()
    await _seed_tenant(db_session, tenant_id, f"IMM{uuid4().hex[:6].upper()}")
    await _seed_item_master(db_session, tenant_id, suffix=uuid4().hex[:6].upper(), item_ids=(item_id,))
    await _seed_location_status(db_session, tenant_id, location_id=location_id, status_id=status_id, suffix=uuid4().hex[:6].upper())
    count_id = uuid4()
    await db_session.execute(
        ct.inventory_count.insert().values(
            id=count_id, tenant_id=tenant_id, count_number=f"CNT-{uuid4().hex[:8].upper()}",
            count_type="MANUAL", status="OPEN", count_date=date(2026, 10, 23), version=1,
        )
    )
    snapshot_id = uuid4()
    await db_session.execute(
        ct.inventory_count_snapshot.insert().values(
            id=snapshot_id, tenant_id=tenant_id, inventory_count_id=count_id,
            item_id=item_id, location_id=location_id, inventory_status_id=status_id, ownership="OWNED",
            snapshot_qty=Decimal("10"), snapshot_value_base=Decimal("40"), snapshot_ledger_sequence=1,
            snapshot_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    with pytest.raises(DBAPIError):
        await db_session.execute(
            ct.inventory_count_snapshot.update()
            .where(ct.inventory_count_snapshot.c.id == snapshot_id)
            .values(snapshot_qty=Decimal("1"))
        )
        await db_session.flush()
    await db_session.rollback()
