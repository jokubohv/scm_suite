"""FS-00 §6 spec-aligned alias paths (additive to the task-card paths)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.core.models import platform as p
from app.tests.integration.platform_config.conftest import TENANT_ID


async def test_reference_currency_and_fiscal_read_aliases(app_client: Any) -> None:
    currencies = await app_client.get("/api/v1/reference/currencies")
    assert currencies.status_code == 200
    assert {"USD", "EUR"}.issubset({item["code"] for item in currencies.json()["items"]})

    created = await app_client.post(
        "/api/v1/admin/fiscal-periods",
        json={"period_key": "2026-12", "start_date": "2026-12-01", "end_date": "2026-12-31"},
    )
    assert created.status_code == 201
    periods = await app_client.get("/api/v1/reference/fiscal-periods", params={"year": 2026})
    assert periods.status_code == 200
    assert [item["period_key"] for item in periods.json()["items"]] == ["2026-12"]
    assert periods.json()["items"][0]["status"] == "OPEN"


async def test_feature_flag_aliases_share_tenant_upsert(app_client: Any, db_session: Any) -> None:
    listed = await app_client.get("/api/v1/admin/feature-flags")
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    put = await app_client.put(
        "/api/v1/admin/feature-flags/inventory.ledger.enabled", json={"is_enabled": True, "notes": "alias put"}
    )
    assert put.status_code == 200, put.text
    assert put.json()["is_enabled"] is True

    # The task-card PATCH path sees and idempotently preserves the same row.
    patched = await app_client.patch(
        "/api/v1/admin/flags/inventory.ledger.enabled", json={"is_enabled": True, "notes": "alias put"}
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == put.json()["version"]

    alias_list = await app_client.get(
        "/api/v1/admin/feature-flags", params={"flag_key": "inventory.ledger.enabled"}
    )
    assert alias_list.json()["count"] == 1


async def test_number_sequence_alias_read(app_client: Any, db_session: Any) -> None:
    await db_session.execute(
        p.number_sequence.insert().values(
            id=uuid4(),
            tenant_id=TENANT_ID,
            doc_type="PO",
            scope_key="TENANT",
            prefix="PO",
            padding=6,
            next_value=7,
            reset_policy="NEVER",
            gap_policy="NON_GAPLESS",
        )
    )
    await db_session.flush()
    response = await app_client.get("/api/v1/admin/number-sequences", params={"doc_type": "PO"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["next_value"] == 7
