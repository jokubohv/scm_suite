#!/usr/bin/env python3
"""Derive the FS-12 builder contract artefacts from the implemented quality router.

Outputs (inside ``app/contracts/quality/``; deterministic, no timestamps):
* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the mounted quality routes
  (real schemas, exactly-one catalogued scope, ``x-implemented`` markers);
* ``events/<event>.schema.json`` — payload JSON Schemas for the twenty required v1 events.

Aggregate/registry regeneration is owned by the integration contract task.
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
ENVELOPE_REQUIRED = ["aggregate", "data", "event_id", "event_type", "event_version", "occurred_at", "tenant_id"]
EVENTS = (
    "quality.spec.created",
    "quality.spec.versioned",
    "quality.spec.retired",
    "quality.plan.created",
    "quality.plan.changed",
    "quality.queue.changed",
    "quality.trigger.changed",
    "quality.trigger.counter_reset",
    "quality.inspection.created",
    "quality.inspection.completed",
    "quality.inspection.passed",
    "quality.inspection.failed",
    "quality.inspection.waived",
    "quality.inspection.cancelled",
    "quality.hold.applied",
    "quality.hold.released",
    "quality.coa.created",
    "quality.coa.issued",
    "quality.coa.revoked",
    "quality.derived_field.evaluated",
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
            resolved = _resolve_ref(target, components, depth + 1)
            if len(obj) > 1:
                extras = {
                    key: _resolve_ref(value, components, depth + 1) for key, value in obj.items() if key != "$ref"
                }
                resolved = {**resolved, **extras}
            return resolved
        return {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_resolve_ref(item, components, depth + 1) for item in obj]
    return obj


def build_fragment() -> dict[str, Any]:
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.quality import ROUTE_SCOPES
    from app.api.routers.quality import router as quality_router

    app = FastAPI(title="quality.management contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(quality_router)
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
                f"fs12_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-12", "quality.management"]
            resolved["x-feature-set"] = "FS-12"
            resolved["x-contract"] = ["quality.management"]
            resolved["x-contract-version"] = "1.0.0"
            resolved["x-required-scopes"] = [scope]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            operations[method] = resolved
        if operations:
            paths[path] = operations
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "quality.management builder fragment",
            "version": "1.0.0",
            "x-feature-set": "FS-12",
            "x-contract": "quality.management",
            "x-plan-version": _plan_version(),
            "x-status": "c0-candidate",
        },
        "paths": paths,
        "x-implemented-operations": sum(len(item) for item in paths.values()),
    }


def _event_schema(event: str) -> dict[str, Any]:
    uuid = {"type": "string", "format": "uuid"}
    payload_props: dict[str, Any] = {
        "inspection_id": uuid,
        "spec_id": uuid,
        "plan_id": uuid,
        "trigger_id": uuid,
        "coa_id": uuid,
        "item_id": uuid,
        "disposition": {"type": ["string", "null"]},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://inventory-platform/contracts/events/quality.management/{event}.schema.json",
        "title": f"quality.management/{event}",
        "type": "object",
        "required": list(ENVELOPE_REQUIRED),
        "properties": {
            "event_id": {"type": "string", "format": "uuid"},
            "event_type": {"const": event},
            "event_version": {"const": "1.0.0"},
            "tenant_id": {"type": "string", "format": "uuid"},
            "occurred_at": {"type": "string", "format": "date-time"},
            "aggregate": {
                "type": "object",
                "required": ["type", "id", "version"],
                "properties": {
                    "type": {"const": "quality"},
                    "id": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                },
            },
            "correlation_id": {"type": ["string", "null"]},
            "causation_id": {"type": ["string", "null"]},
            "data": {"type": "object", "additionalProperties": True, "properties": payload_props},
        },
        "x-event": event,
        "x-contract": "quality.management",
        "x-contract-version": "1.0.0",
        "x-feature-set": "FS-12",
        "x-todo": False,
    }


def build_artefacts() -> dict[Path, str]:
    outputs: dict[Path, str] = {
        HERE / "openapi.fragment.json": json.dumps(build_fragment(), indent=2, sort_keys=True) + "\n"
    }
    events_dir = HERE / "events"
    for event in EVENTS:
        outputs[events_dir / f"{event}.schema.json"] = json.dumps(_event_schema(event), indent=2, sort_keys=True) + "\n"
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale: list[str] = []
    for path, content in build_artefacts().items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        stale.append(str(path.relative_to(HERE)))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(json.dumps({"generated": len(EVENTS) + 1, "stale": stale, "check": args.check}))
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
