"""Deny-by-default route-scope coverage for every mounted non-public route (DM-RBAC-01 / INV-31).

Canonical requirement (Δ-RBAC addendum §4, FS-00 exit gate): **every mounted non-public route must
declare exactly one catalogued capability scope, or be on an explicit catalogue allowlist**
(public/self-service/framework). This suite is strict — it fails with the exact offending paths rather
than silently excluding them:

* un-scoped non-public routes (no scope, not allowlisted) → fail with the path list;
* routes declaring more than one scope → fail;
* routes declaring an unknown scope (not in ``design/permissions.json``) → fail;
* duplicate method+path mounts → fail;
* allowlisted routes must not also declare a scope (classification drift).

Framework endpoints (OpenAPI/docs/liveness/readiness/internal probe) are the only non-business
exceptions and are listed explicitly here.
"""

from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = pytest.mark.contract

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

#: FastAPI's own operational endpoints — mounted, but not part of the business contract.
FRAMEWORK_ROUTE_PATHS = frozenset({"/api/v1/openapi.json", "/api/v1/docs", "/docs/oauth2-redirect", "/internal/deps"})


@dataclass(frozen=True, slots=True)
class RouteRow:
    method: str
    path: str
    scopes: tuple[str, ...]
    public: bool
    self_service: bool
    in_schema: bool

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def framework(self) -> bool:
        return self.path in FRAMEWORK_ROUTE_PATHS

    @property
    def unresolved(self) -> bool:
        return not self.scopes and not self.public and not self.self_service and not self.framework


def _load_app() -> Any:
    os.environ.setdefault("ENV", "test")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory"
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.api.main")
    return main.create_app()


def _iter_leaf_routes(routes: Iterable[Any]) -> list[Any]:
    """FastAPI wraps ``include_router`` results in ``_IncludedRouter`` objects; resolve them."""
    out: list[Any] = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            out.extend(_iter_leaf_routes(route.original_router.routes))
        elif hasattr(route, "path") and getattr(route, "methods", None):
            out.append(route)
    return out


def _route_scopes(route: Any) -> tuple[str, ...]:
    """Collect capability scopes declared by route/dependant dependencies (closure ``scope`` values)."""
    found: list[str] = []

    def add(fn: Any) -> None:
        if fn is None:
            return
        try:
            values = inspect.getclosurevars(fn).nonlocals
        except (TypeError, ValueError):
            return
        scope = values.get("scope")
        if isinstance(scope, str) and scope not in found:
            found.append(scope)

    for dependency in getattr(route, "dependencies", []) or []:
        add(getattr(dependency, "dependency", None))

    def walk(dependant: Any) -> None:
        for dependency in getattr(dependant, "dependencies", []) or []:
            add(getattr(dependency, "call", None))
            walk(dependency)

    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        walk(dependant)
    return tuple(found)


def _route_matrix() -> list[RouteRow]:
    app = _load_app()
    from app.core.security.scopes import ScopeRegistry

    registry = ScopeRegistry()
    rows: list[RouteRow] = []
    for route in _iter_leaf_routes(app.routes):
        for method in sorted(route.methods or ()):
            if method not in HTTP_METHODS:
                continue
            rows.append(
                RouteRow(
                    method=method,
                    path=route.path,
                    scopes=_route_scopes(route),
                    public=registry.is_public_route(method, route.path),
                    self_service=registry.is_self_service_route(method, route.path),
                    in_schema=bool(getattr(route, "include_in_schema", True)),
                )
            )
    return rows


def test_every_non_public_route_has_exactly_one_catalogued_scope() -> None:
    rows = _route_matrix()
    from app.core.security.scopes import ScopeRegistry

    registry = ScopeRegistry()
    catalogue = {scope.scope for scope in registry.catalogue.scopes}

    duplicates = sorted({row.key for row in rows if sum(1 for other in rows if other.key == row.key) > 1})
    multi_scope = sorted(row.key for row in rows if len(row.scopes) > 1)
    unknown_scope = sorted({f"{row.key} -> {scope}" for row in rows for scope in row.scopes if scope not in catalogue})
    missing_scope = sorted(row.key for row in rows if row.unresolved)
    scoped_without_scope = sorted(
        row.key for row in rows if not row.framework and not row.public and not row.self_service and not row.scopes
    )

    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate mounted operations: {duplicates}")
    if multi_scope:
        problems.append(f"routes declaring more than one scope: {multi_scope}")
    if unknown_scope:
        problems.append(f"routes declaring scopes outside the catalogue: {unknown_scope}")
    if missing_scope:
        problems.append(
            f"unscoped non-public routes (owner must add a catalogue scope or explicit allowlist): {missing_scope}"
        )
    if scoped_without_scope:
        problems.append(f"scoped-but-empty routes: {scoped_without_scope}")
    assert problems == [], "\n".join(problems)


def test_allowlisted_routes_are_classified_and_carry_no_scope() -> None:
    rows = _route_matrix()
    from app.core.security.scopes import ScopeRegistry

    registry = ScopeRegistry()
    problems: list[str] = []
    for row in rows:
        if row.framework:
            continue
        if row.public and not registry.is_public_route(row.method, row.path):
            problems.append(f"{row.key}: public classification drift")
        if row.self_service and not registry.is_self_service_route(row.method, row.path):
            problems.append(f"{row.key}: self-service classification drift")
        if (row.public or row.self_service) and row.scopes:
            problems.append(f"{row.key}: allowlisted route also declares scope(s) {row.scopes}")
        if not (row.public or row.self_service) and row.scopes and len(row.scopes) != 1:
            problems.append(f"{row.key}: scoped route must declare exactly one scope, got {row.scopes}")
    assert problems == [], problems


def test_coverage_report_is_complete() -> None:
    rows = _route_matrix()
    business = [row for row in rows if not row.framework]
    scoped = [row for row in business if not row.public and not row.self_service]
    report = {
        "mounted_operations": len(rows),
        "business_operations": len(business),
        "scoped": len(scoped),
        "public": sum(1 for row in business if row.public),
        "self_service": sum(1 for row in business if row.self_service),
        "framework": sum(1 for row in rows if row.framework),
    }
    print(f"route-scope coverage: {report}")
    assert report["mounted_operations"] >= 100
    assert report["scoped"] >= 90
    assert report["scoped"] + report["public"] + report["self_service"] == report["business_operations"]
    assert all(len(row.scopes) == 1 for row in scoped), report


def test_public_allowlist_contains_the_documented_auth_surface() -> None:
    from app.core.security.scopes import ScopeRegistry

    registry = ScopeRegistry()
    entries = set(registry.catalogue.public_allowlist)
    assert {
        "/healthz",
        "/readyz",
        "POST /api/v1/auth/login",
        "POST /api/v1/auth/select-tenant",
        "GET /api/v1/auth/providers",
        "POST /api/v1/auth/sso/start",
        "GET /api/v1/auth/sso/callback",
    } <= entries
    self_service = set(registry.catalogue.self_service_allowlist)
    assert {"GET /api/v1/me", "GET /api/v1/me/tenants"} <= self_service
