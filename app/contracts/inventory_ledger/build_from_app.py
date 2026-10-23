#!/usr/bin/env python3
"""Derive the FS-04 `inventory.ledger@1.0.0` builder fragment from the mounted ledger router.

Output: ``app/contracts/inventory_ledger/openapi.fragment.json`` — implementation-backed OpenAPI 3.1
operations for every mounted inventory-ledger route (real request/response schemas from
``app.openapi()``, refs inlined, exactly the catalogued scopes resolved from the route dependency graph,
``x-implemented`` markers). Balance read-model routes are owned by ``app/contracts/inventory_balances``
and are intentionally excluded here. Deterministic output; ``--check`` fails when the committed fragment
is stale.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory")

METHODS = frozenset({"get", "post", "put", "patch", "delete"})
# Owned by the additive inventory.balances contract fragment (read model), not inventory.ledger.
EXCLUDED_PREFIXES = (
    "/api/v1/inventory/balances",
    "/api/v1/inventory/availability",
    "/api/v1/inventory/in-transit",
)


def _plan_version() -> str:
    manifest = json.loads((REPO_ROOT / "design" / "feature-sets.json").read_text(encoding="utf-8"))
    return str(manifest["plan_version"])


def _resolve_ref(obj: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if depth > 32:
        return obj
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.split("/")[-1]
            target = components.get(name)
            if target is None:
                return obj
            merged = _resolve_ref(target, components, depth + 1)
            if len(obj) > 1:
                extras = {
                    key: _resolve_ref(value, components, depth + 1) for key, value in obj.items() if key != "$ref"
                }
                merged = {**merged, **extras}
            return merged
        return {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_resolve_ref(item, components, depth + 1) for item in obj]
    return obj


def _scopes_from_dependency(fn: Any) -> list[str]:
    if fn is None:
        return []
    try:
        values = inspect.getclosurevars(fn).nonlocals
    except (TypeError, ValueError):
        return []
    scope = values.get("scope")
    return [scope] if isinstance(scope, str) else []


def _route_scopes(route: Any) -> list[str]:
    found: list[str] = []

    def add(scope: str) -> None:
        if scope not in found:
            found.append(scope)

    for dependency in getattr(route, "dependencies", []) or []:
        for scope in _scopes_from_dependency(getattr(dependency, "dependency", None)):
            add(scope)

    def walk(dependant: Any) -> None:
        for dependency in getattr(dependant, "dependencies", []) or []:
            for scope in _scopes_from_dependency(getattr(dependency, "call", None)):
                add(scope)
            walk(dependency)

    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        walk(dependant)
    return found


def build_fragment() -> dict[str, Any]:
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.inventory_ledger import router as ledger_router

    app = FastAPI(title="inventory ledger contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(ledger_router)
    document = app.openapi()
    components: dict[str, Any] = document.get("components", {}).get("schemas", {})
    # scope map keyed by (method, openapi path) from the runtime routes
    scope_map: dict[tuple[str, str], list[str]] = {}
    for route in ledger_router.routes:
        path = getattr(route, "path", None)
        if path:
            for method in getattr(route, "methods", set()) or set():
                if method.lower() in METHODS:
                    scope_map[(method.lower(), path)] = _route_scopes(route)
    paths: dict[str, Any] = {}
    for path, item in sorted(document.get("paths", {}).items()):
        if not path.startswith("/api/v1/") or any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        operations: dict[str, Any] = {}
        for method, operation in item.items():
            if method not in METHODS:
                continue
            scopes = scope_map.get((method, path), [])
            if not scopes:
                raise SystemExit(f"route {method.upper()} {path} has no catalogued scope dependency")
            resolved = _resolve_ref(operation, components)
            resolved.pop("deprecated", None)
            resolved["operationId"] = (
                f"fs04_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-04", "inventory.ledger"]
            resolved["x-feature-set"] = "FS-04"
            resolved["x-contract"] = ["inventory.ledger"]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            resolved["x-required-scopes"] = scopes
            if method in {"post", "patch"}:
                resolved.setdefault(
                    "x-idempotency", "Idempotency-Key required" if method == "post" else "If-Match version guard"
                )
            operations[method] = resolved
        if operations:
            paths[path] = operations
    return {
        "info": {
            "description": "Derived from app.openapi() for the mounted FS-04 inventory-ledger router; implementation-backed operations merged over the spec skeleton.",
            "title": "FS-04 inventory.ledger — implementation-backed contract fragment",
            "version": _plan_version(),
            "x-contract": "inventory.ledger",
            "x-endpoint-count": sum(len(operations) for operations in paths.values()),
            "x-feature-set": "FS-04",
            "x-generated-by": "app/contracts/inventory_ledger/build_from_app.py",
            "x-status": "b4-candidate",
        },
        "openapi": "3.1.0",
        "paths": paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(build_fragment(), indent=2, sort_keys=True) + "\n"
    path = HERE / "openapi.fragment.json"
    stale = not path.exists() or path.read_text(encoding="utf-8") != content
    if stale and not args.check:
        path.write_text(content, encoding="utf-8")
    print(json.dumps({"path": str(path.relative_to(HERE)), "stale": stale, "check": args.check}))
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
