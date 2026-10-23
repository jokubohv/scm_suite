"""Live-DB tests for finance period-status + reference exchange-rate surfaces (task-53)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.api.deps import get_current_principal
from app.core.models import platform as p

TENANT_ID = UUID("9b000000-0000-7000-8000-000000000001")
OTHER_TENANT_ID = UUID("9b000000-0000-7000-8000-000000000002")

pytestmark = pytest.mark.integration

PERIODS = "/api/v1/finance/period-status"
RATES = "/api/v1/reference/exchange-rates"
INTERNAL_RATES = "/api/v1/internal/finance/exchange-rates"


async def _seed_period(
    db_session: Any, tenant_id: UUID, *, period_key: str, start: date, end: date, status: str = "OPEN"
):
    await db_session.execute(
        p.fiscal_period.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            period_key=period_key,
            start_date=start,
            end_date=end,
            status=status,
            source_version=1,
            version=1,
        )
    )
    await db_session.flush()


async def _seed_rate(
    db_session: Any,
    tenant_id: UUID,
    *,
    base: str = "USD",
    quote: str = "EUR",
    rate_date: date,
    rate: str = "0.91",
    source: str = "finance",
):
    await db_session.execute(
        p.exchange_rate.insert().values(
            id=uuid4(),
            tenant_id=tenant_id,
            base_currency=base,
            quote_currency=quote,
            rate_date=rate_date,
            rate=rate,
            source=source,
            version=1,
        )
    )
    await db_session.flush()


async def test_period_status_read_and_tenant_scoping(api_client: Any, db_session: Any, seeded: Any) -> None:
    await _seed_period(db_session, TENANT_ID, period_key="2026-10", start=date(2026, 10, 1), end=date(2026, 10, 31))
    await _seed_period(
        db_session,
        OTHER_TENANT_ID,
        period_key="2026-11",
        start=date(2026, 11, 1),
        end=date(2026, 11, 30),
        status="CLOSED",
    )
    response = await api_client.get(PERIODS, params={"date": "2026-10-15"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OPEN"
    assert body["period_key"] == "2026-10"
    assert body["start_date"] == "2026-10-01"

    # The other tenant's period must not leak through the principal's tenant scope.
    other = await api_client.get(PERIODS, params={"date": "2026-11-03"})
    assert other.status_code == 404
    assert other.json()["code"] == "PERIOD_NOT_FOUND"

    unknown = await api_client.get(PERIODS, params={"date": "2027-01-01"})
    assert unknown.status_code == 404

    malformed = await api_client.get(PERIODS, params={"date": "not-a-date"})
    assert malformed.status_code == 422

    missing = await api_client.get(PERIODS)
    assert missing.status_code == 422


async def test_period_status_scope_denied(api_client: Any, admin_app: Any, limited_principal: Any) -> None:
    admin_app.dependency_overrides[get_current_principal] = lambda: limited_principal
    response = await api_client.get(PERIODS, params={"date": "2026-10-15"})
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_SCOPE"


async def test_exchange_rate_filters_and_tenant_isolation(api_client: Any, db_session: Any, seeded: Any) -> None:
    await _seed_rate(db_session, TENANT_ID, rate_date=date(2026, 10, 1), rate="0.91")
    await _seed_rate(db_session, TENANT_ID, base="USD", quote="GBP", rate_date=date(2026, 10, 2), rate="0.79")
    await _seed_rate(db_session, OTHER_TENANT_ID, base="USD", quote="JPY", rate_date=date(2026, 10, 3), rate="149.5")

    all_rates = await api_client.get(RATES)
    assert all_rates.status_code == 200
    body = all_rates.json()
    assert body["count"] == 2
    assert {item["quote_currency"] for item in body["items"]} == {"EUR", "GBP"}

    filtered = await api_client.get(RATES, params={"base": "usd", "quote": "gbp"})
    assert filtered.status_code == 200
    assert [item["quote_currency"] for item in filtered.json()["items"]] == ["GBP"]

    windowed = await api_client.get(RATES, params={"from": "2026-10-02", "to": "2026-10-02"})
    assert windowed.status_code == 200
    assert [item["rate_date"] for item in windowed.json()["items"]] == ["2026-10-02"]

    bad_currency = await api_client.get(RATES, params={"base": "US"})
    assert bad_currency.status_code == 422

    bad_window = await api_client.get(RATES, params={"from": "2026-10-05", "to": "2026-10-01"})
    assert bad_window.status_code == 422


async def test_internal_exchange_rate_upsert_idempotency_and_rejections(
    api_client: Any, db_session: Any, seeded: Any
) -> None:
    key = uuid4().hex
    payload = {
        "rates": [
            {"base_currency": "usd", "quote_currency": "eur", "rate_date": "2026-10-10", "rate": "0.92"},
            {
                "base_currency": "USD",
                "quote_currency": "GBP",
                "rate_date": "2026-10-10",
                "rate": "0.78",
                "source": "ecb",
            },
            {"base_currency": "US", "quote_currency": "EUR", "rate_date": "2026-10-10", "rate": "1.0"},
            {"base_currency": "USD", "quote_currency": "EUR", "rate_date": "not-a-date", "rate": "1.0"},
            {"base_currency": "USD", "quote_currency": "EUR", "rate_date": "2026-10-11", "rate": "-1"},
        ]
    }
    response = await api_client.put(INTERNAL_RATES, json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upserted"] == 2
    assert len(body["rejected"]) == 3

    count = await db_session.execute(
        select(func.count()).select_from(p.exchange_rate).where(p.exchange_rate.c.tenant_id == TENANT_ID)
    )
    assert int(count.scalar_one()) == 2

    # Same key + same payload replays the stored response without duplicating rows.
    replay = await api_client.put(INTERNAL_RATES, json=payload, headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert replay.json() == body
    count_after = await db_session.execute(
        select(func.count()).select_from(p.exchange_rate).where(p.exchange_rate.c.tenant_id == TENANT_ID)
    )
    assert int(count_after.scalar_one()) == 2

    # Same key + different payload is a conflict.
    conflict = await api_client.put(
        INTERNAL_RATES,
        json={"rates": [{"base_currency": "USD", "quote_currency": "CAD", "rate_date": "2026-10-10", "rate": "1.35"}]},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    missing_key = await api_client.put(INTERNAL_RATES, json={"rates": []})
    assert missing_key.status_code == 422


async def test_internal_exchange_rate_upsert_updates_existing_natural_key(
    api_client: Any, db_session: Any, seeded: Any
) -> None:
    await _seed_rate(db_session, TENANT_ID, rate_date=date(2026, 10, 10), rate="0.90")
    response = await api_client.put(
        INTERNAL_RATES,
        json={"rates": [{"base_currency": "USD", "quote_currency": "EUR", "rate_date": "2026-10-10", "rate": "0.95"}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 200
    row = await db_session.execute(
        select(p.exchange_rate.c.rate).where(
            p.exchange_rate.c.tenant_id == TENANT_ID,
            p.exchange_rate.c.base_currency == "USD",
            p.exchange_rate.c.quote_currency == "EUR",
            p.exchange_rate.c.rate_date == date(2026, 10, 10),
        )
    )
    assert str(row.scalar_one()) == "0.950000000000"

    denied = await api_client.put(INTERNAL_RATES, json={"rates": []})
    assert denied.status_code == 422
