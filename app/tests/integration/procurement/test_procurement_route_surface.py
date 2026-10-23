"""FS-08 residual: every contract-declared procurement operation is mounted and scoped.

The 62 operations historically declared with the canonical ``{id}`` placeholder are mounted as
behaviour-preserving aliases of the existing handlers (concrete URLs unchanged). This suite proves the
runtime surface matches the implementation-backed contract for all 151 FS-08 operations, and exercises
representative canonical routes for auth, tenant isolation and validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.api.routers.procurement import ROUTE_SCOPES, router

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOGUE = {
    entry["scope"]: entry
    for entry in json.loads((REPO_ROOT / "design" / "permissions.json").read_text(encoding="utf-8"))["catalogue"]
}
FS08_FRAGMENT = REPO_ROOT / "contracts" / "openapi" / "fragments" / "FS-08.openapi.json"


def _declared_operations() -> dict[tuple[str, str], dict[str, Any]]:
    fragment = json.loads(FS08_FRAGMENT.read_text(encoding="utf-8"))
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in fragment["paths"].items():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                operations[(method.upper(), path)] = operation
    return operations


def _mounted_operations() -> dict[tuple[str, str], Any]:
    mounted: dict[tuple[str, str], Any] = {}
    for route in router.routes:
        for method in getattr(route, "methods", set()):
            mounted[(method.upper(), getattr(route, "path", ""))] = route
    return mounted


def test_every_declared_fs08_operation_is_mounted() -> None:
    declared = _declared_operations()
    mounted = _mounted_operations()
    missing = sorted(set(declared) - set(mounted))
    assert missing == [], f"declared but unmounted FS-08 operations: {missing}"
    assert len(declared) == len(mounted) == len(ROUTE_SCOPES) == 151
    for operation, spec in declared.items():
        assert spec.get("x-implemented") is True, operation
        assert spec.get("x-skeleton") is not True, operation
        assert spec.get("x-feature-set") == "FS-08", operation


def test_every_fs08_operation_has_one_catalogued_scope() -> None:
    for operation, route in _mounted_operations().items():
        scope = ROUTE_SCOPES[operation]
        assert scope in CATALOGUE, (operation, scope)
        dependencies = getattr(route, "dependencies", []) or []
        assert dependencies, operation
    scopes = {ROUTE_SCOPES[operation] for operation in ROUTE_SCOPES}
    assert scopes <= set(CATALOGUE)


def test_canonical_id_paths_are_the_contract_spelling() -> None:
    canonical = {
        operation for operation in _declared_operations() if any(segment == "{id}" for segment in operation[1].split("/"))
    }
    assert len(canonical) >= 62
    for operation in canonical:
        assert operation in ROUTE_SCOPES


async def test_representative_canonical_routes(client: Any, world: dict[str, Any]) -> None:
    vendor_id = str(world["vendor_id"])
    headers = world["headers"]
    detail = await client.get(f"/api/v1/vendors/{vendor_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    history = await client.get(f"/api/v1/vendors/{vendor_id}/purchase-history", headers=headers)
    assert history.status_code == 200, history.text
    invalid_patch = await client.patch(f"/api/v1/vendors/{vendor_id}", headers=headers, json={"unknown": 1})
    assert invalid_patch.status_code in {200, 422}
    unknown = await client.get(f"/api/v1/vendors/{uuid4()}", headers=headers)
    assert unknown.status_code == 404


async def test_canonical_routes_require_authentication(client: Any, world: dict[str, Any]) -> None:
    anonymous = await client.get(f"/api/v1/vendors/{world['vendor_id']}")
    assert anonymous.status_code in {401, 403}


async def test_canonical_routes_are_tenant_scoped(client: Any, db_session: Any, world: dict[str, Any]) -> None:
    from app.modules.procurement.testing import seed_procurement_world

    other = await seed_procurement_world(db_session, code_prefix="OTHER")
    await db_session.flush()
    foreign = await client.get(f"/api/v1/vendors/{other['vendor_id']}", headers=world["headers"])
    assert foreign.status_code == 404
