"""FS-06 residual: every contract-declared numbered operation is mounted (canonical `{id}` aliases)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.routers.traceability_numbered import ROUTE_SCOPES, router

pytestmark = pytest.mark.integration

FRAGMENT = Path(__file__).resolve().parents[4] / "contracts" / "traceability_numbered" / "openapi.fragment.json"


def _declared() -> set[tuple[str, str]]:
    fragment = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, item in fragment["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_declared_numbered_operations_all_mounted() -> None:
    declared = _declared()
    assert declared == set(ROUTE_SCOPES)
    assert len(declared) == len(ROUTE_SCOPES) == 22
    assert len(router.routes) == 22


def test_canonical_id_aliases_and_serial_list_present() -> None:
    canonical = {operation for operation in ROUTE_SCOPES if "/{id}" in operation[1]}
    assert len(canonical) == 7
    assert ("GET", "/api/v1/serials") in ROUTE_SCOPES
    assert {("GET", "/api/v1/lots/{id}"), ("GET", "/api/v1/serials/{id}/history")} <= canonical
