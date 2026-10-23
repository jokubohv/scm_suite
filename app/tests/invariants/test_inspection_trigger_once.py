"""FS-12 trigger-idempotency invariant (DM-QA-05): at most one inspection per (trigger, source).

Covers both layers: the service dedupe path, the partial unique index backstop, and the documented
frequency (every-Nth) rule.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.quality import tables as q
from app.modules.quality.service import QualityService
from app.tests.integration.quality.conftest import seed_tenant

pytestmark = pytest.mark.integration


async def _seed(client_service: QualityService, session, tenant_id):
    await seed_tenant(session, tenant_id, tenant_id.hex[:6].upper())
    spec = await client_service.create_spec(
        tenant_id=tenant_id,
        values={
            "code": "SPEC-T1",
            "name": "Trigger spec",
            "item_id": str(uuid4()),
            "lines": [{"characteristic": "length", "result_type": "TEXT"}],
        },
    )
    await client_service.activate_spec(tenant_id=tenant_id, spec_id=spec["id"])
    return spec


async def test_trigger_evaluation_creates_exactly_one_inspection(db_session) -> None:
    tenant_id = uuid4()
    service = QualityService(db_session)
    spec = await _seed(service, db_session, tenant_id)
    item_id, location_id = uuid4(), uuid4()
    # Seed an item/location via the shared fixture helper for FK validity.
    from app.tests.integration.quality.conftest import ITEM_ID, LOCATION_ID

    item_id, location_id = ITEM_ID, LOCATION_ID
    plan = await service.create_plan(
        tenant_id=tenant_id,
        values={
            "code": "PLAN-T1",
            "name": "Trigger plan",
            "item_id": str(item_id),
            "quality_spec_id": spec["id"],
            "sampling_method": "FULL",
        },
    )
    trigger = await service.create_trigger(
        tenant_id=tenant_id,
        values={
            "code": "TRG-T1",
            "name": "Receipt trigger",
            "trigger_event": "ITEM_RECEIPT",
            "inspection_plan_id": plan["id"],
        },
    )
    event = {
        "source_entity_type": "ITEM_RECEIPT",
        "source_entity_id": str(uuid4()),
        "item_id": str(item_id),
        "location_id": str(location_id),
    }
    first = await service.evaluate_trigger(tenant_id=tenant_id, trigger_id=trigger["id"], event=event)
    assert first["created"] is True
    second = await service.evaluate_trigger(tenant_id=tenant_id, trigger_id=trigger["id"], event=event)
    assert second["created"] is False and second.get("deduplicated") is True
    count = await db_session.scalar(
        text("SELECT count(*) FROM quality.inspection WHERE tenant_id = :t AND trigger_id = :trg"),
        {"t": tenant_id, "trg": trigger["id"]},
    )
    assert count == 1

    # Direct duplicate insert is rejected by uq_inspection_trigger_once (backstop under races).
    row = (
        (
            await db_session.execute(
                text(
                    "SELECT id::text, inspection_number, item_id::text, location_id::text, source_entity_type, "
                    "source_entity_id::text FROM quality.inspection WHERE tenant_id = :t AND trigger_id = :trg"
                ),
                {"t": tenant_id, "trg": trigger["id"]},
            )
        )
        .mappings()
        .one()
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                q.inspection.insert().values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    inspection_number="INSP-DUP-1",
                    trigger_id=trigger["id"],
                    item_id=row["item_id"],
                    location_id=row["location_id"],
                    source_entity_type=row["source_entity_type"],
                    source_entity_id=row["source_entity_id"],
                    status="PENDING",
                )
            )


async def test_frequency_rule_defers_every_nth_transaction(db_session) -> None:
    tenant_id = uuid4()
    service = QualityService(db_session)
    spec = await _seed(service, db_session, tenant_id)
    from app.tests.integration.quality.conftest import ITEM_ID, LOCATION_ID

    plan = await service.create_plan(
        tenant_id=tenant_id,
        values={"code": "PLAN-F2", "name": "Freq plan", "item_id": str(ITEM_ID), "quality_spec_id": spec["id"]},
    )
    trigger = await service.create_trigger(
        tenant_id=tenant_id,
        values={
            "code": "TRG-F2",
            "name": "Every 2nd",
            "trigger_event": "ITEM_RECEIPT",
            "inspection_plan_id": plan["id"],
            "condition": {"frequency": 2},
        },
    )
    base = {"source_entity_type": "ITEM_RECEIPT", "item_id": str(ITEM_ID), "location_id": str(LOCATION_ID)}
    first = await service.evaluate_trigger(
        tenant_id=tenant_id, trigger_id=trigger["id"], event={**base, "source_entity_id": str(uuid4())}
    )
    assert first["created"] is False and first.get("deferred") is True
    second = await service.evaluate_trigger(
        tenant_id=tenant_id, trigger_id=trigger["id"], event={**base, "source_entity_id": str(uuid4())}
    )
    assert second["created"] is True
