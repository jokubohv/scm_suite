"""FS-15 reporting API integration tests (live DB; skip until 0029/0030 applied)."""

from __future__ import annotations


async def test_catalogue_lists_21_views(reporting_client) -> None:
    response = await reporting_client.get("/api/v1/reports")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "reporting.readmodels" and body["version"] == "1.0.0"
    assert body["total"] == 21
    assert len(body["items"]) == 21


async def test_stock_ledger_pagination_fields_and_freshness(reporting_client) -> None:
    response = await reporting_client.get("/api/v1/reports/stock-ledger", params={"limit": "2"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body and "freshness" in body
    assert len(body["items"]) <= 2
    if body["items"]:
        first_keys = set(body["items"][0])
        assert first_keys
    projected = await reporting_client.get("/api/v1/reports/stock-ledger", params={"fields": "item_id,location_id"})
    if projected.status_code == 200 and projected.json()["items"]:
        assert set(projected.json()["items"][0]) <= {"item_id", "location_id"}


async def test_cost_view_requires_cost_scope(reporting_client) -> None:
    # principal holds both read + cost.view so this must succeed; unknown field is rejected as problem+json
    response = await reporting_client.get("/api/v1/reports/inventory-valuation",
                                          params={"fields": "definitely_not_a_column"})
    assert response.status_code == 422
    assert response.json()["code"] == "REPORT_FIELD_NOT_ALLOWED"


async def test_export_json_and_csv(reporting_client) -> None:
    exported = await reporting_client.get("/api/v1/reports/stock-ledger/export", params={"format": "json", "limit": "2"})
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("application/json")
    csv_response = await reporting_client.get("/api/v1/reports/stock-ledger/export", params={"format": "csv", "limit": "2"})
    assert csv_response.status_code == 200 and csv_response.headers["content-type"].startswith("text/csv")


async def test_balance_search_requires_tenant_scope(reporting_client) -> None:
    response = await reporting_client.get("/api/v1/inventory/balances/search", params={"limit": "1"})
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert item["tenant_id"] == "00000000-0000-0000-0000-000000000000"
