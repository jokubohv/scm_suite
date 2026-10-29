"""FS-10 INV-20 / DM-PLAN-01: planning never writes ledger, balance or cost rows."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.modules.replenishment.service import PlanningService
from app.tests.integration.replenishment.conftest import (
    _seed_item_master,
    _seed_location_status,
    _seed_tenant,
    seed_balance,
)

pytestmark = pytest.mark.integration

MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "replenishment"
LEDGER_TABLES = (
    "inventory_ledger.inventory_transaction",
    "inventory_ledger.inventory_transaction_line",
    "inventory_ledger.inventory_balance",
    "costing.cost_ledger_entry",
    "costing.item_cost",
)


def test_planning_module_has_no_ledger_write_imports() -> None:
    forbidden = ("insert(inventory_ledger", "update(inventory_ledger", "insert(cost", "update(cost")
    for path in MODULE_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in source, f"{path.name} contains forbidden write pattern {pattern!r}"
        assert "app.modules.inventory_ledger.tables" not in source or path.name in {"repository.py", "service.py"}


async def _counts(session, tenant_id) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in LEDGER_TABLES:
        counts[table] = int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one()
        )
    return counts


async def test_planning_runs_do_not_touch_ledger_rows(db_session) -> None:
    tenant_id = uuid4()
    location_id, status_id, item_id = uuid4(), uuid4(), uuid4()
    await _seed_tenant(db_session, tenant_id, f"RO{uuid4().hex[:6].upper()}")
    await _seed_item_master(db_session, tenant_id, suffix=uuid4().hex[:6].upper(), item_ids=(item_id,))
    await _seed_location_status(db_session, tenant_id, location_id=location_id, status_id=status_id, suffix=uuid4().hex[:6].upper())
    await seed_balance(db_session, tenant_id=tenant_id, item_id=item_id, location_id=location_id, status_id=status_id, qty="10", value="40")
    await db_session.flush()
    before = await _counts(db_session, tenant_id)

    service = PlanningService(db_session, tenant_id)
    rule = await service.create_rule(
        {
            "code": "RO1", "name": "Readonly", "engine": "MRP", "allowed_supply_types": ["PURCHASE"],
            "lot_sizing": "LOT_FOR_LOT", "time_fence_days": 0,
            "scopes": [{"scope_type": "LOCATION", "scope_id": str(location_id)}],
        }
    )
    plan = await service.create_supply_plan(
        {
            "planning_rule_id": str(rule["id"]), "horizon_start": "2026-10-01", "horizon_end": "2026-12-31",
            "scope": {"items": [str(item_id)], "locations": [str(location_id)]}, "as_of": "2026-10-01",
        }
    )
    await service.run_supply_plan(plan["id"])
    await service.create_calculation({"calculation_day": "2026-10-23", "scope": {"items": [str(item_id)]}})
    await service.rebuild_atp({"scope": {"items": [str(item_id)], "locations": [str(location_id)]}, "as_of": "2026-10-23"})
    after = await _counts(db_session, tenant_id)
    assert after == before, f"planning wrote ledger/cost rows: {before} -> {after}"


def test_no_ledger_write_client_in_planning_module() -> None:
    """Planning must not hold a ledger write client (INV-20): only balance reads via the facade."""
    repository_source = (MODULE_DIR / "repository.py").read_text(encoding="utf-8")
    assert "PostingPort" not in repository_source
    assert "inventory_ledger.tables" not in repository_source
    service_source = (MODULE_DIR / "service.py").read_text(encoding="utf-8")
    assert "PostingPort" not in service_source
