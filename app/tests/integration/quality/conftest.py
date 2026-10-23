"""FS-12 quality-management integration fixtures (live Postgres; graceful skip without DB).

Seeds tenants/item-master/registry/quality defaults directly through Core metadata (no dependency on
other contexts' services), mounts only the quality router and overrides DB + principal dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from app.api.deps import Principal, get_current_principal, get_db
from app.api.errors import install_error_handlers
from app.api.routers.quality import router
from app.core.models import platform as platform_models
from app.modules.item_master import tables as it
from app.modules.quality import tables as q
from app.modules.traceability import tables as reg

ALL_SCOPES = frozenset(
    {
        "quality.read",
        "quality.spec.write",
        "quality.inspect",
        "quality.disposition",
        "quality.coa.write",
        "quality.admin",
    }
)

ITEM_ID = uuid4()
LOCATION_ID = uuid4()
AVAILABLE_STATUS = uuid4()
HOLD_STATUS = uuid4()


async def seed_tenant(session: Any, tenant_id: Any, suffix: str) -> dict[str, Any]:
    await session.execute(
        insert(platform_models.tenant).values(
            id=tenant_id, tenant_id=tenant_id, code=f"QA{suffix}", name=f"QA {suffix}", timezone="UTC", status="ACTIVE"
        )
    )
    await session.execute(
        insert(platform_models.feature_flag).values(
            id=uuid4(), tenant_id=tenant_id, flag_key="quality.management.enabled", is_enabled=True, source="MIGRATION"
        )
    )
    for key, value, value_type in (
        ("hold_status_code", "HOLD", "text"),
        ("hold_release_status_code", "AVAILABLE", "text"),
        ("default_queue_code", "DEFAULT", "text"),
        ("coa_sampling_eligible", ["FULL", "SAMPLE_RANDOM"], "json"),
        ("derived_fields", [], "json"),
        ("hold_requires_approval", True, "boolean"),
    ):
        import json

        raw = (
            json.dumps(value)
            if value_type == "json"
            else (str(value).lower() if isinstance(value, bool) else json.dumps(value))
        )
        await session.execute(
            insert(platform_models.tenant_setting).values(
                id=uuid4(),
                tenant_id=tenant_id,
                namespace="quality",
                key=key,
                value=raw,
                value_type=value_type,
                is_default=True,
            )
        )
    for doc_type, prefix in (("INSP", "INSP-"), ("COA", "COA-")):
        await session.execute(
            insert(platform_models.number_sequence).values(
                id=uuid4(),
                tenant_id=tenant_id,
                doc_type=doc_type,
                scope_key="TENANT",
                prefix=prefix,
                padding=6,
                next_value=1,
                reset_policy="NEVER",
                gap_policy="NON_GAPLESS",
            )
        )
    await session.execute(
        insert(q.inspection_queue).values(
            id=uuid4(),
            tenant_id=tenant_id,
            code="DEFAULT",
            name="Default queue",
            assignment_strategy="MANUAL",
            is_active=True,
        )
    )
    type_id = uuid4()
    await session.execute(
        insert(it.item_type_definition).values(id=type_id, tenant_id=tenant_id, code="INVENTORY", name="Inventory")
    )
    await session.execute(
        insert(it.item).values(
            id=ITEM_ID,
            tenant_id=tenant_id,
            item_number=f"QA-ITEM-{suffix}",
            display_name=f"QA Item {suffix}",
            item_type_id=type_id,
            is_active=True,
            created_from="MANUAL",
        )
    )
    await session.execute(
        insert(it.item_inventory).values(tenant_id=tenant_id, item_id=ITEM_ID, costing_method="AVERAGE")
    )
    await session.execute(
        insert(reg.location).values(
            id=LOCATION_ID, tenant_id=tenant_id, code="WH1", name="WH1", location_type="WAREHOUSE"
        )
    )
    for status_id, code, committable, sellable, default in (
        (AVAILABLE_STATUS, "AVAILABLE", True, True, True),
        (HOLD_STATUS, "HOLD", False, False, False),
    ):
        await session.execute(
            insert(reg.inventory_status).values(
                id=status_id,
                tenant_id=tenant_id,
                code=code,
                name=code,
                is_available_for_allocation=committable,
                is_committable=committable,
                is_sellable=sellable,
                is_default=default,
                is_system=True,
            )
        )
    return {"tenant_id": tenant_id, "item_id": ITEM_ID}


@pytest.fixture()
async def seeded(db_session: Any) -> dict[str, Any]:
    tenant_id = uuid4()
    data = await seed_tenant(db_session, tenant_id, tenant_id.hex[:6].upper())
    await db_session.flush()
    return {
        **data,
        "location_id": LOCATION_ID,
        "available_status": AVAILABLE_STATUS,
        "hold_status": HOLD_STATUS,
    }


@pytest.fixture()
def principal(seeded: dict[str, Any]) -> Principal:
    return Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(seeded["tenant_id"]),
        token_version=0,
        scopes=ALL_SCOPES,
    )


@pytest.fixture()
def quality_app(db_session: Any, principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> AsyncIterator[Any]:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    return app


@pytest.fixture()
async def client(quality_app: FastAPI) -> AsyncIterator[Any]:
    transport = httpx.ASGITransport(app=quality_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


def spec_payload(item_id: Any, *, code: str = "SPEC-1") -> dict[str, Any]:
    return {
        "code": code,
        "name": f"{code} spec",
        "item_id": str(item_id),
        "lines": [
            {"characteristic": "length", "result_type": "NUMERIC", "min_value": "9", "max_value": "11"},
            {"characteristic": "visual", "result_type": "PASS_FAIL"},
            {"characteristic": "internal-note", "result_type": "TEXT", "is_internal": True},
        ],
    }


def plan_payload(spec_id: str, item_id: Any, *, code: str = "PLAN-1") -> dict[str, Any]:
    return {
        "code": code,
        "name": f"{code} plan",
        "item_id": str(item_id),
        "quality_spec_id": spec_id,
        "inspection_type": "INCOMING",
        "sampling_method": "FULL",
        "sample_size": 1,
    }


async def create_active_spec(client: Any, item_id: Any, *, code: str = "SPEC-1") -> dict[str, Any]:
    created = await client.post("/api/v1/quality/specs", json=spec_payload(item_id, code=code), headers=idem())
    assert created.status_code == 201, created.text
    spec_id = created.json()["spec"]["id"]
    activated = await client.post(f"/api/v1/quality/specs/{spec_id}/activate", json={}, headers=idem())
    assert activated.status_code == 200, activated.text
    return activated.json()["spec"]


async def create_plan(client: Any, spec_id: str, item_id: Any, *, code: str = "PLAN-1") -> dict[str, Any]:
    created = await client.post(
        "/api/v1/quality/inspection-plans", json=plan_payload(spec_id, item_id, code=code), headers=idem()
    )
    assert created.status_code == 201, created.text
    return created.json()["plan"]


async def create_inspection(client: Any, seeded: dict[str, Any], plan_id: str) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/quality/inspections",
        json={
            "inspection_plan_id": plan_id,
            "item_id": str(seeded["item_id"]),
            "location_id": str(seeded["location_id"]),
            "source_entity_type": "ITEM_RECEIPT",
            "source_entity_id": str(uuid4()),
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    return created.json()["inspection"]


async def record_and_complete(
    client: Any, seeded: dict[str, Any], inspection_id: str, *, fail: bool = False, disposition: str | None = None
) -> dict[str, Any]:
    started = await client.post(f"/api/v1/quality/inspections/{inspection_id}/start", json={})
    assert started.status_code == 200, started.text
    results = await client.post(
        f"/api/v1/quality/inspections/{inspection_id}/results",
        json={
            "results": [
                {
                    "sequence": 1,
                    "characteristic": "length",
                    "result_type": "NUMERIC",
                    "measured_value_number": "12" if fail else "10",
                },
                {
                    "sequence": 2,
                    "characteristic": "visual",
                    "result_type": "PASS_FAIL",
                    "measured_value_text": "FAIL" if fail else "PASS",
                },
            ]
        },
    )
    assert results.status_code == 200, results.text
    body = {"notes": "integration"}
    if disposition:
        body["disposition"] = disposition
    completed = await client.post(f"/api/v1/quality/inspections/{inspection_id}/complete", json=body, headers=idem())
    assert completed.status_code == 200, completed.text
    return completed.json()["inspection"]
