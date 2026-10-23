#!/usr/bin/env python3
"""Derive the FS-10 builder contract artefacts from the implemented replenishment router and schema models.

Outputs (all inside ``app/contracts/replenishment/``; deterministic, no timestamps):

* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the mounted replenishment
  router (real request/response schemas, refs inlined, exactly-one scope, ``x-implemented`` markers)
  consumed by ``python -m app.contracts.cli generate``;
* ``count-ref.schema.json`` / ``count-ref.schema.json`` / ``count-ref.schema.json`` — the
  in-process `replenishment.planning@1.0.0` payload schemas for FS-04/FS-09/FS-10;
* ``events/<event>.schema.json`` — payload JSON Schemas for the 15 ``replenishment.planning`` events.
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
                extras = {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items() if key != "$ref"}
                merged = {**merged, **extras}
            return merged
        return {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_resolve_ref(item, components, depth + 1) for item in obj]
    return obj


def build_fragment() -> dict[str, Any]:
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.replenishment import ROUTE_SCOPES
    from app.api.routers.replenishment import router as replenishment_router

    app = FastAPI(title="replenishment contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(replenishment_router)
    document = app.openapi()
    components: dict[str, Any] = document.get("components", {}).get("schemas", {})
    paths: dict[str, Any] = {}
    for path, item in sorted(document.get("paths", {}).items()):
        if not path.startswith("/api/v1/"):
            continue
        operations: dict[str, Any] = {}
        for method, operation in sorted(item.items()):
            if method not in METHODS:
                continue
            full = (method.upper(), path)
            scope = ROUTE_SCOPES.get(full)
            if scope is None:
                raise SystemExit(f"route {full} has no declared contract scope")
            resolved = _resolve_ref(operation, components)
            resolved.pop("deprecated", None)
            resolved["operationId"] = (
                f"fs05_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-10", "replenishment.planning"]
            resolved["x-feature-set"] = "FS-10"
            resolved["x-contract"] = ["replenishment.planning"]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            resolved["x-required-scopes"] = [scope]
            if method in {"post", "patch", "put", "delete"}:
                resolved.setdefault("parameters", []).append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    }
                )
            resolved["responses"].setdefault(
                "422",
                {
                    "description": "Validation / policy error (RFC 9457 problem+json)",
                    "content": {"application/problem+json": {"schema": {"type": "object"}}},
                },
            )
            operations[method] = resolved
        if operations:
            paths[path] = operations
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "FS-10 replenishment.planning builder fragment",
            "version": _plan_version(),
            "description": "Implementation-backed FS-10 replenishment & valuation operations (generated from the router).",
        },
        "paths": paths,
    }


def build_payload_schemas() -> dict[Path, dict[str, Any]]:
    from app.contracts.replenishment import schemas

    return {
        HERE / "planning-schemas.json": {"planning": schemas.planning_plan_schemas()},
    }


def build_event_schemas() -> dict[Path, dict[str, Any]]:
    from app.contracts.replenishment import schemas

    return {
        HERE / "events" / f"{event}.schema.json": schema
        for event, schema in schemas.all_event_payload_schemas().items()
    }


def write_all() -> list[Path]:
    written: list[Path] = []
    fragment = build_fragment()
    (HERE / "openapi.fragment.json").write_text(
        json.dumps(fragment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    written.append(HERE / "openapi.fragment.json")
    for path, payload in {**build_payload_schemas(), **build_event_schemas()}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def check() -> int:
    expected = build_fragment()
    actual = json.loads((HERE / "openapi.fragment.json").read_text(encoding="utf-8"))
    if actual != expected:
        return 2
    for path, payload in {**build_payload_schemas(), **build_event_schemas()}.items():
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != payload:
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the FS-10 replenishment contract artefacts")
    parser.add_argument("--check", action="store_true", help="verify the committed artefacts are current")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    written = write_all()
    print(f"wrote {len(written)} artefacts under {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
