"""Shared helpers for FS-00 platform contract tests (C0 freeze candidate).

The application is imported lazily with test-safe env defaults so these tests are pure/static (no DB
required). Route metadata is resolved from the mounted FastAPI routers: FastAPI wraps ``include_router``
results in ``_IncludedRouter`` objects, so traversal resolves ``original_router.routes`` recursively.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory"
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# FastAPI's own operational endpoints: mounted but not part of the business contract.
FRAMEWORK_ROUTE_PATHS = frozenset(
    {"/api/v1/openapi.json", "/api/v1/docs", "/docs/oauth2-redirect", "/internal/deps", "/api/v1/mcp"}
)

# Routes currently mounted without a catalogue scope/allowlist. These are reported (not edited) and
# block strict C0 route-scope coverage until the owner fixes the router or design/permissions.json.
KNOWN_UNSCOPED_ROUTES: dict[str, dict[str, str]] = {}


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
        return not self.scopes and not self.public and not self.self_service


def load_app() -> Any:
    os.environ.setdefault("ENV", "test")
    os.environ.setdefault("DATABASE_URL", DEFAULT_DATABASE_URL)
    from app.core.config import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.api.main")
    return main.app


def iter_leaf_routes(routes: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            out.extend(iter_leaf_routes(route.original_router.routes))
        elif hasattr(route, "path") and getattr(route, "methods", None):
            out.append(route)
    return out


def route_scopes(route: Any) -> tuple[str, ...]:
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


def scope_registry() -> Any:
    from app.core.security.scopes import ScopeRegistry

    return ScopeRegistry()


def route_matrix() -> list[RouteRow]:
    app = load_app()
    registry = scope_registry()
    rows: list[RouteRow] = []
    for route in iter_leaf_routes(app.routes):
        for method in sorted(route.methods or ()):
            if method not in HTTP_METHODS:
                continue
            rows.append(
                RouteRow(
                    method=method,
                    path=route.path,
                    scopes=route_scopes(route),
                    public=registry.is_public_route(method, route.path),
                    self_service=registry.is_self_service_route(method, route.path),
                    in_schema=bool(getattr(route, "include_in_schema", True)),
                )
            )
    return rows


def load_json(relpath: str) -> Any:
    return json.loads((REPO_ROOT / relpath).read_text(encoding="utf-8"))


def platform_fragment() -> dict[str, Any]:
    return load_json("app/contracts/platform/openapi.fragment.json")


def fragment_operations(fragment: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): operation
        for path, item in (fragment.get("paths") or {}).items()
        for method, operation in item.items()
        if isinstance(operation, dict)
    }


def aggregate_document() -> dict[str, Any]:
    return load_json("contracts/openapi/openapi.json")


def catalogue_scopes() -> set[str]:
    registry = scope_registry()
    return {scope.scope for scope in registry.catalogue.scopes}


def fs00_contract_names() -> list[str]:
    manifest = load_json("design/feature-sets.json")
    for feature_set in manifest["feature_sets"]:
        if feature_set["id"] == "FS-00":
            return [provided["contract"] for provided in feature_set["provides"]]
    raise AssertionError("FS-00 missing from manifest")


def fs00_event_registry_entries() -> list[dict[str, Any]]:
    registry = load_json("contracts/events/registry.json")
    names = set(fs00_contract_names())
    return [entry for entry in registry["contracts"] if entry["contract"] in names]
