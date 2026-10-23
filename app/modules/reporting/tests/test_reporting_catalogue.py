"""FS-15 catalogue/contract unit tests (no DB required)."""

from __future__ import annotations

from app.contracts.reporting import contract
from app.modules.reporting.catalogue import ENDPOINTS, ENDPOINTS_BY_PATH, FILTER_COLUMNS, REPORT_ALIASES
from app.modules.reporting.service import ReportingError, decode_cursor, encode_cursor, json_safe
from app.modules.reporting.views import VIEW_NAMES, VIEWS

EXPECTED_VIEWS = [f"v_{name}" for name in (
    "stock_ledger", "inventory_balance", "inventory_valuation", "inventory_valuation_detail",
    "inventory_snapshot", "negative_inventory", "open_commitments", "backorders",
    "pending_fulfillment", "inventory_activity_detail", "inventory_turnover_inputs",
    "inventory_profitability_inputs", "atp_current", "planning_workbench", "purchase_history",
    "manufacturing_workbench", "lot_expiry", "serial_history", "consigned_balance",
    "quality_inspection_status", "available_to_build",
)]


def test_catalogue_has_exactly_21_views_v01_v21() -> None:
    assert len(VIEWS) == 21
    assert list(VIEW_NAMES) == EXPECTED_VIEWS
    numbers = [view.number for view in VIEWS]
    assert numbers == [f"V-{i:02d}" for i in range(1, 22)]


def test_materialized_set_matches_spec() -> None:
    materialized = {view.name for view in VIEWS if view.materialized}
    assert materialized == {"v_inventory_valuation_detail", "v_inventory_turnover_inputs",
                            "v_inventory_profitability_inputs", "v_manufacturing_workbench",
                            "v_available_to_build"}


def test_every_view_has_tenant_filter_and_order_key() -> None:
    for view in VIEWS:
        assert "tenant_id" in view.sql, view.name
        assert view.order_key, view.name
        assert view.name.startswith("v_")


def test_future_wave_views_have_typed_fallbacks() -> None:
    guarded = {"v_atp_current", "v_planning_workbench", "v_manufacturing_workbench",
               "v_consigned_balance", "v_quality_inspection_status", "v_available_to_build"}
    for view in VIEWS:
        if view.name in guarded:
            assert view.fallback_sql, view.name
            if view.name != "v_consigned_balance":
                assert "WHERE false" in view.fallback_sql


def test_endpoint_surface_matches_contract_help() -> None:
    paths = {spec.path for spec in ENDPOINTS}
    assert "/reports" not in paths  # catalogue endpoint is separate, not a view row
    assert "/reports/open-commitments" in paths
    assert "/reports/available-to-build" in paths
    assert "/inventory/balances/search" in paths
    assert "/inventory/details/search" in paths
    assert len([s for s in ENDPOINTS if not s.export]) == 22
    assert all(s.scope in {"reporting.read", "reporting.cost.view", "reporting.export"} for s in ENDPOINTS)
    aliases = set(REPORT_ALIASES)
    assert len(aliases) == 22


def test_filters_are_allow_listed() -> None:
    for spec in ENDPOINTS:
        for name in spec.filters:
            assert name in FILTER_COLUMNS, (spec.path, name)
    assert "reporting.cost.view" in {s.scope for s in ENDPOINTS if s.cost}
    assert ENDPOINTS_BY_PATH["/reports/inventory-valuation"].scope == "reporting.cost.view"


def test_cursor_roundtrip_and_invalid() -> None:
    token = encode_cursor(["item-1", "2026-10-01"])
    assert decode_cursor(token, 2) == ["item-1", "2026-10-01"]
    try:
        decode_cursor("not-base64!", 2)
    except ReportingError as exc:
        assert exc.code == "REPORT_CURSOR_INVALID"
    else:  # pragma: no cover
        raise AssertionError("invalid cursor accepted")


def test_json_safe_decimal_uuid_datetime() -> None:
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import UUID
    value = {"d": Decimal("1.2500"), "u": UUID("00000000-0000-0000-0000-000000000001"),
             "t": datetime(2026, 10, 1, tzinfo=UTC), "nested": [Decimal("0")]}
    safe = json_safe(value)
    assert safe == {"d": "1.2500", "u": "00000000-0000-0000-0000-000000000001",
                    "t": "2026-10-01T00:00:00+00:00", "nested": ["0"]}


def test_contract_facade_surface() -> None:
    assert contract.CONTRACT == "reporting.readmodels"
    assert contract.VERSION == "1.0.0"
    assert len(contract.VIEWS) == 21
    assert contract.EVENTS == ("reporting.view.refreshed", "reporting.refresh.failed")
    assert contract.JOBS == ("reporting.refresh_views", "reporting.reconcile_readmodels")
    assert len(contract.endpoint_paths()) == 23
    metadata = contract.view_metadata()
    assert metadata[0]["view_name"] == "v_stock_ledger"
    assert any(row["materialized"] for row in metadata)
