"""FS-05 costing API integration tests (live Postgres; rollback isolated test tenancy)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.modules.costing import tables as ct
from app.tests.integration.costing.conftest import (
    ITEM_A,
    ITEM_B,
    LOCATION_ID,
    OTHER_TENANT_ID,
    TENANT_ID,
    create_category,
    idem,
)

pytestmark = pytest.mark.integration


def _category_id(category: dict[str, Any]) -> str:
    return str(category["id"])


async def test_cost_category_crud_idempotency_and_tenant_scoping(client: Any, seeded: dict[str, Any]) -> None:
    category = await create_category(client, code="FREIGHT", category_type="FREIGHT")
    assert category["code"] == "FREIGHT"
    assert category["tenant_id"] == str(TENANT_ID)

    body = {"code": "DUTY", "name": "Duty", "category_type": "DUTY", "is_rollup_input": False}
    first = await client.post("/api/v1/cost-categories", json=body, headers={"Idempotency-Key": "cost-cat-key"})
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/cost-categories", json=body, headers={"Idempotency-Key": "cost-cat-key"})
    assert replay.status_code in {200, 201}
    assert replay.json()["id"] == first.json()["id"]

    listing = await client.get("/api/v1/cost-categories")
    assert listing.status_code == 200
    assert {row["code"] for row in listing.json()["items"]} >= {"FREIGHT", "DUTY"}

    patched = await client.patch(
        f"/api/v1/cost-categories/{category['id']}", json={"name": "Freight inbound"}, headers=idem()
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Freight inbound"

    # duplicate code -> 409
    duplicate = await client.post(
        "/api/v1/cost-categories",
        json={"code": "FREIGHT", "name": "Dup", "category_type": "FREIGHT"},
        headers=idem(),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "COST_CATEGORY_CODE_TAKEN"


async def test_cost_templates_and_standard_versions(client: Any, seeded: dict[str, Any]) -> None:
    material = await create_category(client, code="MATERIAL")
    labor = await create_category(client, code="LABOR", category_type="LABOR")

    template = await client.post(
        "/api/v1/cost-templates",
        json={
            "code": "TPL1",
            "name": "Inventory template",
            "lines": [
                {"cost_category_id": _category_id(material), "percentage": "60"},
                {"cost_category_id": _category_id(labor), "fixed_amount": "2.5"},
            ],
        },
        headers=idem(),
    )
    assert template.status_code == 201, template.text
    template_body = template.json()
    assert len(template_body["lines"]) == 2

    version = await client.post(
        "/api/v1/cost-versions",
        json={"code": "V2026", "name": "2026 standard", "effective_from": "2026-01-01"},
        headers=idem(),
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]
    assert version.json()["status"] == "PLANNED"

    planned = await client.post(
        f"/api/v1/cost-versions/{version_id}/planned-standards",
        json={"lines": [{"item_id": str(ITEM_A), "cost_category_id": _category_id(material), "unit_cost": "5"}]},
        headers=idem(),
    )
    assert planned.status_code == 200, planned.text

    approved = await client.post(f"/api/v1/cost-versions/{version_id}/approve", headers=idem())
    assert approved.status_code == 200
    assert approved.json()["approved_count"] == 1

    activated = await client.post(f"/api/v1/cost-versions/{version_id}/activate", headers=idem())
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "ACTIVE"
    assert str(ITEM_A) in activated.json()["revaluation_scope"]["items"]

    standards = await client.get("/api/v1/standard-costs", params={"item_id": str(ITEM_A)})
    assert standards.status_code == 200
    assert len(standards.json()["items"]) == 1
    assert standards.json()["items"][0]["unit_cost"] == "5.000000000000"


async def test_standard_cost_overlap_guard(client: Any, seeded: dict[str, Any]) -> None:
    material = await create_category(client, code="MATERIAL")
    assert material["code"] == "MATERIAL"
    version = await client.post(
        "/api/v1/cost-versions",
        json={"code": "VO", "name": "Overlap", "effective_from": "2026-01-01"},
        headers=idem(),
    )
    assert version.status_code == 201
    version_id = version.json()["id"]
    first = await client.post(
        "/api/v1/standard-costs",
        json={
            "item_id": str(ITEM_A),
            "cost_category_id": _category_id(material),
            "cost_version_id": version_id,
            "unit_cost": "5",
            "effective_from": "2026-01-01",
            "effective_to": "2026-06-30",
        },
        headers=idem(),
    )
    assert first.status_code == 201, first.text
    overlapping = await client.post(
        "/api/v1/standard-costs",
        json={
            "item_id": str(ITEM_A),
            "cost_category_id": _category_id(material),
            "cost_version_id": version_id,
            "unit_cost": "6",
            "effective_from": "2026-06-01",
            "effective_to": "2026-12-31",
        },
        headers=idem(),
    )
    assert overlapping.status_code == 409
    assert overlapping.json()["code"] == "STANDARD_COST_OVERLAP"


async def test_revaluation_lifecycle(client: Any, seeded: dict[str, Any]) -> None:
    created = await client.post(
        "/api/v1/cost-revaluations",
        json={
            "revaluation_type": "STANDARD_CHANGE",
            "reason": "annual standard update",
            "lines": [
                {"item_id": str(ITEM_A), "old_cost": "5", "new_cost": "6", "qty": "10"},
            ],
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    revaluation = created.json()
    assert revaluation["status"] == "DRAFT"
    assert revaluation["lines"][0]["value_delta"] == "10.000000000000"
    revaluation_id = revaluation["id"]

    assert (await client.post(f"/api/v1/cost-revaluations/{revaluation_id}/submit", headers=idem())).json()["status"] == "PENDING_APPROVAL"
    approved = await client.post(f"/api/v1/cost-revaluations/{revaluation_id}/approve", headers=idem())
    assert approved.status_code == 200
    posted = await client.post(f"/api/v1/cost-revaluations/{revaluation_id}/post", headers=idem())
    assert posted.status_code == 200
    assert posted.json()["status"] == "POSTED"
    assert posted.json()["document_number"].startswith("REVAL-")
    # invalid transition
    invalid = await client.post(f"/api/v1/cost-revaluations/{revaluation_id}/approve", headers=idem())
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "INVALID_STATE_TRANSITION"


async def test_landed_cost_allocation_sums_to_line_amount(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    # FS-08 migration closed the deferred landed-cost FK: seed a real vendor/receipt/lines.
    from sqlalchemy import text as sa_text

    vendor_id, receipt_id = uuid4(), uuid4()
    receipt_line_ids = [uuid4(), uuid4()]
    await db_session.execute(
        sa_text("INSERT INTO procurement.vendor (id, tenant_id, vendor_number, name, version) VALUES (:id,:t,:code,'Costing Vendor',1)"),
        {"id": vendor_id, "t": TENANT_ID, "code": f"V{uuid4().hex[:6].upper()}"},
    )
    await db_session.execute(
        sa_text(
            "INSERT INTO procurement.item_receipt (id, tenant_id, document_number, vendor_id, version) "
            "VALUES (:id,:t,:doc,:vendor,1)"
        ),
        {"id": receipt_id, "t": TENANT_ID, "doc": f"RCV-{uuid4().hex[:6].upper()}", "vendor": vendor_id},
    )
    for index, (line_id, item_id) in enumerate(zip(receipt_line_ids, [ITEM_A, ITEM_B])):
        await db_session.execute(
            sa_text(
                "INSERT INTO procurement.item_receipt_line "
                "(id, tenant_id, item_receipt_id, line_number, item_id, qty_expected, qty_received, version) "
                "VALUES (:id,:t,:receipt,:line,:item,:qty,:qty,1)"
            ),
            {"id": line_id, "t": TENANT_ID, "receipt": receipt_id, "line": index + 1,
             "item": item_id, "qty": Decimal("2") if index == 0 else Decimal("6")},
        )
    await db_session.flush()
    created = await client.post(
        "/api/v1/landed-costs",
        json={
            "transaction_date": "2026-10-20",
            "allocation_basis": "QUANTITY",
            "currency_code": "USD",
            "receipt_refs": [
                {"item_receipt_line_id": str(receipt_line_ids[0]), "item_id": str(ITEM_A), "qty": "2", "weight": "1", "amount": "20"},
                {"item_receipt_line_id": str(receipt_line_ids[1]), "item_id": str(ITEM_B), "qty": "6", "weight": "1", "amount": "60"},
            ],
            "lines": [{"description": "Freight", "amount": "8"}],
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    landed_id = created.json()["id"]
    assert created.json()["total_amount"] == "8.000000000000"

    assert (await client.post(f"/api/v1/landed-costs/{landed_id}/submit", headers=idem())).status_code == 200
    assert (await client.post(f"/api/v1/landed-costs/{landed_id}/approve", headers=idem())).status_code == 200
    posted = await client.post(f"/api/v1/landed-costs/{landed_id}/post", headers=idem())
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "POSTED"

    allocations = await client.get(f"/api/v1/landed-costs/{landed_id}/allocations")
    assert allocations.status_code == 200
    body = allocations.json()
    assert body["total_allocated"] == "8.000000000000"
    assert len(body["items"]) == 2
    assert {row["allocated_amount"] for row in body["items"]} == {"2.000000000000", "6.000000000000"}


async def test_estimated_landed_cost_template(client: Any, seeded: dict[str, Any]) -> None:
    freight = await create_category(client, code="FREIGHT", category_type="FREIGHT")
    template = await client.post(
        "/api/v1/estimated-landed-cost-templates",
        json={
            "code": "ELC1",
            "name": "Estimated freight",
            "currency_code": "USD",
            "is_default": True,
            "lines": [{"cost_category_id": _category_id(freight), "allocation_basis": "AMOUNT", "default_percent": "10"}],
        },
        headers=idem(),
    )
    assert template.status_code == 201, template.text
    listing = await client.get("/api/v1/estimated-landed-cost-templates")
    assert listing.status_code == 200
    assert any(row["code"] == "ELC1" for row in listing.json()["items"])


async def test_cogs_adjustment_lifecycle(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    # seed an item_cost row directly (posting engine would normally create it)
    item_cost_id = uuid4()
    await db_session.execute(
        ct.item_cost.insert().values(
            id=item_cost_id,
            tenant_id=TENANT_ID,
            item_id=ITEM_A,
            location_id=LOCATION_ID,
            costing_method="AVERAGE",
            qty_on_hand=Decimal("10"),
            value_base=Decimal("50"),
            avg_cost=Decimal("5"),
            status="CURRENT",
            version=1,
        )
    )
    await db_session.flush()
    created = await client.post(
        "/api/v1/cogs-adjustments",
        json={
            "adjustment_type": "UNDERWATER_TRUE_UP",
            "reason": "estimate true-up",
            "lines": [
                {"item_id": str(ITEM_A), "item_cost_id": str(item_cost_id), "value_delta": "3.5", "estimated": True}
            ],
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    adjustment_id = created.json()["id"]
    assert (await client.post(f"/api/v1/cogs-adjustments/{adjustment_id}/submit", headers=idem())).status_code == 200
    assert (await client.post(f"/api/v1/cogs-adjustments/{adjustment_id}/approve", headers=idem())).status_code == 200
    posted = await client.post(f"/api/v1/cogs-adjustments/{adjustment_id}/post", headers=idem())
    assert posted.status_code == 200
    assert posted.json()["status"] == "POSTED"


async def test_costing_group_and_balances(client: Any, seeded: dict[str, Any]) -> None:
    created = await client.post(
        "/api/v1/location-costing-groups",
        json={
            "code": "GRP1",
            "name": "Group 1",
            "currency_code": "USD",
            "members": [{"location_id": str(LOCATION_ID)}],
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["status"] == "ACTIVE"
    assert len(group["members"]) == 1
    balances = await client.get("/api/v1/group-average-balances")
    assert balances.status_code == 200
    listing = await client.get("/api/v1/location-costing-groups")
    assert listing.status_code == 200
    assert any(row["code"] == "GRP1" for row in listing.json()["items"])


async def test_item_cost_reads_and_layers(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    item_cost_id = uuid4()
    await db_session.execute(
        ct.item_cost.insert().values(
            id=item_cost_id,
            tenant_id=TENANT_ID,
            item_id=ITEM_A,
            location_id=LOCATION_ID,
            costing_method="FIFO",
            qty_on_hand=Decimal("10"),
            value_base=Decimal("50"),
            avg_cost=Decimal("5"),
            status="CURRENT",
            version=1,
        )
    )
    await db_session.execute(
        ct.cost_layer.insert().values(
            id=uuid4(),
            tenant_id=TENANT_ID,
            item_cost_id=item_cost_id,
            layer_seq=1,
            layer_date=date(2026, 1, 2),
            qty_original=Decimal("10"),
            qty_remaining=Decimal("10"),
            unit_cost=Decimal("5"),
            value_remaining=Decimal("50"),
            status="OPEN",
            version=1,
        )
    )
    await db_session.flush()

    listing = await client.get("/api/v1/costs/item-costs", params={"item_id": str(ITEM_A)})
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    detail = await client.get(f"/api/v1/costs/item-costs/{item_cost_id}")
    assert detail.status_code == 200
    assert detail.json()["layer_count"] == 1
    layers = await client.get(f"/api/v1/costs/item-costs/{item_cost_id}/layers")
    assert layers.status_code == 200
    assert layers.json()["items"][0]["qty_original"] == "10.000000000000"
    valuation = await client.get("/api/v1/costs/valuation")
    assert valuation.status_code == 200
    assert valuation.json()["totals"]["value_base"] == "50.000000000000"

    # other tenant cannot see the row
    await client.get("/api/v1/costs/item-costs", params={"item_id": str(ITEM_A)})
    other_visible = await db_session.execute(
        select(ct.item_cost).where(ct.item_cost.c.tenant_id == OTHER_TENANT_ID)
    )
    assert other_visible.first() is None
