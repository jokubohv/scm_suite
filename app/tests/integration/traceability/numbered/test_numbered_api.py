"""FS-06 numbered-inventory API integration tests (live Postgres; graceful skip when unavailable)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.api.deps import get_current_principal
from app.core.models import platform as platform_models
from app.modules.inventory_ledger import tables as ledger
from app.tests.integration.traceability.numbered.conftest import idem

pytestmark = pytest.mark.integration


async def _create_lot(client: Any, item_id: Any, *, number: str, days: int = 90, **extra: Any) -> dict[str, Any]:
    payload = {
        "item_id": str(item_id),
        "lot_number": number,
        "expiry_date": (date.today() + timedelta(days=days)).isoformat(),
        **extra,
    }
    response = await client.post("/api/v1/lots", json=payload, headers=idem())
    assert response.status_code == 201, response.text
    return response.json()["lot"]


async def _create_serial(client: Any, item_id: Any, *, number: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/serials",
        json={"item_id": str(item_id), "serial_number": number, **extra},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["serial"]


async def test_lot_lifecycle_and_idempotency(client: Any, seeded: dict[str, Any]) -> None:
    lot = await _create_lot(client, seeded["lot_item"], number="LOT-A1")
    assert lot["lot_number"] == "LOT-A1"
    assert lot["is_active"] is True
    assert lot["version"] == 1

    duplicate = await client.post(
        "/api/v1/lots",
        json={"item_id": str(seeded["lot_item"]), "lot_number": "LOT-A1"},
        headers=idem(),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "LOT_CONFLICT"

    fetched = await client.get(f"/api/v1/lots/{lot['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["lot"]["lot_number"] == "LOT-A1"

    listed = await client.get("/api/v1/lots", params={"item_id": str(seeded["lot_item"])})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [lot["id"]]

    patched = await client.patch(f"/api/v1/lots/{lot['id']}", json={"notes": "reviewed"}, headers={"If-Match": "1"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["lot"]["notes"] == "reviewed"
    assert patched.json()["lot"]["version"] == 2

    stale = await client.patch(f"/api/v1/lots/{lot['id']}", json={"notes": "again"}, headers={"If-Match": "1"})
    assert stale.status_code == 412
    assert stale.json()["code"] == "VERSION_CONFLICT"

    key = idem()
    first = await client.post(f"/api/v1/lots/{lot['id']}/inactivate", json={"reason": "obsolete"}, headers=key)
    assert first.status_code == 200, first.text
    replay = await client.post(f"/api/v1/lots/{lot['id']}/inactivate", json={"reason": "obsolete"}, headers=key)
    assert replay.status_code == 200
    assert replay.json()["lot"]["is_active"] is False


async def test_lot_patch_immutable_after_movements(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    lot = await _create_lot(client, seeded["lot_item"], number="LOT-M1")
    transaction_id, line_id = uuid4(), uuid4()
    await db_session.execute(
        insert(ledger.inventory_transaction).values(
            id=transaction_id,
            tenant_id=seeded["tenant_id"],
            transaction_type="T-01",
            transaction_date=date.today(),
            ledger_sequence=1,
        )
    )
    await db_session.execute(
        insert(ledger.inventory_transaction_line).values(
            id=line_id,
            tenant_id=seeded["tenant_id"],
            inventory_transaction_id=transaction_id,
            line_number=1,
            item_id=seeded["lot_item"],
            location_id=seeded["location_id"],
            inventory_status_id=seeded["status_id"],
            qty_uom=Decimal("1"),
            qty_base=Decimal("1"),
            movement_type="RECEIPT",
            transaction_date=date.today(),
            lot_id=lot["id"],
        )
    )
    response = await client.patch(
        f"/api/v1/lots/{lot['id']}", json={"expiry_date": (date.today() + timedelta(days=5)).isoformat()}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "IDENTITY_IMMUTABLE"


async def test_serial_registration_and_history(client: Any, seeded: dict[str, Any]) -> None:
    serial = await _create_serial(client, seeded["serial_item"], number="SN-0001")
    assert serial["state"] == "UNKNOWN"
    duplicate = await client.post(
        "/api/v1/serials", json={"item_id": str(seeded["serial_item"]), "serial_number": "SN-0001"}, headers=idem()
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "SERIAL_CONFLICT"

    fetched = await client.get(f"/api/v1/serials/{serial['id']}", params={"include_history": "true"})
    assert fetched.status_code == 200
    body = fetched.json()["serial"]
    assert body["serial_number"] == "SN-0001"
    assert body["history"] == []

    history = await client.get(f"/api/v1/serials/{serial['id']}/history")
    assert history.status_code == 200
    assert history.json()["items"] == []

    item_serials = await client.get(f"/api/v1/items/{seeded['serial_item']}/serials")
    assert item_serials.status_code == 200
    assert [row["id"] for row in item_serials.json()["items"]] == [serial["id"]]


async def test_numbered_validate_and_fefo_plan(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    lot_old = await _create_lot(client, seeded["lot_item"], number="LOT-OLD", days=30)
    lot_new = await _create_lot(client, seeded["lot_item"], number="LOT-NEW", days=120)
    for lot_id, qty in ((lot_old["id"], 5), (lot_new["id"], 4)):
        await db_session.execute(
            insert(ledger.inventory_balance_lot).values(
                id=uuid4(),
                tenant_id=seeded["tenant_id"],
                item_id=seeded["lot_item"],
                location_id=seeded["location_id"],
                lot_id=lot_id,
                inventory_status_id=seeded["status_id"],
                ownership="OWNED",
                qty_on_hand=qty,
                qty_committed=0,
                expiry_date=None,
            )
        )

    plan = await client.post(
        "/api/v1/numbered-inventory/fefo-plan",
        json={"item_id": str(seeded["lot_item"]), "location_id": str(seeded["location_id"]), "qty": "6"},
    )
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert [row["lot_number"] for row in body["allocations"]] == ["LOT-OLD", "LOT-NEW"]
    assert body["allocations"][0]["qty"] == "5.000000000000"
    assert body["allocations"][1]["qty"] == "1.000000000000"
    assert Decimal(body["unfilled_qty"]) == 0

    over = await client.post(
        "/api/v1/numbered-inventory/fefo-plan",
        json={"item_id": str(seeded["lot_item"]), "location_id": str(seeded["location_id"]), "qty": "20"},
    )
    assert over.status_code == 200
    assert Decimal(over.json()["unfilled_qty"]) == 11

    invalid = await client.post(
        "/api/v1/numbered-inventory/validate",
        json={
            "item_id": str(seeded["serial_item"]),
            "transaction_type": "T-12",
            "line": {"qty_base": "3", "details": [{"serial_id": str(uuid4()), "qty_base": "2"}]},
        },
    )
    assert invalid.status_code == 200
    codes = {violation["code"] for violation in invalid.json()["violations"]}
    assert {"SERIAL_QTY_MISMATCH", "DETAIL_QTY_SUM_MISMATCH"} <= codes

    valid = await client.post(
        "/api/v1/numbered-inventory/validate",
        json={
            "item_id": str(seeded["serial_item"]),
            "transaction_type": "T-12",
            "line": {"qty_base": "1", "details": [{"serial_id": str(uuid4()), "qty_base": "1"}]},
        },
    )
    assert valid.json()["valid"] is True

    search = await client.get("/api/v1/numbered-inventory/search", params={"item_id": str(seeded["lot_item"])})
    assert search.status_code == 200
    assert len(search.json()["lots"]) == 2

    status = await client.get("/api/v1/numbered-inventory/deferred-fks/status")
    assert status.status_code == 200
    report = status.json()
    assert report["validated"] is True
    assert report["orphan_counts"]
    assert all(count == 0 for count in report["orphan_counts"].values())


async def test_tenant_isolation(db_session: Any, seeded: dict[str, Any]) -> None:
    from app.api.deps import Principal
    from app.tests.integration.traceability.numbered.conftest import ALL_SCOPES

    tenant_principal = Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(seeded["tenant_id"]),
        token_version=0,
        scopes=ALL_SCOPES,
    )
    other_tenant_id = uuid4()
    other = Principal(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(other_tenant_id),
        token_version=0,
        scopes=ALL_SCOPES,
    )
    import httpx
    from fastapi import FastAPI

    from app.api.deps import get_db
    from app.api.errors import install_error_handlers
    from app.api.routers.traceability_numbered import router

    def _app(principal: Principal) -> FastAPI:
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(router)

        async def _db() -> Any:
            yield db_session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_principal] = lambda: principal
        return app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(tenant_principal)), base_url="http://test"
    ) as owner:
        lot = await _create_lot(owner, seeded["lot_item"], number="LOT-ISO")
    from app.tests.integration.traceability.numbered.conftest import _enable_domain, _tenant

    await _tenant(db_session, other_tenant_id, "NUMOTHER")
    await _enable_domain(db_session, other_tenant_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(other)), base_url="http://test") as stranger:
        cross = await stranger.get(f"/api/v1/lots/{lot['id']}")
        assert cross.status_code == 404
        listed = await stranger.get("/api/v1/lots")
        assert listed.json()["items"] == []


async def test_domain_flag_off_returns_domain_disabled(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    await db_session.execute(
        update(platform_models.metadata.tables["platform.feature_flag"])
        .where(platform_models.metadata.tables["platform.feature_flag"].c.tenant_id == seeded["tenant_id"])
        .values(is_enabled=False)
    )
    disabled = await client.post(
        "/api/v1/lots", json={"item_id": str(seeded["lot_item"]), "lot_number": "LOT-OFF"}, headers=idem()
    )
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "DOMAIN_DISABLED"
