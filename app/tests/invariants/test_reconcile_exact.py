"""FS-15 reconcile-exact invariants (INV-03/INV-10, DM-GLOBAL-09): views aggregate to source read models."""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _skip_unless_applied(db_session):
    if not await db_session.scalar(text("SELECT to_regclass('reporting.v_stock_ledger') IS NOT NULL")):
        pytest.skip("reporting views not applied yet")


async def test_balance_view_reconciles_to_balance_read_model(db_session) -> None:
    await _skip_unless_applied(db_session)
    view = await db_session.scalar(text(
        "SELECT COALESCE(SUM(qty_on_hand), 0) FROM reporting.v_inventory_balance"))
    source = await db_session.scalar(text(
        "SELECT COALESCE(SUM(qty_on_hand), 0) FROM inventory_ledger.inventory_balance"))
    assert view == source


async def test_stock_ledger_view_reconciles_to_ledger_lines(db_session) -> None:
    await _skip_unless_applied(db_session)
    view = await db_session.scalar(text("SELECT COALESCE(SUM(qty_base), 0) FROM reporting.v_stock_ledger"))
    source = await db_session.scalar(text(
        "SELECT COALESCE(SUM(qty_base), 0) FROM inventory_ledger.inventory_transaction_line"))
    assert view == source


async def test_valuation_view_reconciles_to_item_cost(db_session) -> None:
    await _skip_unless_applied(db_session)
    view = await db_session.scalar(text("SELECT COALESCE(SUM(value_base), 0) FROM reporting.v_inventory_valuation"))
    source = await db_session.scalar(text("SELECT COALESCE(SUM(value_base), 0) FROM costing.item_cost"))
    assert view == source
