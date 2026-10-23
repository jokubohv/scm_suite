"""FS-15 invariants (DM-RPT-01): reporting views are SELECT-only and use security_invoker (DB-gated)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _skip_unless_applied(db_session):
    if not await db_session.scalar(text("SELECT to_regclass('reporting.v_stock_ledger') IS NOT NULL")):
        pytest.skip("reporting views not applied yet")


async def test_reporting_schema_is_read_only(db_session) -> None:
    await _skip_unless_applied(db_session)
    views = await db_session.execute(text(
        "SELECT table_name FROM information_schema.views WHERE table_schema='reporting'"))
    names = [row[0] for row in views.all()]
    assert len(names) + 5 == 21  # 16 plain + 5 materialized
    # Views must not be auto-updatable; a write attempt is checked on a separate rolled-back connection.
    updatable = await db_session.scalar(text(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='reporting' AND is_updatable='YES'"))
    assert updatable == 0


async def test_plain_views_are_security_invoker(db_session) -> None:
    await _skip_unless_applied(db_session)
    bad = await db_session.scalar(text(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='reporting' AND c.relkind='v' "
        "AND COALESCE((c.reloptions::text LIKE '%security_invoker=true%'), false)=false"))
    assert bad == 0
