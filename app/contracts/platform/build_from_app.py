#!/usr/bin/env python3
"""Derive the FS-00 builder extension fragment from the implemented FastAPI app (C0 freeze).

Source of truth for *implemented* operations is ``app.openapi()`` (the mounted routers), not the
spec skeleton. This script converts every mounted OpenAPI operation into a builder-fragment operation:

* real request/response schemas from ``app.openapi()`` (component ``$ref``s are inlined so the
  aggregate needs no extra components);
* ``x-implemented: true`` / ``x-skeleton: false`` (the generator lets these override the spec
  skeleton for the same path+method and adds genuinely new operations);
* the exactly-one capability scope resolved from the route dependency graph (``require_scope``),
  or an explicit ``x-allowlist`` for catalogued public/self-service routes;
* non-business operational endpoints (``/healthz``, ``/readyz``) are included with their public
  allowlist marker; FastAPI docs/openapi routes are not part of the contract (see evidence).

The output is deterministic (sorted keys, no timestamps). ``--check`` exits non-zero when the
committed fragment is stale. It only writes inside ``app/contracts/platform/`` /
``app/contracts/order_boundary/`` (both FS-00 contract directories).
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory")

METHODS = frozenset({"get", "post", "put", "patch", "delete"})
OPERATIONAL_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})
# Contract ownership of FS-00 REST surfaces: finance intents/read APIs map to platform.finance,
# everything else to platform.core (platform.events is the event contract, no REST surface).
FINANCE_PATH_HINTS = ("finance", "journal-intent", "fiscal-period", "exchange-rate")


def _plan_version() -> str:
    manifest = json.loads((REPO_ROOT / "design" / "feature-sets.json").read_text(encoding="utf-8"))
    return str(manifest["plan_version"])


def _leaf_routes(routes: Iterable[Any]) -> list[Any]:
    """Flatten FastAPI's included-router wrappers (``_IncludedRouter``) into APIRoutes."""
    out: list[Any] = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            out.extend(_leaf_routes(route.original_router.routes))
        elif hasattr(route, "path") and getattr(route, "methods", None):
            out.append(route)
    return out


def _route_scopes(route: Any) -> list[str]:
    found: list[str] = []

    def inspect_dep(fn: Any) -> None:
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
        inspect_dep(getattr(dependency, "dependency", None))

    def walk(dependant: Any) -> None:
        for dependency in getattr(dependant, "dependencies", []) or []:
            inspect_dep(getattr(dependency, "call", None))
            walk(dependency)

    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        walk(dependant)
    return found


def _resolve_ref(obj: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if depth > 24:
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
                extra = {k: _resolve_ref(v, components, depth + 1) for k, v in obj.items() if k != "$ref"}
                merged = {**merged, **extra}
            return merged
        return {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_resolve_ref(item, components, depth + 1) for item in obj]
    return obj


def _contracts_for_path(path: str) -> list[str]:
    if any(hint in path for hint in FINANCE_PATH_HINTS):
        return ["platform.finance", "platform.core"]
    return ["platform.core"]


def build_platform_fragment() -> dict[str, Any]:
    from app.api.main import app
    from app.core.security.scopes import ScopeRegistry

    spec = app.openapi()
    components = (spec.get("components") or {}).get("schemas") or {}
    registry = ScopeRegistry()
    # Keep the pre-C0 spec operationIds for operations the spec already declared (stable contract IDs);
    # genuinely new implemented operations use FastAPI's generated operationId.
    from app.contracts.parse_specs import build_model

    spec_operation_ids = {
        (endpoint.method.lower(), endpoint.path): endpoint.operation_id
        for feature_set in build_model().feature_sets
        if feature_set.id == "FS-00"
        for endpoint in feature_set.endpoints
    }
    routes = _leaf_routes(app.routes)
    route_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for route in routes:
        for method in sorted(route.methods or ()):
            if method.lower() not in METHODS:
                continue
            scopes = _route_scopes(route)
            path = route.path
            route_meta[(path, method.lower())] = {
                "scopes": scopes,
                "public": registry.is_public_route(method, path),
                "self_service": registry.is_self_service_route(method, path),
            }

    paths: dict[str, Any] = {}
    missing_meta: list[str] = []
    skipped_unscoped: list[str] = []
    for raw_path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            meta = route_meta.get((raw_path, method))
            if meta is None:
                missing_meta.append(f"{method.upper()} {raw_path}")
                continue
            resolved = _resolve_ref(operation, components)
            new_operation: dict[str, Any] = {
                "tags": list(operation.get("tags") or []),
                "summary": operation.get("summary") or f"{method.upper()} {raw_path}",
                "operationId": spec_operation_ids.get((method, raw_path)) or operation.get("operationId"),
                "x-feature-set": "FS-00",
                "x-contracts": _contracts_for_path(raw_path),
                "x-implemented": True,
                "x-skeleton": False,
                "x-source": "app.openapi()",
                "x-route-class": "public" if meta["public"] else ("self-service" if meta["self_service"] else "scoped"),
                "responses": resolved.get("responses") or {"200": {"description": "OK"}},
            }
            if operation.get("description"):
                new_operation["description"] = operation["description"]
            parameters = [
                parameter for parameter in (resolved.get("parameters") or [])
                if not (
                    str(parameter.get("in", "")).lower() == "header"
                    and str(parameter.get("name", "")).lower() == "authorization"
                )
            ]
            if parameters:
                new_operation["parameters"] = parameters
            if operation.get("requestBody"):
                new_operation["requestBody"] = resolved.get("requestBody")
            if operation.get("security"):
                new_operation["security"] = operation["security"]
            if operation.get("deprecated"):
                new_operation["deprecated"] = True
            if meta["scopes"]:
                new_operation["x-required-scopes"] = meta["scopes"]
            if meta["public"]:
                new_operation["x-allowlist"] = "public"
            elif meta["self_service"]:
                new_operation["x-allowlist"] = "self-service"
            if raw_path in OPERATIONAL_EXEMPT_PATHS:
                new_operation["x-operational"] = True
            if not (meta["scopes"] or meta["public"] or meta["self_service"]):
                # No catalogue scope and not on an allowlist: an app route (out of contract until the
                # owner adds a scope/allowlist entry or the route is removed). Excluded here so the
                # committed contract stays lint-clean; listed in evidence/C0 as a residual gap.
                skipped_unscoped.append(f"{method.upper()} {raw_path}")
                continue
            paths.setdefault(raw_path, {})[method] = new_operation

    fragment = {
        "openapi": "3.1.0",
        "info": {
            "title": "FS-00 platform — implementation-backed contract fragment",
            "version": _plan_version(),
            "description": "Derived from app.openapi() (mounted FS-00 routers). Operations are implementation-backed; the aggregate merges them over the spec skeleton for the same path+method and appends new implemented operations.",
            "x-feature-set": "FS-00",
            "x-status": "c0-candidate",
            "x-source": "app.openapi()",
            "x-generated-by": "app/contracts/platform/build_from_app.py",
        },
        "paths": dict(sorted(paths.items())),
        "x-implemented-operations": sum(len(item) for item in paths.values()),
        "x-unresolved-openapi-paths": sorted(missing_meta),
        "x-skipped-unscoped-routes": sorted(skipped_unscoped),
    }
    return fragment


def build_order_boundary_fragment() -> dict[str, Any]:
    """order.demand/order.supply are FS-00-provided *stub* contracts (version 0.1.0).

    No order-boundary routers are implemented yet; keep the honest placeholder so the generator's
    spec skeletons remain the only declarations (C0 residual: order.demand/supply pending FS owner).
    """
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "FS-00 order boundary — stub contract fragment (order.demand/order.supply 0.1.0)",
            "version": _plan_version(),
            "description": "TODO(C0/residual): no order-boundary routers are implemented yet; spec skeletons remain authoritative. Add implementation-backed paths here when order.demand/order.supply are implemented.",
            "x-feature-set": "FS-00",
            "x-status": "pending-implementation",
            "x-contracts": ["order.demand@0.1.0", "order.supply@0.1.0"],
            "x-generated-by": "app/contracts/platform/build_from_app.py",
        },
        "paths": {},
    }


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build FS-00 builder fragments from app.openapi()")
    parser.add_argument("--check", action="store_true", help="fail if committed fragments are stale")
    args = parser.parse_args(argv)
    outputs = {
        REPO_ROOT / "app/contracts/platform/openapi.fragment.json": _dump(build_platform_fragment()),
        REPO_ROOT / "app/contracts/order_boundary/openapi.fragment.json": _dump(build_order_boundary_fragment()),
    }
    stale: list[str] = []
    for path, text in outputs.items():
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                stale.append(str(path.relative_to(REPO_ROOT)))
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print("stale builder fragments (run app/contracts/platform/build_from_app.py):")
        for path in stale:
            print(f"  {path}")
        return 1
    if not args.check:
        print(f"wrote {len(outputs)} builder fragments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
