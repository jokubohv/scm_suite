"""Static contract checks for the boundary router (scopes, route inventory, no duplicate paths)."""

from __future__ import annotations

import json
from pathlib import Path

from app.api.routers.platform import boundary as boundary_router

CATALOGUE = Path(__file__).resolve().parents[4] / "design" / "permissions.json"


def _route_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for route in boundary_router.router.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.add((method, path))
    return pairs


def test_route_scope_map_matches_router_routes() -> None:
    assert _route_pairs() == set(boundary_router.ROUTE_SCOPES)


def test_every_declared_scope_is_catalogued() -> None:
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    scopes = {row["scope"] for row in catalogue["catalogue"]}
    unknown = {scope for scope in boundary_router.ROUTE_SCOPES.values() if scope not in scopes}
    assert unknown == set()


def test_expected_endpoint_inventory() -> None:
    expected = {
        ("GET", "/api/v1/order/demand/open"),
        ("PUT", "/api/v1/internal/order-boundary/demand-doubles"),
        ("GET", "/api/v1/finance/period-status"),
        ("GET", "/api/v1/reference/exchange-rates"),
        ("PUT", "/api/v1/internal/finance/exchange-rates"),
        ("POST", "/api/v1/internal/outbox/replay"),
        ("POST", "/api/v1/admin/jobs/{name}/run"),
        ("GET", "/api/v1/admin/job-runs"),
        ("GET", "/api/v1/admin/job-runs/{run_id}"),
        ("POST", "/api/v1/admin/job-runs/{run_id}/cancel"),
    }
    assert expected == _route_pairs()


def test_no_duplicate_paths_within_router() -> None:
    seen: set[tuple[str, str]] = set()
    for route in boundary_router.router.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            assert (method, path) not in seen, f"duplicate route {method} {path}"
            seen.add((method, path))
