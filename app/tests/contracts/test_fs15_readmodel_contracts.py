"""FS-15 read-model contract: 21 views, catalogue, endpoints, scopes, events, jobs (no DB)."""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.reporting import contract
from app.modules.reporting.catalogue import ENDPOINTS, REPORT_ALIASES
from app.modules.reporting.views import VIEWS

ROOT = Path(__file__).resolve().parents[3]
PERMISSIONS = json.loads((ROOT / "design/permissions.json").read_text())
CATALOGUE = {entry["scope"] for entry in PERMISSIONS["catalogue"]}


def test_21_read_models_contract() -> None:
    assert len(VIEWS) == 21
    assert len(contract.VIEWS) == 21
    assert contract.CONTRACT == "reporting.readmodels" and contract.VERSION == "1.0.0"


def test_endpoint_scopes_are_catalogued() -> None:
    for spec in ENDPOINTS:
        assert spec.scope in CATALOGUE, (spec.path, spec.scope)
    assert REPORT_ALIASES["available-to-build"] == "v_available_to_build"


def test_join_and_dimension_columns_present() -> None:
    required = {"v_stock_ledger": {"transaction_id", "line_id", "qty_base", "value_base"},
                "v_inventory_balance": {"qty_on_hand", "qty_committed", "qty_available", "in_transit"},
                "v_open_commitments": {"qty_committed", "qty_fulfilled", "qty_backordered"},
                "v_available_to_build": {"buildable_qty", "policy_version", "input_hash", "per_level"}}
    for view in VIEWS:
        expected = required.get(view.name, set())
        for column in expected:
            assert column in view.sql, (view.name, column)


def test_events_and_jobs_registered() -> None:
    assert contract.EVENTS == ("reporting.view.refreshed", "reporting.refresh.failed")
    assert contract.JOBS == ("reporting.refresh_views", "reporting.reconcile_readmodels")
    for event in contract.EVENTS:
        schema = ROOT / "contracts/events/reporting.readmodels" / f"{event}.schema.json"
        assert schema.exists(), schema


def test_openapi_fragment_covers_all_endpoints() -> None:
    fragment = json.loads((ROOT / "app/contracts/reporting/openapi.fragment.json").read_text())
    paths = fragment["paths"]
    assert "/api/v1/reports" in paths
    assert "/api/v1/reports/stock-ledger" in paths
    assert "/api/v1/inventory/balances/search" in paths
    assert "/api/v1/reports/{report_name}/export" in paths
    assert len(paths) == 24
