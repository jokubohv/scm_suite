"""FS-05 residual: period-close/true-up processing marks OPEN T-06 absorption variances CLOSED."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.modules.costing.repository import CostingRepository
from app.modules.costing.service import CostingService

pytestmark = pytest.mark.integration


async def _create_variance(db_session: Any, tenant_id: str, item_id: str, *, amount: str = "5", bill_id: str | None = None) -> dict[str, Any]:
    repo = CostingRepository(db_session, tenant_id)
    return await repo.create_variance(
        {
            "variance_type": "LANDED_COST_ABSORPTION",
            "item_id": UUID(item_id),
            "amount_base": Decimal(amount),
            "account_ref": "LANDED_COST_ABSORPTION",
            "status": "OPEN",
            "detail": {"vendor_bill_id": bill_id or str(uuid4()), "estimated_amount": "25", "actual_amount": "30"},
        }
    )


async def test_process_open_absorption_variances_is_idempotent(db_session, seeded: dict[str, Any]) -> None:
    service = CostingService(db_session, seeded["tenant_id"])
    first = await _create_variance(db_session, seeded["tenant_id"], seeded["item_a"], amount="5")
    second = await _create_variance(db_session, seeded["tenant_id"], seeded["item_b"], amount="-3")
    result = await service.process_landed_cost_absorption_variances()
    assert result["processed"] == 2 and result["variance_type"] == "LANDED_COST_ABSORPTION"
    assert Decimal(result["total_amount_base"]) == Decimal("2")
    rows = {str(item["id"]): item for item in result["items"]}
    assert rows[str(first["id"])]["status"] == "CLOSED"
    assert rows[str(second["id"])]["status"] == "CLOSED"
    assert rows[str(first["id"])]["detail"]["processed_by"] == "period_close"

    replay = await service.process_landed_cost_absorption_variances()
    assert replay["processed"] == 0 and replay["items"] == []


async def test_process_can_target_specific_variances_and_tenant_is_scoped(db_session, seeded: dict[str, Any]) -> None:
    service = CostingService(db_session, seeded["tenant_id"])
    local = await _create_variance(db_session, seeded["tenant_id"], seeded["item_a"], amount="7")
    other = await _create_variance(db_session, seeded["other_tenant_id"], seeded["other_item"], amount="11")
    targeted = await service.process_landed_cost_absorption_variances(variance_ids=[str(local["id"])])
    assert targeted["processed"] == 1 and str(targeted["items"][0]["id"]) == str(local["id"])
    other_service = CostingService(db_session, seeded["other_tenant_id"])
    remaining = await other_service.list_variances(variance_type="LANDED_COST_ABSORPTION", status="OPEN")
    assert [str(item["id"]) for item in remaining["items"]] == [str(other["id"])]
    other_processed = await other_service.process_landed_cost_absorption_variances()
    assert other_processed["processed"] == 1
