#!/usr/bin/env python3
"""Derive the FS-02 builder contract artefacts from the implemented pricing router.

Outputs (all inside ``app/contracts/pricing/``; deterministic, no timestamps):

* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the mounted pricing
  router (real request/response schemas from ``app.openapi()``, refs inlined, exactly-one scope,
  ``x-implemented`` markers) consumed by ``python -m app.contracts.cli generate``;
* ``priced-quote.schema.json`` / ``price-resolution.schema.json`` — canonical JSON Schemas for the
  external order-boundary consumer (D3/Δ-2);
* ``events/<event>.schema.json`` — payload JSON Schemas for the six ``pricing.*`` events.
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
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory")

METHODS = frozenset({"get", "post", "put", "patch", "delete"})
PLACEHOLDER_DESCRIPTIONS = {"", "Successful Response", "Validation Error"}


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
    from app.api.routers.pricing import ROUTE_SCOPES
    from app.api.routers.pricing import router as pricing_router

    app = FastAPI(title="pricing contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(pricing_router)
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
                f"fs02_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-02", "pricing.engine"]
            resolved["x-feature-set"] = "FS-02"
            resolved["x-contract"] = ["pricing.engine"]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            resolved["x-required-scopes"] = [scope]
            if full in {("POST", "/api/v1/price-levels"), ("POST", "/api/v1/item-prices")}:
                resolved.setdefault("x-idempotency", "Idempotency-Key required")
            operations[method] = resolved
        if operations:
            paths[path] = operations
    return {
        "info": {
            "description": "Derived from app.openapi() for the FS-02 pricing router; implementation-backed operations merged over the spec skeleton.",
            "title": "FS-02 pricing — implementation-backed contract fragment",
            "version": _plan_version(),
            "x-contract": ["pricing.engine@1.0.0"],
            "x-endpoint-count": sum(len(operations) for operations in paths.values()),
            "x-feature-set": "FS-02",
            "x-generated-by": "app/contracts/pricing/build_from_app.py",
            "x-status": "b2-candidate",
        },
        "openapi": "3.1.0",
        "paths": paths,
    }


def build_artefacts() -> dict[Path, str]:
    from app.contracts.pricing.schemas import event_schemas, price_resolution_schema, priced_quote_schema

    outputs: dict[Path, str] = {}
    fragment = build_fragment()
    outputs[HERE / "openapi.fragment.json"] = json.dumps(fragment, indent=2, sort_keys=True) + "\n"
    outputs[HERE / "priced-quote.schema.json"] = json.dumps(priced_quote_schema(), indent=2, sort_keys=True) + "\n"
    outputs[HERE / "price-resolution.schema.json"] = (
        json.dumps(price_resolution_schema(), indent=2, sort_keys=True) + "\n"
    )
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
