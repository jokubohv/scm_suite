"""Unit guard: V-21 is a pass-through over the FS-09 outbox event, never a local ATB formula."""

from __future__ import annotations

import importlib

from app.modules.reporting.views import V21, VIEWS


def test_v21_materialized_pass_through_sql() -> None:
    assert V21.name == "v_available_to_build"
    assert V21.materialized is True
    sql = V21.sql
    assert "platform.outbox_event" in sql
    assert "available_to_build.calculated" in sql
    assert "payload->'data'" in sql
    assert "DISTINCT ON (tenant_id, assembly_item, location_id)" in sql
    assert "occurred_at DESC" in sql
    # No local recomputation of the ATB formula (no BOM/balance joins, no cache table).
    assert "manufacturing." not in sql
    assert "inventory_ledger." not in sql
    assert "available_to_build_cache" not in sql
    assert V21.fallback_sql and "WHERE false" in V21.fallback_sql
    assert V21 in VIEWS


def test_0029_source_guard_targets_platform_outbox() -> None:
    migration = importlib.import_module("app.migrations.versions.0029_reporting_views")
    assert migration.SOURCE_GUARDS["v_available_to_build"] == "platform.outbox_event"
    assert "available_to_build_cache" not in repr(migration.SOURCE_GUARDS)
