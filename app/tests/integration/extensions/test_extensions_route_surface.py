"""FS-14 residual: every contract-declared extension operation is mounted and canonical.

The frozen FS-14 fragment declares canonical ``{id}`` placeholders; the runtime mounts the equivalent
resource-specific routes plus behaviour-preserving ``{id}`` aliases and the collection-level repair-order
PATCH. This suite proves declared<->mounted parity and exercises canonical/auth/tenant behaviour.
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
from app.api.routers.extensions import ROUTE_SCOPES, router

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
FRAGMENT = REPO_ROOT / "contracts" / "openapi" / "fragments" / "FS-14.openapi.json"
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


def test_every_declared_fs14_operation_is_mounted() -> None:
    declared, mounted = _declared(), _mounted()
    assert sorted(set(declared) - set(mounted)) == []
    assert len(declared) == len(mounted) == len(ROUTE_SCOPES) == 53
    for operation, spec in declared.items():
        assert spec.get("x-feature-set") == "FS-14", operation
        if "x-implemented" in spec:
            assert spec["x-implemented"] is True, operation


def test_every_fs14_operation_has_one_catalogued_scope() -> None:
    for operation, route in _mounted().items():
        scope = ROUTE_SCOPES[operation]
        assert scope in CATALOGUE, (operation, scope)
        assert getattr(route, "dependencies", None), operation
    assert {scope for scope in ROUTE_SCOPES.values()} <= CATALOGUE


def test_canonical_id_paths_and_collection_patch_are_present() -> None:
    canonical = {operation for operation in _declared() if "{id}" in operation[1]}
    assert len(canonical) >= 16
    for operation in canonical:
        assert operation in ROUTE_SCOPES, operation
    assert ("PATCH", "/api/v1/warranty/repair-orders") in _mounted()


async def test_representative_canonical_routes(client: Any, world: dict[str, Any]) -> None:
    terms = await client.put(
        f"/api/v1/items/{world['item_id']}/warranty",
        json={"duration_months": 24, "terms": "standard", "coverage": {"parts": True, "labor": True}},
        headers=world["headers"],
    )
    assert terms.status_code == 200, terms.text
    registration = await client.post(
        "/api/v1/warranty/registrations",
        json={
            "item_id": str(world["item_id"]),
            "external_customer_ref": "CUST-RS-1",
            "external_invoice_ref": "INV-RS-1",
            "registered_at": "2026-01-15T00:00:00Z",
            "source": "MANUAL",
        },
        headers=world["headers"],
    )
    assert registration.status_code == 201, registration.text
    registration_id = registration.json()["id"]
    canonical = await client.get(f"/api/v1/warranty/registrations/{registration_id}", headers=world["headers"])
    assert canonical.status_code == 200, canonical.text
    unknown = await client.get(f"/api/v1/warranty/claims/{uuid4()}", headers=world["headers"])
    assert unknown.status_code == 404, unknown.text


async def test_collection_level_repair_order_patch(client: Any, world: dict[str, Any]) -> None:
    created = await client.post(
        "/api/v1/warranty/repair-orders",
        json={"item_id": str(world["item_id"]), "labor_cost": "10", "notes": "rma"},
        headers=world["headers"],
    )
    assert created.status_code == 201, created.text
    repair_id = created.json()["id"]
    patched = await client.patch(
        "/api/v1/warranty/repair-orders",
        json={"id": repair_id, "labor_cost": "25", "status": "IN_PROGRESS"},
        headers=world["headers"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "IN_PROGRESS"
    missing_id = await client.patch("/api/v1/warranty/repair-orders", json={"notes": "no id"}, headers=world["headers"])
    assert missing_id.status_code == 422, missing_id.text


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
        response = await anonymous.get(f"/api/v1/customer-parts/{uuid4()}")
    assert response.status_code in {401, 403}, response.text


async def test_canonical_routes_are_tenant_scoped(db_session: Any, client: Any, world: dict[str, Any]) -> None:
    from app.modules.extensions.testing import seed_extensions_world

    other = await seed_extensions_world(db_session, code_prefix="OTH")
    registration = await client.post(
        "/api/v1/warranty/registrations",
        json={
            "item_id": str(other["item_id"]),
            "external_customer_ref": "CUST-OTH-1",
            "external_invoice_ref": "INV-OTH-1",
            "registered_at": "2026-01-15T00:00:00Z",
            "source": "MANUAL",
        },
        headers=other["headers"],
    )
    assert registration.status_code == 201, registration.text
    response = await client.get(f"/api/v1/warranty/registrations/{registration.json()['id']}", headers=world["headers"])
    assert response.status_code == 404, response.text
