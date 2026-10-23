"""FS-12 quality-hold invariant (DM-QA-02 + DM-TRACE-03): holds are ledger T-30 postings only.

Quality tables never carry quantities; applying/releasing a hold must produce exactly one T-30
status-change posting each, move quantity into a non-committable status, preserve total quantity/value,
and be linked back to the inspection via platform.document_link.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, text

from app.modules.inventory_ledger import tables as ledger
from app.modules.quality.service import QualityService
from app.tests.integration.quality.conftest import (
    AVAILABLE_STATUS,
    HOLD_STATUS,
    ITEM_ID,
    LOCATION_ID,
    seed_tenant,
)

pytestmark = pytest.mark.integration


async def _seed_balance(db_session, tenant_id, qty: str = "5") -> None:
    await db_session.execute(
        insert(ledger.inventory_balance).values(
            id=uuid4(),
            tenant_id=tenant_id,
            item_id=ITEM_ID,
            location_id=LOCATION_ID,
            inventory_status_id=AVAILABLE_STATUS,
            lot_id=None,
            serial_id=None,
            ownership="OWNED",
            qty_on_hand=Decimal(qty),
            qty_committed=Decimal("0"),
            qty_on_order=Decimal("0"),
            value_base=Decimal("0"),
        )
    )


async def _failed_inspection(db_session, service: QualityService, tenant_id):
    spec = await service.create_spec(
        tenant_id=tenant_id,
        values={
            "code": "SPEC-H1",
            "name": "Hold spec",
            "item_id": str(ITEM_ID),
            "lines": [{"characteristic": "length", "result_type": "NUMERIC", "min_value": 1, "max_value": 2}],
        },
    )
    await service.activate_spec(tenant_id=tenant_id, spec_id=spec["id"])
    plan = await service.create_plan(
        tenant_id=tenant_id,
        values={"code": "PLAN-H1", "name": "Hold plan", "item_id": str(ITEM_ID), "quality_spec_id": spec["id"]},
    )
    inspection = await service.create_inspection_on_demand(
        tenant_id=tenant_id,
        values={
            "inspection_plan_id": plan["id"],
            "item_id": str(ITEM_ID),
            "location_id": str(LOCATION_ID),
            "source_entity_type": "ITEM_RECEIPT",
        },
    )
    await service.start_inspection(tenant_id=tenant_id, inspection_id=inspection["id"], inspector_id=None)
    await service.record_results(
        tenant_id=tenant_id,
        inspection_id=inspection["id"],
        results=[
            {
                "quality_spec_line_id": plan["lines"][0]["quality_spec_line_id"],
                "sequence": 1,
                "result_type": "NUMERIC",
                "measured_value_number": "9",
            }
        ],
    )
    return await service.complete_inspection(tenant_id=tenant_id, inspection_id=inspection["id"], disposition="HOLD")


async def _balances(db_session, tenant_id) -> dict:
    rows = (
        (
            await db_session.execute(
                ledger.inventory_balance.select().where(ledger.inventory_balance.c.tenant_id == tenant_id)
            )
        )
        .mappings()
        .all()
    )
    return {row["inventory_status_id"]: Decimal(row["qty_on_hand"]) for row in rows}


async def test_hold_moves_quantity_to_non_committable_status_via_t30(db_session) -> None:
    tenant_id = uuid4()
    service = QualityService(db_session)
    await seed_tenant(db_session, tenant_id, tenant_id.hex[:6].upper())
    await _seed_balance(db_session, tenant_id)
    inspection = await _failed_inspection(db_session, service, tenant_id)
    assert inspection["status"] == "FAILED"
    assert inspection["disposition"] == "HOLD"

    state_before = await db_session.scalar(
        text(
            "SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :t AND transaction_type = 'T-30'"
        ),
        {"t": tenant_id},
    )
    result = await service.apply_hold(
        tenant_id=tenant_id,
        inspection_id=inspection["id"],
        positions=[
            {
                "item_id": str(ITEM_ID),
                "location_id": str(LOCATION_ID),
                "inventory_status_id": str(AVAILABLE_STATUS),
                "qty": "2",
            }
        ],
    )
    assert result["inventory_transaction_id"]
    after = await _balances(db_session, tenant_id)
    assert after[AVAILABLE_STATUS] == Decimal("3")
    assert after[HOLD_STATUS] == Decimal("2")
    assert after[AVAILABLE_STATUS] + after[HOLD_STATUS] == Decimal("5")  # quantity conservation

    t30 = await db_session.scalar(
        text(
            "SELECT count(*) FROM inventory_ledger.inventory_transaction "
            "WHERE tenant_id = :t AND transaction_type = 'T-30' AND source_doc_type = 'quality.inspection' "
            "AND source_doc_id = :i"
        ),
        {"t": tenant_id, "i": inspection["id"]},
    )
    assert t30 == 1
    assert int(state_before or 0) == 0

    links = await db_session.scalar(
        text(
            "SELECT count(*) FROM platform.document_link WHERE tenant_id = :t AND from_type = 'quality.inspection' "
            "AND from_id = :i AND link_type LIKE 'QUALITY_HOLD%'"
        ),
        {"t": tenant_id, "i": inspection["id"]},
    )
    assert links == 1

    # Quality tables carry no quantities: the only movement record is the ledger transaction.
    quantity_columns = await db_session.scalar(
        text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'quality' "
            "AND column_name IN ('qty_on_hand','qty_base','qty_committed')"
        )
    )
    assert quantity_columns == 0


async def test_hold_release_restores_available_status(db_session) -> None:
    tenant_id = uuid4()
    service = QualityService(db_session)
    await seed_tenant(db_session, tenant_id, tenant_id.hex[:6].upper())
    await _seed_balance(db_session, tenant_id)
    inspection = await _failed_inspection(db_session, service, tenant_id)
    positions = [
        {
            "item_id": str(ITEM_ID),
            "location_id": str(LOCATION_ID),
            "inventory_status_id": str(AVAILABLE_STATUS),
            "qty": "2",
        }
    ]
    await service.apply_hold(tenant_id=tenant_id, inspection_id=inspection["id"], positions=positions)
    waived = await service.waive_inspection(
        tenant_id=tenant_id, inspection_id=inspection["id"], reason="concession", approval_ref="APP-1"
    )
    assert waived["status"] == "WAIVED"
    released = await service.release_hold(
        tenant_id=tenant_id,
        inspection_id=inspection["id"],
        positions=[
            {
                "item_id": str(ITEM_ID),
                "location_id": str(LOCATION_ID),
                "inventory_status_id": str(HOLD_STATUS),
                "qty": "2",
            }
        ],
    )
    assert released["inventory_transaction_id"]
    after = await _balances(db_session, tenant_id)
    assert after[AVAILABLE_STATUS] == Decimal("5")
    t30 = await db_session.scalar(
        text(
            "SELECT count(*) FROM inventory_ledger.inventory_transaction WHERE tenant_id = :t AND transaction_type = 'T-30'"
        ),
        {"t": tenant_id},
    )
    assert t30 == 2
