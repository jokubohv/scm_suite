"""FS-13 residual: every contract-declared consignment operation is mounted and canonical.

The frozen FS-13 fragment declares canonical ``{id}`` placeholders; the runtime mounts the equivalent
``{agreement_id}``/``{transfer_id}`` routes plus behaviour-preserving ``{id}`` aliases. This suite proves
declared<->mounted parity and exercises canonical/auth/tenant behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_db
from app.api.errors import install_error_handlers
from app.api.routers.consigned import ROUTE_SCOPES, router

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
FRAGMENT = REPO_ROOT / "contracts" / "openapi" / "fragments" / "FS-13.openapi.json"
CATALOGUE = {
    entry["scope"]
    for entry in json.loads((REPO_ROOT / "design" / "permissions.json").read_text(encoding="utf-8"))["catalogue"]
}


def _declared() -> dict[tuple[str, str], dict[str, Any]]:
    fragment = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    return {
        (method.upper(), path): operation
        for path, item in fragment["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }


def _mounted() -> dict[tuple[str, str], Any]:
    return {
        (method.upper(), route.path): route
        for route in router.routes
        for method in getattr(route, "methods", set())
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }


def test_every_declared_fs13_operation_is_mounted() -> None:
    declared, mounted = _declared(), _mounted()
    assert sorted(set(declared) - set(mounted)) == []
    assert len(declared) == len(mounted) == len(ROUTE_SCOPES) == 39
    for operation, spec in declared.items():
        assert spec.get("x-feature-set") == "FS-13", operation
        if "x-implemented" in spec:
            assert spec["x-implemented"] is True, operation


def test_every_fs13_operation_has_one_catalogued_scope() -> None:
    for operation, route in _mounted().items():
        scope = ROUTE_SCOPES[operation]
        assert scope in CATALOGUE, (operation, scope)
        assert getattr(route, "dependencies", None), operation
    assert {scope for scope in ROUTE_SCOPES.values()} <= CATALOGUE


def test_canonical_id_paths_are_present() -> None:
    canonical = {operation for operation in _declared() if "{id}" in operation[1]}
    assert len(canonical) >= 13
    for operation in canonical:
        assert operation in ROUTE_SCOPES, operation


async def _agreement_with_item(client: Any, world: dict[str, Any]) -> dict[str, Any]:
    agreement = await client.post(
        "/api/v1/consignment-agreements",
        json={"vendor_id": str(world["vendor_id"]), "start_date": "2026-01-01", "payment_trigger": "ON_SALE", "currency_code": "USD"},
        headers=world["headers"],
    )
    assert agreement.status_code == 201, agreement.text
    agreement_id = agreement.json()["id"]
    covered = await client.post(
        f"/api/v1/consignment-agreements/{agreement_id}/items",
        json={
            "item_id": str(world["item_id"]),
            "location_id": str(world["location_id"]),
            "consigned_status_id": str(world["consigned_status_id"]),
            "owned_status_id": str(world["owned_status_id"]),
            "unit_cost": "5",
            "min_qty": 0,
            "max_qty": 100,
        },
        headers=world["headers"],
    )
    assert covered.status_code == 201, covered.text
    activated = await client.post(
        f"/api/v1/consignment-agreements/{agreement_id}/activate", json={}, headers=world["headers"]
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


async def test_representative_canonical_routes(client: Any, world: dict[str, Any]) -> None:
    agreement = await _agreement_with_item(client, world)
    canonical = await client.get(f"/api/v1/consignment-agreements/{agreement['id']}", headers=world["headers"])
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["id"] == agreement["id"]
    items = await client.get(f"/api/v1/consignment-agreements/{agreement['id']}/items", headers=world["headers"])
    assert items.status_code == 200, items.text
    assert items.json()["items"]
    unknown = await client.get(f"/api/v1/consignment-agreements/{uuid4()}", headers=world["headers"])
    assert unknown.status_code == 404, unknown.text


async def _anonymous_client(db_session: Any) -> httpx.AsyncClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _override_db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_canonical_routes_require_authentication(db_session: Any) -> None:
    async with await _anonymous_client(db_session) as anonymous:
        response = await anonymous.get(f"/api/v1/consignment-agreements/{uuid4()}")
    assert response.status_code in {401, 403}, response.text


async def test_canonical_routes_are_tenant_scoped(db_session: Any, client: Any, world: dict[str, Any]) -> None:
    from app.modules.consigned.testing import seed_consigned_world

    other = await seed_consigned_world(db_session, code_prefix="OTH")
    foreign = await _agreement_with_item(client, other)
    response = await client.get(f"/api/v1/consignment-agreements/{foreign['id']}", headers=world["headers"])
    assert response.status_code == 404, response.text
