"""FS-15 live-DB tests for the 21 reporting views and reporting API (graceful skip before 0029 applies)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.reporting import contract
from app.modules.reporting.refresh import refresh_materialized_views
from app.modules.reporting.service import ReportService
from app.modules.reporting.views import MATERIALIZED_VIEWS, VIEW_NAMES


async def _require_reporting(db_session: AsyncSession) -> None:
    exists = await db_session.scalar(text("SELECT to_regclass('reporting.v_stock_ledger') IS NOT NULL"))
    if not exists:
        pytest.skip("reporting views not applied yet (migrations 0029/0030 pending)")


async def test_all_21_views_readable(db_session: AsyncSession) -> None:
    await _require_reporting(db_session)
    for view in VIEW_NAMES:
        rows = await db_session.execute(text(f"SELECT * FROM reporting.{view} LIMIT 0"))
        assert rows.keys(), view


async def test_materialized_views_and_refresh_function(db_session: AsyncSession) -> None:
    await _require_reporting(db_session)
    count = await db_session.scalar(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='reporting' AND c.relkind='m'"))
    assert count == 5
    refreshed = await refresh_materialized_views(db_session, concurrent=False)
    assert {row["view_name"] for row in refreshed} == set(MATERIALIZED_VIEWS)


async def test_reporting_is_read_only(db_session: AsyncSession) -> None:
    await _require_reporting(db_session)
    for view in VIEW_NAMES:
        for verb in ("INSERT", "UPDATE", "DELETE"):
            try:
                async with db_session.begin_nested():
                    await db_session.execute(text(f"{verb} INTO reporting.{view} DEFAULT VALUES"))
            except Exception:
                continue
            pytest.fail(f"write verb {verb} unexpectedly accepted by reporting.{view}")


async def test_service_tenant_scope_and_cursor(db_session: AsyncSession) -> None:
    await _require_reporting(db_session)
    service = ReportService(db_session, tenant_id="00000000-0000-0000-0000-000000000000")
    page = await service.query("v_inventory_balance", limit=2)
    assert isinstance(page.items, list) and len(page.items) <= 2
    if page.items:
        assert all(item.get("tenant_id") == "00000000-0000-0000-0000-000000000000" for item in page.items)
    freshness = await service.get_freshness("v_inventory_balance")
    assert freshness["view_name"] == "v_inventory_balance"


async def test_contract_facade_against_db(db_session: AsyncSession) -> None:
    await _require_reporting(db_session)
    data = await contract.query_view(db_session, tenant_id="00000000-0000-0000-0000-000000000000",
                                     app_user_id=None, view_name="v_stock_ledger", limit=1)
    assert "items" in data and "freshness" in data
    assert len(contract.endpoint_paths()) == 23
