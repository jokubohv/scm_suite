"""FS-05 residual: contract-declared canonical routes are mounted, scoped and tenant-isolated.

30 of the 35 contract-declared-but-unmounted FS-05 operations are mounted at their exact contract
spelling as behaviour-preserving aliases (the canonical ``{id}`` placeholder rebinds to the existing
handler parameter) or two genuine gap handlers (ELC-template PATCH, group inactivate). Five malformed
spec-extraction paths remain unmounted and are asserted here as the documented exception set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.api.deps import get_db
from app.api.errors import install_error_handlers
from app.api.routers.costing import ROUTE_SCOPES, router
from app.modules.costing.service import CostingService

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOGUE = {
    entry["scope"]: entry
    for entry in json.loads((REPO_ROOT / "design" / "permissions.json").read_text(encoding="utf-8"))["catalogue"]
}
FS05_FRAGMENT = REPO_ROOT / "app" / "contracts" / "costing" / "openapi.fragment.json"
AGGREGATE = REPO_ROOT / "contracts" / "openapi" / "openapi.json"
METHODS = {"get", "post", "put", "patch", "delete"}

#: Deliberate exceptions: malformed/duplicated paths emitted by the FS-05 spec extraction. They carry no
#: resource id or repeat the resource prefix, so no behaviour-preserving canonical route can be mounted.
EXCEPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/cogs-adjustments/{id}"),
        ("POST", "/api/v1/cost-revaluations/approve"),
        ("POST", "/api/v1/cost-revaluations/v1/cost-revaluations/{id}/submit"),
        ("POST", "/api/v1/cost-revaluations/{id}/v1/cost-revaluations/{id}/submit"),
        ("POST", "/api/v1/landed-costs/{id}/allocations/v1/landed-costs/{id}/submit"),
    }
)


def _ops(spec: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): operation
        for path, item in spec.get("paths", {}).items()
        for method, operation in item.items()
        if method in METHODS
    }


def _mounted() -> dict[tuple[str, str], Any]:
    mounted: dict[tuple[str, str], Any] = {}
    for route in router.routes:
        for method in getattr(route, "methods", set()):
            mounted[(method.upper(), getattr(route, "path", ""))] = route
    return mounted


def _aggregate_fs05() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        operation: spec
        for operation, spec in _ops(json.loads(AGGREGATE.read_text(encoding="utf-8"))).items()
        if spec.get("x-feature-set") == "FS-05"
    }


def test_fs05_fragment_is_implementation_backed_and_fully_mounted() -> None:
    fragment = _ops(json.loads(FS05_FRAGMENT.read_text(encoding="utf-8")))
    mounted = _mounted()
    assert len(fragment) == len(mounted) == len(ROUTE_SCOPES) >= 95
    assert sorted(set(fragment) - set(mounted)) == []
    assert sorted(set(mounted) - set(fragment)) == []
    for operation, spec in fragment.items():
        assert spec.get("x-implemented") is True, operation
        assert spec.get("x-skeleton") is not True, operation
        assert spec.get("x-feature-set") == "FS-05", operation
        assert len(spec.get("x-required-scopes") or []) == 1, operation


def test_declared_fs05_operations_are_mounted_except_documented_exceptions() -> None:
    declared = _aggregate_fs05()
    missing = {operation for operation in declared if operation not in _mounted()}
    assert missing == EXCEPTIONS
    assert len(EXCEPTIONS) == 5
    mounted_canonical = {
        operation
        for operation in declared
        if any(segment == "{id}" for segment in operation[1].split("/")) and operation not in EXCEPTIONS
    }
    assert mounted_canonical <= set(ROUTE_SCOPES)
    assert not (mounted_canonical & EXCEPTIONS)


def test_every_fs05_route_has_one_catalogued_scope_and_dependency() -> None:
    mounted = _mounted()
    assert set(mounted) == set(ROUTE_SCOPES)
    for operation, route in mounted.items():
        scope = ROUTE_SCOPES[operation]
        assert scope in CATALOGUE, (operation, scope)
        assert getattr(route, "dependencies", None), operation
    assert {ROUTE_SCOPES[operation] for operation in ROUTE_SCOPES} <= set(CATALOGUE)


async def test_representative_canonical_aliases_delegate(client: Any, seeded: dict[str, Any]) -> None:
    category = await client.post(
        "/api/v1/cost-categories",
        json={"code": f"MAT-{uuid4().hex[:6]}", "name": "Material", "category_type": "MATERIAL"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert category.status_code == 201, category.text
    category_id = category.json()["id"]

    # canonical {id} spelling of an existing concrete route
    detail = await client.get(f"/api/v1/cost-categories/{category_id}")
    assert detail.status_code == 200 and detail.json()["id"] == category_id
    patched = await client.patch(
        f"/api/v1/cost-categories/{category_id}",
        json={"name": "Material Updated"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert patched.status_code == 200, patched.text

    # canonical group aliases + genuine inactivate gap
    group = await client.post(
        "/api/v1/location-costing-groups",
        json={"code": f"GRP-{uuid4().hex[:6]}", "name": "Group", "currency_code": "USD"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    assert (await client.get(f"/api/v1/location-costing-groups/{group_id}")).status_code == 200
    member = await client.post(
        f"/api/v1/location-costing-groups/{group_id}/members",
        json={"location_id": seeded["location_id"]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert member.status_code == 200, member.text
    removed = await client.delete(
        f"/api/v1/location-costing-groups/{group_id}/members/{seeded['location_id']}",
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert removed.status_code == 200, removed.text
    inactivated = await client.post(
        f"/api/v1/location-costing-groups/{group_id}/inactivate", headers={"Idempotency-Key": uuid4().hex}
    )
    assert inactivated.status_code == 200, inactivated.text
    assert inactivated.json()["is_active"] is False

    # canonical recomputation alias (different literal path spelling)
    recalc = await client.post(
        "/api/v1/costs/recalculations", json={"items": []}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert recalc.status_code not in {404, 405}, recalc.text  # job seed may be absent → 409, but route is mounted


async def test_canonical_elc_template_patch_is_mounted(client: Any, seeded: dict[str, Any]) -> None:
    category = await client.post(
        "/api/v1/cost-categories",
        json={"code": f"ELC-{uuid4().hex[:6]}", "name": "Freight", "category_type": "FREIGHT"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert category.status_code == 201, category.text
    template = await client.post(
        "/api/v1/estimated-landed-cost-templates",
        json={
            "code": f"ELC-{uuid4().hex[:6]}",
            "name": "Freight estimate",
            "currency_code": "USD",
            "lines": [
                {
                    "cost_category_id": category.json()["id"],
                    "allocation_basis": "AMOUNT",
                    "default_percent": "10",
                }
            ],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert template.status_code == 201, template.text
    template_id = template.json()["id"]
    patched = await client.patch(
        f"/api/v1/estimated-landed-cost-templates/{template_id}",
        json={"name": "Freight estimate updated"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Freight estimate updated"
    missing = await client.patch(
        f"/api/v1/estimated-landed-cost-templates/{uuid4()}",
        json={"name": "missing"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert missing.status_code == 404, missing.text


async def test_canonical_routes_require_authentication(db_session: Any) -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def _db() -> Any:
        yield db_session

    app.dependency_overrides[get_db] = _db
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anonymous:
        response = await anonymous.get(f"/api/v1/landed-costs/{uuid4()}")
    assert response.status_code in {401, 403}


async def test_canonical_routes_are_tenant_scoped(client: Any, db_session: Any, seeded: dict[str, Any]) -> None:
    other_service = CostingService(db_session, seeded["other_tenant_id"])
    foreign = await other_service.create_category(
        {"code": f"OTH-{uuid4().hex[:6]}", "name": "Other tenant", "category_type": "MATERIAL"}
    )
    detail = await client.get(f"/api/v1/cost-categories/{foreign['id']}")
    assert detail.status_code == 404, detail.text
    canonical = await client.get(f"/api/v1/cost-categories/{foreign['id']}")
    assert canonical.status_code == 404
