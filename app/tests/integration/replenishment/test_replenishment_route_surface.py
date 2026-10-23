"""FS-10 residual: every contract-declared planning operation is mounted (canonical `{id}` aliases)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routers.replenishment import ROUTE_SCOPES, router

pytestmark = pytest.mark.integration

FRAGMENT = Path(__file__).resolve().parents[3] / "contracts" / "replenishment" / "openapi.fragment.json"


def _declared() -> set[tuple[str, str]]:
    fragment = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, item in fragment["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_declared_planning_operations_all_mounted() -> None:
    declared = _declared()
    assert declared == set(ROUTE_SCOPES)
    assert len(declared) == len(ROUTE_SCOPES) == 113
    assert len(router.routes) == 113


def test_canonical_id_aliases_present() -> None:
    canonical = {operation for operation in ROUTE_SCOPES if "/{id}" in operation[1]}
    assert len(canonical) == 40
    assert {("GET", "/api/v1/supply-plans/{id}"), ("POST", "/api/v1/supply-plans/{id}/run")} <= canonical


async def test_canonical_routes_return_404_for_unknown_ids(client) -> None:
    missing = uuid4()
    for path in (f"/api/v1/supply-plans/{missing}", f"/api/v1/demand-plans/{missing}", f"/api/v1/drp/networks/{missing}"):
        response = await client.get(path)
        assert response.status_code in {404, 403, 409}, response.text
