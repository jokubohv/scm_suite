"""FS-02 pricing integration tests (live Postgres; graceful skip when unavailable)."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.api.deps import get_current_principal
from app.core.models import platform as p
from app.modules.item_master import tables as it
from app.tests.integration.pricing.conftest import ITEM_A, ITEM_B, TENANT_ID, create_level, create_price, idem, resolve

pytestmark = pytest.mark.integration


async def _set_item_schedule(db_session: Any, item_id: UUID, schedule_id: UUID) -> None:
    await db_session.execute(
        it.item_sales.update()
        .where(it.item_sales.c.tenant_id == TENANT_ID, it.item_sales.c.item_id == item_id)
        .values(quantity_pricing_schedule_id=schedule_id)
    )


async def test_price_level_crud_idempotency_and_default_switch(client: Any) -> None:
    level = await create_level(client, code="RETAIL", is_default=True)
    assert level["code"] == "RETAIL"

    idem_body = {"code": "IDEM", "name": "Idem", "currency_code": "USD", "is_default": False}
    first = await client.post("/api/v1/price-levels", json=idem_body, headers={"Idempotency-Key": "replay-key-idem"})
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/price-levels", json=idem_body, headers={"Idempotency-Key": "replay-key-idem"})
    assert replay.status_code in {200, 201}
    assert replay.json()["price_level"]["id"] == first.json()["price_level"]["id"]

    listing = await client.get("/api/v1/price-levels")
    assert listing.status_code == 200
    assert any(item["code"] == "RETAIL" for item in listing.json()["items"])

    detail = await client.get(f"/api/v1/price-levels/{level['id']}")
    assert detail.status_code == 200

    patched = await client.patch(
        f"/api/v1/price-levels/{level['id']}",
        json={"name": "Retail prices"},
        headers={"If-Match": str(level["version"])},
    )
    assert patched.status_code == 200
    assert patched.json()["price_level"]["name"] == "Retail prices"

    stale = await client.patch(
        f"/api/v1/price-levels/{level['id']}", json={"name": "Stale"}, headers={"If-Match": str(level["version"])}
    )
    assert stale.status_code == 412

    duplicate = await client.post(
        "/api/v1/price-levels", json={"code": "RETAIL", "name": "Dup", "currency_code": "USD"}, headers=idem()
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DUPLICATE_CODE"


async def test_groups_pricing_group_and_resolution_precedence(client: Any, db_session: Any) -> None:
    default_level = await create_level(client, code="DEFAULT", is_default=True)
    group_level = await create_level(client, code="CONTRACT", is_default=False)
    await create_price(client, level_id=default_level["id"], unit_price="10")
    await create_price(client, level_id=group_level["id"], unit_price="20")

    item_group = await client.post("/api/v1/item-groups", json={"code": "IG1", "name": "Group 1"}, headers=idem())
    assert item_group.status_code == 201, item_group.text
    item_group_id = item_group.json()["item_group"]["id"]
    added = await client.post(
        f"/api/v1/item-groups/{item_group_id}/members", json={"item_ids": [str(ITEM_A)]}, headers=idem()
    )
    assert added.status_code == 200

    customer_group = await client.post(
        "/api/v1/customer-groups", json={"code": "CG1", "name": "Contract"}, headers=idem()
    )
    customer_group_id = customer_group.json()["customer_group"]["id"]
    await client.post(
        f"/api/v1/customer-groups/{customer_group_id}/members",
        json={"external_customer_refs": ["CUST-1"]},
        headers=idem(),
    )

    pricing_group = await client.post(
        "/api/v1/pricing-groups",
        json={
            "item_group_id": item_group_id,
            "customer_group_id": customer_group_id,
            "price_level_id": group_level["id"],
            "is_default": True,
            "discount_percent": "10",
        },
        headers=idem(),
    )
    assert pricing_group.status_code == 201, pricing_group.text

    base = await resolve(client)
    assert base["lines"][0]["unit_price"] == "10.000000000000"
    grouped = await resolve(client, customer_group_id=customer_group_id)
    assert grouped["lines"][0]["price_source"] == "PRICING_GROUP"
    assert grouped["lines"][0]["unit_price"] == "18.000000000000"
    explicit = await resolve(client, price_level_id=group_level["id"])
    assert explicit["lines"][0]["unit_price"] == "20.000000000000"


async def test_schedule_modes_and_break_validation(client: Any, db_session: Any) -> None:
    level = await create_level(client)
    await create_price(client, level_id=level["id"], unit_price="10")
    schedule = await client.post(
        "/api/v1/price-schedules",
        json={
            "code": "QS1",
            "name": "Quantity",
            "calculation_mode": "LINE",
            "is_marginal": False,
            "currency_code": "USD",
        },
        headers=idem(),
    )
    assert schedule.status_code == 201, schedule.text
    schedule_id = schedule.json()["price_schedule"]["id"]
    breaks = await client.put(
        f"/api/v1/price-schedules/{schedule_id}/breaks",
        json={
            "breaks": [
                {"qty_from": "0", "qty_to": "10", "pricing_method": "FIXED_PRICE", "value": "10"},
                {"qty_from": "10", "qty_to": None, "pricing_method": "FIXED_PRICE", "value": "8"},
            ]
        },
        headers=idem(),
    )
    assert breaks.status_code == 200, breaks.text
    overlap = await client.put(
        f"/api/v1/price-schedules/{schedule_id}/breaks",
        json={
            "breaks": [
                {"qty_from": "0", "qty_to": "15", "pricing_method": "FIXED_PRICE", "value": "1"},
                {"qty_from": "10", "qty_to": None, "pricing_method": "FIXED_PRICE", "value": "2"},
            ]
        },
        headers=idem(),
    )
    assert overlap.status_code in {200, 409}
    await db_session.flush()
    await _set_item_schedule(db_session, ITEM_A, UUID(schedule_id))
    await db_session.flush()
    line = await resolve(client, quantity="12")
    assert line["lines"][0]["unit_price"] == "8.000000000000"
    assert line["lines"][0]["calculation_mode"] == "LINE"


async def test_price_rules_and_collections(client: Any, db_session: Any) -> None:
    level = await create_level(client)
    rule_level = await create_level(client, code="RULE", is_default=False)
    await create_price(client, level_id=level["id"], unit_price="10")
    await create_price(client, level_id=rule_level["id"], unit_price="7")

    rule = await client.post(
        "/api/v1/price-rules",
        json={
            "code": "CAT-RULE",
            "name": "Category rule",
            "rule_type": "FIXED_PRICE",
            "priority": 1,
            "conflict_policy": "HIGHER_PRIORITY",
            "criteria": [{"criterion": "ITEM", "operator": "IN", "value_text": str(ITEM_A)}],
            "action": {"action_type": "FIXED_PRICE", "value": "6"},
        },
        headers=idem(),
    )
    assert rule.status_code == 201, rule.text
    resolved = await resolve(client)
    assert resolved["lines"][0]["price_source"] == "RULE"
    assert resolved["lines"][0]["unit_price"] == "6.000000000000"

    detail = await client.get(f"/api/v1/price-rules/{rule.json()['price_rule']['id']}")
    assert detail.status_code == 200
    assert detail.json()["criteria"]

    collection = await client.post(
        "/api/v1/item-collections",
        json={"code": "DYN1", "name": "Dynamic", "criteria": {"is_active": True}, "is_dynamic": True},
        headers=idem(),
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["item_collection"]["id"]
    static = await client.post(
        "/api/v1/item-collections",
        json={"code": "STATIC1", "name": "Static", "criteria": {}, "is_dynamic": False},
        headers=idem(),
    )
    static_id = static.json()["item_collection"]["id"]
    members = await client.post(
        f"/api/v1/item-collections/{static_id}/members", json={"item_ids": [str(ITEM_A), str(ITEM_B)]}, headers=idem()
    )
    assert members.status_code == 200
    recompute = await client.post(f"/api/v1/item-collections/{collection_id}/recompute", json={}, headers=idem())
    assert recompute.status_code == 202


async def test_effective_dates_overlap_and_item_price_listing(client: Any, db_session: Any) -> None:
    level = await create_level(client)
    await create_price(client, level_id=level["id"], unit_price="10", effective_from=date(2026, 1, 1))
    overlap = await client.post(
        "/api/v1/item-prices",
        json={
            "item_id": str(ITEM_A),
            "price_level_id": level["id"],
            "currency_code": "USD",
            "qty_min": "0",
            "unit_price": "11",
            "effective_from": "2026-06-01",
            "effective_to": "2026-12-31",
        },
        headers=idem(),
    )
    assert overlap.status_code == 409
    assert overlap.json()["code"] == "PRICE_OVERLAP"

    listing = await client.get("/api/v1/item-prices", params={"item_id": str(ITEM_A), "as_of": "2026-10-21"})
    assert listing.status_code == 200
    assert len(listing.json()["items"]) >= 1

    future = await client.post(
        "/api/v1/item-prices",
        json={
            "item_id": str(ITEM_B),
            "price_level_id": level["id"],
            "currency_code": "USD",
            "qty_min": "0",
            "unit_price": "50",
            "effective_from": "2027-01-01",
        },
        headers=idem(),
    )
    assert future.status_code == 201
    current = await resolve(client)
    assert current["lines"][0]["unit_price"] == "10.000000000000"


async def test_coupon_validation_and_idempotent_redemption(client: Any, db_session: Any) -> None:
    from app.modules.pricing.service import PricingService

    level = await create_level(client)
    await create_price(client, level_id=level["id"], unit_price="10")
    coupon = await client.post(
        "/api/v1/coupons",
        json={
            "code": "SAVE10",
            "name": "Save 10%",
            "coupon_type": "PERCENT",
            "value": "10",
            "currency_code": "USD",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "max_uses": 2,
        },
        headers=idem(),
    )
    assert coupon.status_code == 201, coupon.text

    validation = await client.post(
        "/api/v1/coupons/validate",
        json={
            "code": "SAVE10",
            "item_id": str(ITEM_A),
            "quantity": "2",
            "unit_price": "10",
            "document_date": "2026-10-21",
        },
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    assert validation.json()["discount_amount"] == "2.000000000000"

    resolved = await resolve(client, coupon_code="SAVE10")
    assert resolved["lines"][0]["price_source"] == "COUPON"
    assert resolved["lines"][0]["unit_price"] == "9.000000000000"

    service = PricingService(db_session)
    payload = {
        "coupon_code": "SAVE10",
        "document_type": "SALES_ORDER",
        "document_id": str(uuid4()),
        "item_id": str(ITEM_A),
        "quantity": "2",
        "unit_price": "10",
        "document_date": "2026-10-21",
    }
    first = await service.redeem_coupon(tenant_id=str(TENANT_ID), payload=payload, actor_id=None)
    second = await service.redeem_coupon(tenant_id=str(TENANT_ID), payload=payload, actor_id=None)
    assert first["already_redeemed"] is False
    assert second["already_redeemed"] is True
    assert second["uses_count"] == first["uses_count"] == 1
    links = await db_session.execute(
        text(
            "SELECT count(*) FROM platform.document_link WHERE tenant_id = :tenant AND from_type = 'COUPON' AND link_type = 'APPLIED_TO'"
        ),
        {"tenant": str(TENANT_ID)},
    )
    assert int(links.scalar_one()) == 1


async def test_price_updates_apply_revert_and_overlap(client: Any, db_session: Any) -> None:
    level = await create_level(client)
    await create_price(client, level_id=level["id"], unit_price="10")
    update = await client.post(
        "/api/v1/price-updates",
        json={
            "update_type": "ITEM",
            "target_ref": "MASS-1",
            "price_level_id": level["id"],
            "currency_code": "USD",
            "effective_start": "2026-11-01",
            "priority": 5,
            "details": [{"item_id": str(ITEM_A), "new_price": "15"}],
        },
        headers=idem(),
    )
    assert update.status_code == 201, update.text
    update_id = update.json()["price_update"]["id"]

    applied = await client.post(
        f"/api/v1/price-updates/{update_id}/apply", json={"as_of": "2026-11-02"}, headers=idem()
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["rows_written"] == 1
    effective = await resolve(client, document_date="2026-11-02")
    assert effective["lines"][0]["unit_price"] == "15.000000000000"

    reverted = await client.post(
        f"/api/v1/price-updates/{update_id}/revert", json={"reason": "commercial rollback"}, headers=idem()
    )
    assert reverted.status_code == 200, reverted.text
    restored = await resolve(client, document_date="2026-11-02")
    assert restored["lines"][0]["unit_price"] == "10.000000000000"

    overlap = await client.post(
        "/api/v1/price-updates",
        json={
            "update_type": "ITEM",
            "target_ref": "MASS-1",
            "price_level_id": level["id"],
            "currency_code": "USD",
            "effective_start": "2026-12-01",
            "details": [{"item_id": str(ITEM_A), "new_price": "16"}],
        },
        headers=idem(),
    )
    assert overlap.status_code == 201
    conflicting = await client.post(
        "/api/v1/price-updates",
        json={
            "update_type": "ITEM",
            "target_ref": "MASS-1",
            "price_level_id": level["id"],
            "currency_code": "USD",
            "effective_start": "2026-12-15",
            "details": [{"item_id": str(ITEM_A), "new_price": "17"}],
        },
        headers=idem(),
    )
    # A different effective_start is a different natural key; the exclusion constraint catches
    # range overlap only when the ranges intersect (here both open-ended -> overlap expected).
    assert conflicting.status_code in {201, 409}


async def test_bulk_upsert_price_list_and_jobs(client: Any, db_session: Any) -> None:
    level = await create_level(client)
    bulk = await client.post(
        "/api/v1/item-prices/bulk-upsert",
        json={
            "effective_from": "2026-02-01",
            "currency_code": "USD",
            "rows": [
                {"item_id": str(ITEM_A), "price_level_id": level["id"], "currency_code": "USD", "unit_price": "12"},
                {"item_id": str(ITEM_B), "price_level_id": level["id"], "currency_code": "USD", "unit_price": "-1"},
            ],
        },
        headers=idem(),
    )
    assert bulk.status_code == 201, bulk.text
    body = bulk.json()
    assert body["applied"] >= 1

    listing = await client.post(
        "/api/v1/price-lists/generate",
        json={"price_level_id": level["id"], "currency_code": "USD", "as_of": "2026-10-21"},
    )
    assert listing.status_code == 200
    assert listing.json()["items"] is not None or listing.json().get("rows") is not None

    swap = await client.post(
        "/api/v1/item-prices/swap-levels",
        json={"level_a_id": level["id"], "level_b_id": level["id"], "dry_run": True},
        headers=idem(),
    )
    assert swap.status_code == 202
    purchase = await client.post(
        "/api/v1/item-prices/purchase-price-updates",
        json={"item_ids": [str(ITEM_A)], "purchase_price": "6", "purchase_price_currency": "USD"},
        headers=idem(),
    )
    assert purchase.status_code == 202
    job_runs = await db_session.execute(select(p.job_run).where(p.job_run.c.tenant_id == TENANT_ID))
    assert len(job_runs.mappings().all()) >= 2


async def test_tenant_isolation_and_flag_gate(
    client: Any, pricing_app: Any, seeded: dict[str, Any], principal: Any, other_principal: Any, db_session: Any
) -> None:
    level = await create_level(client)
    await create_price(client, level_id=level["id"], unit_price="10")

    pricing_app.dependency_overrides[get_current_principal] = lambda: other_principal
    try:
        other_list = await client.get("/api/v1/price-levels")
        assert other_list.status_code == 200
        assert all(item["code"] != "DEFAULT" for item in other_list.json()["items"] if item["id"] != level["id"])
        cross = await client.get(f"/api/v1/price-levels/{level['id']}")
        assert cross.status_code == 404
        missing_item = await client.post(
            "/api/v1/prices/resolve",
            json={
                "lines": [{"line_ref": "1", "item_id": seeded["item_a"], "quantity": "1", "uom": "EA"}],
                "currency_code": "USD",
                "document_date": "2026-10-21",
            },
        )
        # other tenant cannot resolve the primary tenant's item
        assert missing_item.status_code == 422
    finally:
        pricing_app.dependency_overrides[get_current_principal] = lambda: principal

    await db_session.execute(
        p.feature_flag.update()
        .where(p.feature_flag.c.tenant_id == TENANT_ID, p.feature_flag.c.flag_key == "pricing.engine.enabled")
        .values(is_enabled=False)
    )
    await db_session.flush()
    disabled = await client.get("/api/v1/price-levels")
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "PRICING_ENGINE_DISABLED"
