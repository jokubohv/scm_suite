"""FS-10 planning API integration tests (live Postgres; rollback-isolated test tenancy)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.models import platform as p
from app.modules.replenishment import tables as rt
from app.tests.integration.replenishment.conftest import (
    ITEM_A,
    LOCATION_ID,
    OTHER_TENANT_ID,
    TENANT_ID,
    idem,
)

pytestmark = pytest.mark.integration


async def _create_rule(client: Any, **overrides: Any) -> dict[str, Any]:
    body = {
        "code": "MRP1", "name": "MRP rule", "engine": "MRP", "allowed_supply_types": ["PURCHASE"],
        "lot_sizing": "LOT_FOR_LOT", "time_fence_days": 0,
        "scopes": [{"scope_type": "LOCATION", "scope_id": str(LOCATION_ID), "include_descendants": False}],
    }
    body.update(overrides)
    response = await client.post("/api/v1/planning-rules", json=body, headers=idem())
    assert response.status_code == 201, response.text
    return response.json()


async def _create_demand_plan(client: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/demand-plans",
        json={
            "method": "MOVING_AVERAGE", "horizon_start": "2026-10-01", "horizon_end": "2026-12-31",
            "bucket_granularity": "WEEK", "scope": {"items": [str(ITEM_A)], "locations": [str(LOCATION_ID)]},
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_supply_plan(client: Any, rule_id: str, demand_plan_id: str | None = None) -> dict[str, Any]:
    scope: dict[str, Any] = {"items": [str(ITEM_A)], "locations": [str(LOCATION_ID)]}
    if demand_plan_id:
        scope["demand_plan_id"] = demand_plan_id
    response = await client.post(
        "/api/v1/supply-plans",
        json={
            "planning_rule_id": rule_id, "horizon_start": "2026-10-01", "horizon_end": "2026-12-31",
            "scope": scope, "as_of": "2026-10-01", "bucket_granularity": "WEEK",
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_planning_rule_crud_and_idempotency(client: Any) -> None:
    rule = await _create_rule(client)
    assert rule["code"] == "MRP1" and rule["scopes"][0]["scope_type"] == "LOCATION"

    first = await client.post("/api/v1/planning-rules", json={"code": "IDEM", "name": "Idem"}, headers={"Idempotency-Key": "rule-key"})
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/planning-rules", json={"code": "IDEM", "name": "Idem"}, headers={"Idempotency-Key": "rule-key"})
    assert replay.status_code in {200, 201} and replay.json()["id"] == first.json()["id"]

    listing = await client.get("/api/v1/planning-rules")
    assert listing.status_code == 200
    assert {row["code"] for row in listing.json()["items"]} >= {"MRP1", "IDEM"}

    scopes = await client.put(
        f"/api/v1/planning-rules/{rule['id']}/scopes", json={"scopes": []}, headers=idem()
    )
    assert scopes.status_code == 200 and scopes.json()["count"] == 0


async def test_time_fence_blocks_frozen_demand_override(client: Any) -> None:
    created = await client.post(
        "/api/v1/time-fences",
        json={"scope_type": "GLOBAL", "fence_days": 10, "behavior": "FROZEN", "demand_change_threshold": "20"},
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    plan = await _create_demand_plan(client)
    calculated = await client.post(f"/api/v1/demand-plans/{plan['id']}/calculate", headers=idem())
    assert calculated.status_code == 200, calculated.text
    lines = await client.get(f"/api/v1/demand-plans/{plan['id']}/lines")
    assert lines.status_code == 200
    line = lines.json()["items"][0]
    frozen = await client.patch(
        f"/api/v1/demand-plans/{plan['id']}/lines/{line['id']}",
        json={"qty_manual_override": "5"}, headers=idem(),
    )
    assert frozen.status_code == 409
    assert frozen.json()["code"] == "TIME_FENCE_VIOLATION"
    slushed = await client.post(
        "/api/v1/time-fences",
        json={"scope_type": "GLOBAL", "fence_days": 10, "behavior": "SLUSHED"},
        headers=idem(),
    )
    assert slushed.status_code == 201 and slushed.json()["behavior"] == "SLUSHED"


async def test_demand_plan_lifecycle(client: Any) -> None:
    plan = await _create_demand_plan(client)
    calculated = await client.post(f"/api/v1/demand-plans/{plan['id']}/calculate", headers=idem())
    assert calculated.status_code == 200
    assert calculated.json()["status"] == "CALCULATED"
    lines = await client.get(f"/api/v1/demand-plans/{plan['id']}/lines")
    assert lines.json()["count"] > 0
    detail = await client.get(f"/api/v1/demand-plans/{plan['id']}", params={"include": "lines"})
    assert detail.status_code == 200 and len(detail.json()["lines"]) > 0
    approved = await client.post(f"/api/v1/demand-plans/{plan['id']}/approve", headers=idem())
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    archived = await client.post(f"/api/v1/demand-plans/{plan['id']}/archive", headers=idem())
    assert archived.status_code == 200 and archived.json()["status"] == "ARCHIVED"


async def test_supply_plan_run_suggestions_and_dry_run_conversion(client: Any, db_session: Any) -> None:
    rule = await _create_rule(client)
    plan = await _create_supply_plan(client, rule["id"])
    assert plan["status"] == "QUEUED"
    run = await client.post(f"/api/v1/supply-plans/{plan['id']}/run", headers=idem())
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "CALCULATED"
    lines = await client.get(f"/api/v1/supply-plans/{plan['id']}/lines")
    assert lines.status_code == 200 and lines.json()["count"] > 0
    suggestions = await client.get(f"/api/v1/supply-plans/{plan['id']}/suggestions")
    assert suggestions.status_code == 200
    if suggestions.json()["count"]:
        suggestion = suggestions.json()["items"][0]
        converted = await client.post(
            f"/api/v1/supply-plans/{plan['id']}/create-orders",
            json={"suggestion_ids": [suggestion["id"]], "dry_run": True}, headers=idem(),
        )
        assert converted.status_code == 200
        assert converted.json()["items"][0]["status"] == "DRY_RUN"
    approved = await client.post(f"/api/v1/supply-plans/{plan['id']}/approve", headers=idem())
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"

    # deterministic run_key: creating the same plan again returns the same run
    duplicate = await _create_supply_plan(client, rule["id"])
    assert duplicate["id"] == plan["id"]


async def test_aim_calculation_formula_version_and_apply(client: Any) -> None:
    created = await client.post(
        "/api/v1/replenishment-calculations",
        json={"calculation_day": "2026-10-23", "scope": {"items": [str(ITEM_A)], "locations": [str(LOCATION_ID)]}},
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    calculation = created.json()
    assert calculation["formula_version"] == "aim-default-v1/1"
    assert calculation["status"] == "COMPLETED"
    lines = await client.get(f"/api/v1/replenishment-calculations/{calculation['id']}/lines")
    assert lines.status_code == 200 and lines.json()["count"] == 1
    line = lines.json()["items"][0]
    assert line["formula_version"] == "aim-default-v1/1"
    assert line["action"] in {"APPLY", "SKIP_MANUAL"}
    applied = await client.post(f"/api/v1/replenishment-calculations/{calculation['id']}/apply", json={"dry_run": True}, headers=idem())
    assert applied.status_code == 200
    assert applied.json()["dry_run"] is True
    order_items = await client.get("/api/v1/replenishment/order-items")
    assert order_items.status_code == 200


async def test_allocation_strategy_and_run(client: Any) -> None:
    strategy = await client.post(
        "/api/v1/allocation-strategies",
        json={
            "code": "PRIO1", "name": "Priority", "method": "PRIORITY", "fefo_enabled": True,
            "rules": [{"sequence": 1, "criterion": "REQUIRED_BY_DATE", "direction": "ASC"}],
        },
        headers=idem(),
    )
    assert strategy.status_code == 201, strategy.text
    run = await client.post(
        "/api/v1/allocation-runs",
        json={"allocation_strategy_id": strategy.json()["id"], "as_of_date": "2026-10-23"}, headers=idem(),
    )
    assert run.status_code == 201, run.text
    assert run.json()["status"] == "COMPLETED"
    results = await client.get(f"/api/v1/allocation-runs/{run.json()['id']}/results")
    assert results.status_code == 200
    exceptions = await client.get(f"/api/v1/allocation-runs/{run.json()['id']}/exceptions")
    assert exceptions.status_code == 200


async def test_atp_rebuild_and_query_clamped(client: Any) -> None:
    rebuilt = await client.post(
        "/api/v1/atp/rebuild",
        json={"scope": {"items": [str(ITEM_A)], "locations": [str(LOCATION_ID)]}, "as_of": "2026-10-23"},
        headers=idem(),
    )
    assert rebuilt.status_code == 202, rebuilt.text
    assert rebuilt.json()["rebuilt_count"] > 0
    projection = await client.get("/api/v1/atp/projection", params={"item_id": str(ITEM_A), "location_id": str(LOCATION_ID)})
    assert projection.status_code == 200
    assert projection.json()["items"]
    assert all(Decimal(row["projected_available"]) >= 0 for row in projection.json()["items"])
    query = await client.get("/api/v1/atp", params={"item_id": str(ITEM_A), "location_id": str(LOCATION_ID), "qty": "5"})
    assert query.status_code == 200
    assert query.json()["policy_version"].startswith("atp-earliest-v1/")


async def test_drp_network_bod_and_cycle_guard(client: Any) -> None:
    network = await client.post(
        "/api/v1/drp/networks",
        json={"code": "NET1", "name": "Network", "effective_from": "2026-01-01"}, headers=idem(),
    )
    assert network.status_code == 201, network.text
    members = await client.put(
        f"/api/v1/drp/networks/{network.json()['id']}/members",
        json={"members": [{"location_id": str(LOCATION_ID), "level": 0, "is_source": True}]}, headers=idem(),
    )
    assert members.status_code == 200
    bod = await client.post(
        "/api/v1/bills-of-distribution",
        json={"drp_network_id": network.json()["id"], "code": "BOD1", "name": "BOD", "version": 1, "effective_from": "2026-01-01"},
        headers=idem(),
    )
    assert bod.status_code == 201, bod.text
    cycle = await client.put(
        f"/api/v1/bills-of-distribution/{bod.json()['id']}/levels",
        json={"levels": [
            {"level": 1, "source_location_id": str(LOCATION_ID), "destination_location_id": str(LOCATION_ID)},
        ]}, headers=idem(),
    )
    assert cycle.status_code == 422
    assert cycle.json()["code"] == "INVALID_DRP_GRAPH"
    bad = await client.post(
        "/api/v1/drp/networks",
        json={"code": "BAD", "name": "Bad", "members": [{"location_id": str(LOCATION_ID)}, {"location_id": str(LOCATION_ID)}]},
        headers=idem(),
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "INVALID_DRP_GRAPH"


async def test_preferences_workbench_signals_and_disabled_flag(client: Any, db_session: Any) -> None:
    preferences = await client.get("/api/v1/planning/preferences")
    assert preferences.status_code == 200
    assert preferences.json()["policy_versions"]["aim"] == "aim-default-v1/1"
    workbench = await client.get("/api/v1/planning/workbench")
    assert workbench.status_code == 200 and "items" in workbench.json()
    signals = await client.get("/api/v1/planning/signals")
    assert signals.status_code == 200
    await db_session.execute(
        p.feature_flag.update()
        .where(p.feature_flag.c.tenant_id == TENANT_ID, p.feature_flag.c.flag_key == "replenishment.planning.enabled")
        .values(is_enabled=False)
    )
    await db_session.flush()
    disabled = await client.post("/api/v1/planning-rules", json={"code": "OFF", "name": "Off"}, headers=idem())
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "DOMAIN_DISABLED"


async def test_tenant_scoping(client: Any, db_session: Any) -> None:
    await _create_rule(client, code="SCOPED")
    rows = (await db_session.execute(select(rt.planning_rule.c.tenant_id).where(rt.planning_rule.c.code == "SCOPED"))).scalars().all()
    assert {str(row) for row in rows} == {str(TENANT_ID)}
    other = await db_session.execute(select(rt.planning_rule).where(rt.planning_rule.c.tenant_id == OTHER_TENANT_ID))
    assert other.first() is None
