"""Fiscal periods admin API (live DB): create/list, overlap + date/status guards, RBAC."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import pytest

from app.api.deps import Principal
from app.core.models import platform as p
from app.tests.integration.platform_config.conftest import OTHER_TENANT_ID, TENANT_ID

PREFIX = "/api/v1/admin/fiscal-periods"


async def test_create_and_list_fiscal_periods(app_client: Any) -> None:
    assert (await app_client.get(PREFIX)).json()["items"] == []

    created = await app_client.post(
        PREFIX,
        json={"period_key": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-31"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["period_key"] == "2026-10"
    assert body["status"] == "OPEN"
    assert body["closed_at"] is None
    assert body["source_version"] == 1

    listed = await app_client.get(PREFIX)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1 and items[0]["period_key"] == "2026-10"


async def test_closed_period_and_query_filters(app_client: Any) -> None:
    closed = await app_client.post(
        PREFIX,
        json={
            "period_key": "2026-09",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "status": "CLOSED",
        },
    )
    assert closed.status_code == 201
    assert closed.json()["status"] == "CLOSED"
    assert closed.json()["closed_at"] is not None

    by_status = await app_client.get(PREFIX, params={"status": "closed"})
    assert by_status.status_code == 200
    assert {item["period_key"] for item in by_status.json()["items"]} == {"2026-09"}

    by_year = await app_client.get(PREFIX, params={"year": 2026})
    assert by_year.status_code == 200
    assert {item["period_key"] for item in by_year.json()["items"]} == {"2026-09"}

    other_year = await app_client.get(PREFIX, params={"year": 2027})
    assert other_year.json()["items"] == []

    bad_status = await app_client.get(PREFIX, params={"status": "PAUSED"})
    assert bad_status.status_code == 422
    assert bad_status.json()["code"] == "PERIOD_STATUS_INVALID"


async def test_duplicate_and_overlapping_periods_rejected(app_client: Any) -> None:
    october = {"period_key": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-31"}
    assert (await app_client.post(PREFIX, json=october)).status_code == 201

    duplicate = await app_client.post(PREFIX, json=october)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "PERIOD_OVERLAP"  # overlap is checked before the unique key

    overlap = await app_client.post(
        PREFIX,
        json={"period_key": "2026-10b", "start_date": "2026-10-15", "end_date": "2026-11-15"},
    )
    assert overlap.status_code == 409
    problem = overlap.json()
    assert problem["code"] == "PERIOD_OVERLAP"
    assert problem["details"][0]["overlaps"]

    adjacent = await app_client.post(
        PREFIX,
        json={"period_key": "2026-11", "start_date": "2026-11-01", "end_date": "2026-11-30"},
    )
    assert adjacent.status_code == 201


async def test_period_validation_errors(app_client: Any) -> None:
    reversed_dates = await app_client.post(
        PREFIX,
        json={"period_key": "2026-10", "start_date": "2026-10-31", "end_date": "2026-10-01"},
    )
    assert reversed_dates.status_code == 422
    assert reversed_dates.json()["code"] == "PERIOD_DATES_INVALID"

    bad_key = await app_client.post(
        PREFIX, json={"period_key": "not a key!", "start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert bad_key.status_code == 422
    assert bad_key.json()["code"] == "PERIOD_KEY_INVALID"

    bad_status = await app_client.post(
        PREFIX,
        json={
            "period_key": "2026-10",
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
            "status": "REOPEN",
        },
    )
    assert bad_status.status_code == 422  # pydantic Literal


async def test_fiscal_periods_are_tenant_scoped(app_client: Any, db_session: Any) -> None:
    await db_session.execute(
        p.fiscal_period.insert().values(
            id=UUID("7a000000-0000-7000-8000-0000000000a1"),
            tenant_id=OTHER_TENANT_ID,
            period_key="2026-10",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            status="OPEN",
            source_version=1,
        )
    )
    await db_session.flush()
    response = await app_client.get(PREFIX)
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_fiscal_requires_scope(client_factory: Any, principal: Principal) -> None:
    reader_without = Principal(
        identity_id=principal.identity_id,
        app_user_id=principal.app_user_id,
        tenant_id=principal.tenant_id,
        token_version=0,
        scopes=frozenset({"platform.finance.sync"}),
    )
    async with client_factory(reader_without) as client:
        denied = await client.get(PREFIX)
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN_SCOPE"
