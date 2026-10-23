#!/usr/bin/env python3
"""Derive the FS-03 builder contract artefacts from the implemented traceability router.

Outputs (inside ``app/contracts/traceability/``; deterministic, no timestamps):

* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the 29 mounted routes
  (real request/response schemas, refs inlined, exactly-one catalogued scope, ``x-implemented`` markers);
* ``events/<event>.schema.json`` — payload JSON Schemas for the five required v1 events + additive
  bootstrap/zone observability events;
* ``bootstrap-state.schema.json`` / ``location.schema.json`` — canonical JSON Schemas for consumers.
"""

from __future__ import annotations

import argparse
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


def build_fragment() -> dict[str, Any]:
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.traceability import ROUTE_SCOPES
    from app.api.routers.traceability import router as traceability_router

    app = FastAPI(title="traceability contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(traceability_router)
    document = app.openapi()
    components: dict[str, Any] = document.get("components", {}).get("schemas", {})
    paths: dict[str, Any] = {}
    for path, item in sorted(document.get("paths", {}).items()):
        if not path.startswith("/api/v1/"):
            continue
        operations: dict[str, Any] = {}
        for method, operation in item.items():
            if method not in METHODS:
                continue
            full = (method.upper(), path)
            scope = ROUTE_SCOPES.get(full)
            if scope is None:
                raise SystemExit(f"route {full} has no declared contract scope")
            resolved = _resolve_ref(operation, components)
            resolved.pop("deprecated", None)
            resolved["operationId"] = (
                f"fs03_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-03", "traceability.registry"]
            resolved["x-feature-set"] = "FS-03"
            resolved["x-contract"] = ["traceability.registry"]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            resolved["x-required-scopes"] = [scope]
            if method == "post":
                resolved.setdefault("x-idempotency", "Idempotency-Key required")
            operations[method] = resolved
        if operations:
            paths[path] = operations
    return {
        "info": {
            "description": "Derived from app.openapi() for the FS-03 traceability router; implementation-backed operations merged over the spec skeleton.",
            "title": "FS-03 traceability registry — implementation-backed contract fragment",
            "version": _plan_version(),
            "x-contract": ["traceability.registry@1.0.0"],
            "x-endpoint-count": sum(len(operations) for operations in paths.values()),
            "x-feature-set": "FS-03",
            "x-generated-by": "app/contracts/traceability/build_from_app.py",
            "x-status": "b3-candidate",
        },
        "openapi": "3.1.0",
        "paths": paths,
    }


def build_artefacts() -> dict[Path, str]:
    from app.contracts.traceability.schemas import bootstrap_schema, event_schemas, location_schema

    outputs: dict[Path, str] = {}
    fragment = build_fragment()
    outputs[HERE / "openapi.fragment.json"] = json.dumps(fragment, indent=2, sort_keys=True) + "\n"
    outputs[HERE / "bootstrap-state.schema.json"] = json.dumps(bootstrap_schema(), indent=2, sort_keys=True) + "\n"
    outputs[HERE / "location.schema.json"] = json.dumps(location_schema(), indent=2, sort_keys=True) + "\n"
    events_dir = HERE / "events"
    for name, schema in event_schemas().items():
        outputs[events_dir / f"{name}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = build_artefacts()
    stale: list[str] = []
    for path, content in outputs.items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        stale.append(str(path.relative_to(HERE)))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(json.dumps({"generated": len(outputs), "stale": stale, "check": args.check}))
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
