"""FS-15 provider contract: the implemented reporting router matches the declared fragment and scopes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from app.api.routers.reporting.router import ROUTE_SCOPES, router

ROOT = Path(__file__).resolve().parents[3]


def test_provider_openapi_matches_fragment_paths() -> None:
    app = FastAPI()
    app.include_router(router)
    implemented = set(app.openapi()["paths"])
    fragment = json.loads((ROOT / "app/contracts/reporting/openapi.fragment.json").read_text())["paths"]
    assert implemented == set(fragment)


def test_route_scopes_cover_every_get_operation() -> None:
    app = FastAPI()
    app.include_router(router)
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            assert (method.upper(), path) in ROUTE_SCOPES, (method, path)


def test_contract_events_and_facade_importable() -> None:
    from app.contracts.reporting import contract
    assert contract.CONTRACT == "reporting.readmodels"
    assert hasattr(contract, "query_view")
    assert hasattr(contract, "get_freshness")
    assert hasattr(contract, "export_view")
