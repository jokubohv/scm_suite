"""FS-15 V-21 pass-through regression: reporting consumes FS-09 ATB results from the platform outbox.

No local ATB formula is allowed (RULE-RPT-ATB): the materialised view projects the latest
``available_to_build.calculated`` event payload (``payload->'data'``) per tenant/assembly/location.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.models.platform import tenant
from app.core.outbox import emit, event_envelope
from app.modules.reporting.refresh import refresh_materialized_views
from app.modules.reporting.service import ReportService

pytestmark = pytest.mark.integration


async def _require_view(db_session: AsyncSession) -> None:
    if not await db_session.scalar(text("SELECT to_regclass('reporting.v_available_to_build') IS NOT NULL")):
        pytest.skip("reporting views not applied yet (migrations 0029/0030 pending)")


async def _new_tenant(db_session: AsyncSession, prefix: str) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await db_session.execute(
        insert(tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=f"{prefix}{uuid.uuid4().hex[:6].upper()}",
            name=f"V21 {prefix} tenant",
            status="ACTIVE",
            base_currency_code="USD",
        )
    )
    return tenant_id


async def _emit_atb(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    assembly_item_id: uuid.UUID,
    location_id: uuid.UUID,
    buildable_qty: str,
    input_hash: str,
    policy_version: str = "atb-v1",
    occurred_at: Any = None,
) -> uuid.UUID:
    payload = {
        "tenant_id": str(tenant_id),
        "item_id": str(uuid.uuid4()),
        "assembly_item_id": str(assembly_item_id),
        "location_id": str(location_id),
        "level_mode": "ALL_LEVELS",
        "basis": "ON_HAND",
        "as_of": "2026-10-23",
        "buildable_qty": buildable_qty,
        "limiting_component": {"component_item_id": str(uuid.uuid4()), "available": "12", "qty_per": "2"},
        "limiting_component_id": str(uuid.uuid4()),
        "qty_per_component": "2",
        "available_component_qty": "12",
        "per_level": [{"level": 1, "components": []}],
        "policy_version": policy_version,
        "input_hash": input_hash,
        "degraded": False,
    }
    envelope = event_envelope(
        event_type="available_to_build.calculated",
        tenant_id=str(tenant_id),
        aggregate_type="ASSEMBLY_ITEM",
        aggregate_id=str(assembly_item_id),
        payload=payload,
    )
    if occurred_at is not None:
        # outbox_event is append-only (UPDATE/DELETE trigger); control ordering at insert time.
        envelope["occurred_at"] = (
            occurred_at if isinstance(occurred_at, datetime) else datetime.fromisoformat(occurred_at)
        )
        envelope["payload"]["occurred_at"] = envelope["occurred_at"].isoformat()
    table = platform_models.metadata.tables["platform.outbox_event"]
    await db_session.execute(table.insert().values(**envelope))
    return uuid.UUID(envelope["id"])


async def _v21_rows(db_session: AsyncSession, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    result = await db_session.execute(
        text(
            "SELECT tenant_id, assembly_item, location_id, level_mode, basis, as_of, buildable_qty, "
            "limiting_component, per_level, policy_version, input_hash, qty_per_component, "
            "available_component_qty, degraded FROM reporting.v_available_to_build "
            "WHERE tenant_id = :tenant ORDER BY assembly_item, location_id"
        ),
        {"tenant": tenant_id},
    )
    return [dict(row._mapping) for row in result.all()]


async def test_v21_projects_latest_fs09_atb_event(db_session: AsyncSession) -> None:
    await _require_view(db_session)
    tenant_a = await _new_tenant(db_session, "V21A")
    assembly = uuid.uuid4()
    location = uuid.uuid4()
    # Older calculation (deterministically superseded) then the latest calculation.
    await _emit_atb(
        db_session,
        tenant_id=tenant_a,
        assembly_item_id=assembly,
        location_id=location,
        buildable_qty="5",
        input_hash="old-hash",
        occurred_at="2026-10-22T10:00:00+00:00",
    )
    await _emit_atb(
        db_session,
        tenant_id=tenant_a,
        assembly_item_id=assembly,
        location_id=location,
        buildable_qty="9",
        input_hash="new-hash",
        occurred_at="2026-10-23T10:00:00+00:00",
    )
    await _emit_atb(
        db_session,
        tenant_id=tenant_a,
        assembly_item_id=assembly,
        location_id=location,
        buildable_qty="7",
        input_hash="middle-hash",
        policy_version="atb-v1",
        occurred_at="2026-10-23T09:00:00+00:00",
    )
    refreshed = await refresh_materialized_views(db_session, concurrent=False)
    assert any(row["view_name"] == "v_available_to_build" and row["refreshed"] for row in refreshed)

    rows = await _v21_rows(db_session, tenant_a)
    assert len(rows) == 1
    row = rows[0]
    assert str(row["tenant_id"]) == str(tenant_a)
    assert str(row["assembly_item"]) == str(assembly)
    assert str(row["location_id"]) == str(location)
    assert row["level_mode"] == "ALL_LEVELS" and row["basis"] == "ON_HAND"
    assert str(row["buildable_qty"]) == "9.000000000000" or float(row["buildable_qty"]) == 9.0
    assert row["policy_version"] == "atb-v1"
    assert row["input_hash"] == "new-hash"
    assert float(row["qty_per_component"]) == 2.0
    assert float(row["available_component_qty"]) == 12.0
    assert row["degraded"] is False
    assert isinstance(row["per_level"], list)
    assert row["as_of"] is not None

    # Reporting service path (same source, tenant-scoped) also sees the pass-through row.
    service = ReportService(db_session, tenant_id=str(tenant_a))
    page = await service.query("v_available_to_build", limit=10)
    assert len(page.items) == 1
    assert page.items[0]["input_hash"] == "new-hash"
    assert page.freshness["view_name"] == "v_available_to_build"
    assert page.freshness["materialized"] is True


async def test_v21_tenant_isolation_and_unique_refresh_index(db_session: AsyncSession) -> None:
    await _require_view(db_session)
    tenant_a = await _new_tenant(db_session, "V21IA")
    tenant_b = await _new_tenant(db_session, "V21IB")
    shared_assembly = uuid.uuid4()
    shared_location = uuid.uuid4()
    await _emit_atb(
        db_session,
        tenant_id=tenant_a,
        assembly_item_id=shared_assembly,
        location_id=shared_location,
        buildable_qty="3",
        input_hash="tenant-a",
    )
    await _emit_atb(
        db_session,
        tenant_id=tenant_b,
        assembly_item_id=shared_assembly,
        location_id=shared_location,
        buildable_qty="4",
        input_hash="tenant-b",
    )
    await refresh_materialized_views(db_session, concurrent=False)

    tenant_a_rows = await _v21_rows(db_session, tenant_a)
    tenant_b_rows = await _v21_rows(db_session, tenant_b)
    assert len(tenant_a_rows) == 1 and len(tenant_b_rows) == 1
    assert tenant_a_rows[0]["input_hash"] == "tenant-a"
    assert tenant_b_rows[0]["input_hash"] == "tenant-b"
    assert float(tenant_a_rows[0]["buildable_qty"]) == 3.0
    assert float(tenant_b_rows[0]["buildable_qty"]) == 4.0

    # Unique index required for REFRESH ... CONCURRENTLY is present (no duplicate keys possible).
    index = await db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'reporting' "
            "AND tablename = 'v_available_to_build' AND indexname = 'uq_reporting_v_available_to_build'"
        )
    )
    indexdef = index.scalar_one()
    assert "UNIQUE INDEX" in indexdef
    assert "tenant_id" in indexdef and "assembly_item" in indexdef and "location_id" in indexdef


async def test_v21_ignores_malformed_events_and_keeps_contract_columns(db_session: AsyncSession) -> None:
    await _require_view(db_session)
    tenant_c = await _new_tenant(db_session, "V21IC")
    # An unrelated outbox event must not contribute to V-21.
    await emit(
        db_session,
        event_type="work_order.created",
        tenant_id=str(tenant_c),
        aggregate_type="work_order",
        aggregate_id=str(uuid.uuid4()),
        payload={"tenant_id": str(tenant_c), "work_order_id": str(uuid.uuid4())},
    )
    malformed = await emit(
        db_session,
        event_type="available_to_build.calculated",
        tenant_id=str(tenant_c),
        aggregate_type="ASSEMBLY_ITEM",
        aggregate_id=str(uuid.uuid4()),
        payload={"tenant_id": str(tenant_c), "note": "missing assembly/location"},
    )
    await refresh_materialized_views(db_session, concurrent=False)
    rows = await _v21_rows(db_session, tenant_c)
    assert rows == []
    assert malformed["event_type"] == "available_to_build.calculated"

    columns = await db_session.execute(
        text(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'reporting.v_available_to_build'::regclass AND attnum > 0 AND NOT attisdropped "
            "ORDER BY attnum"
        )
    )
    names = [row[0] for row in columns.all()]
    assert names == [
        "tenant_id",
        "assembly_item",
        "location_id",
        "level_mode",
        "basis",
        "as_of",
        "buildable_qty",
        "limiting_component",
        "per_level",
        "policy_version",
        "input_hash",
        "qty_per_component",
        "available_component_qty",
        "degraded",
    ]
