#!/usr/bin/env python3
"""Derive the FS-06 builder contract artefacts from the implemented numbered-inventory router.

Outputs (inside ``app/contracts/traceability_numbered/``; deterministic, no timestamps):
* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the 14 mounted routes
  (real request/response schemas, refs inlined, exactly-one catalogued scope, ``x-implemented`` markers);
* ``events/<event>.schema.json`` — payload JSON Schemas for the five required v1 events.

The aggregate contract/registry regeneration (`python -m app.contracts.cli generate`) and the
``contracts/openapi`` merge are owned by the integration/contract task; this script only keeps the FS-06
extension point implementation-backed and deterministic.
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
ENVELOPE_REQUIRED = [
    "aggregate",
    "data",
    "event_id",
    "event_type",
    "event_version",
    "occurred_at",
    "tenant_id",
]


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
    from app.api.routers.traceability_numbered import ROUTE_SCOPES
    from app.api.routers.traceability_numbered import router as numbered_router

    app = FastAPI(title="traceability.numbered contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(numbered_router)
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
                f"fs06_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-06", "traceability.numbered"]
            resolved["x-feature-set"] = "FS-06"
            resolved["x-contract"] = ["traceability.numbered"]
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
            "title": "traceability.numbered builder fragment",
            "version": "1.0.0",
            "x-feature-set": "FS-06",
            "x-contract": "traceability.numbered",
            "x-plan-version": _plan_version(),
            "x-status": "c0-candidate",
        },
        "paths": paths,
        "x-implemented-operations": sum(len(item) for item in paths.values()),
    }


def _event_schema(event: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    data_schema = {
        "type": "object",
        "required": required,
        "additionalProperties": True,
        "properties": properties,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://inventory-platform/contracts/events/traceability.numbered/{event}.schema.json",
        "title": f"traceability.numbered/{event}",
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
                    "type": {"const": "traceability.numbered"},
                    "id": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                },
            },
            "correlation_id": {"type": ["string", "null"]},
            "causation_id": {"type": ["string", "null"]},
            "data": data_schema,
        },
        "x-event": event,
        "x-contract": "traceability.numbered",
        "x-contract-version": "1.0.0",
        "x-feature-set": "FS-06",
        "x-todo": False,
    }


def event_schemas() -> dict[str, dict[str, Any]]:
    uuid = {"type": "string", "format": "uuid"}
    return {
        "traceability.lot.created": _event_schema(
            "traceability.lot.created",
            {
                "lot_id": uuid,
                "item_id": uuid,
                "lot_number": {"type": "string"},
                "expiry_date": {"type": ["string", "null"], "format": "date"},
                "manufacture_date": {"type": ["string", "null"], "format": "date"},
                "source_doc": {"type": ["string", "null"]},
            },
            ["lot_id", "item_id", "lot_number"],
        ),
        "traceability.lot.inactivated": _event_schema(
            "traceability.lot.inactivated",
            {"lot_id": uuid, "reason": {"type": ["string", "null"]}, "actor_id": {"type": ["string", "null"]}},
            ["lot_id"],
        ),
        "traceability.serial.created": _event_schema(
            "traceability.serial.created",
            {
                "serial_id": uuid,
                "item_id": uuid,
                "serial_number": {"type": "string"},
                "location_id": {"type": ["string", "null"]},
                "inventory_transaction_id": {"type": ["string", "null"]},
            },
            ["serial_id", "item_id", "serial_number"],
        ),
        "traceability.serial.state_changed": _event_schema(
            "traceability.serial.state_changed",
            {
                "serial_id": uuid,
                "item_id": uuid,
                "from_state": {"type": "string"},
                "to_state": {"type": "string"},
                "from_location_id": {"type": ["string", "null"]},
                "to_location_id": {"type": ["string", "null"]},
                "inventory_transaction_id": {"type": ["string", "null"]},
                "event": {"type": "string"},
            },
            ["serial_id", "item_id", "to_state"],
        ),
        "traceability.expiry.warning": _event_schema(
            "traceability.expiry.warning",
            {
                "item_id": uuid,
                "lot_id": uuid,
                "expiry_date": {"type": "string", "format": "date"},
                "days_to_expiry": {"type": "integer"},
                "on_hand": {"type": "string"},
            },
            ["item_id", "lot_id", "expiry_date", "days_to_expiry"],
        ),
    }


def build_artefacts() -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    outputs[HERE / "openapi.fragment.json"] = json.dumps(build_fragment(), indent=2, sort_keys=True) + "\n"
    event_dir = HERE / "events"
    for name, schema in event_schemas().items():
        outputs[event_dir / f"{name}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale: list[str] = []
    outputs = build_artefacts()
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
