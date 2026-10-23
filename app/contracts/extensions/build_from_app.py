#!/usr/bin/env python3
"""FS-14 extensions.registry builder contract artefacts (deterministic)."""

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
WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})
ENVELOPE_REQUIRED = ["event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"]
ENVELOPE_PROPERTIES: dict[str, Any] = {
    "event_id": {"type": "string", "format": "uuid"},
    "event_type": {"type": "string", "minLength": 3},
    "event_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
    "tenant_id": {"type": "string", "format": "uuid"},
    "occurred_at": {"type": "string", "format": "date-time"},
    "aggregate": {
        "type": "object",
        "required": ["type", "id", "version"],
        "properties": {
            "type": {"type": "string"},
            "id": {"type": "string"},
            "version": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    "correlation_id": {"type": "string"},
    "data": {"type": "object", "additionalProperties": True},
}

EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "warranty.terms.updated": ("item_id", "duration_months", "is_active"),
    "warranty.registered": (
        "registration_id",
        "item_id",
        "serial_id",
        "customer_ref",
        "warranty_start",
        "warranty_end",
        "source",
    ),
    "warranty.registration.voided": ("registration_id", "reason"),
    "warranty.registration.expired": ("registration_id", "warranty_end"),
    "warranty.claim.opened": ("claim_id", "registration_id", "claim_type"),
    "warranty.claim.validated": ("claim_id", "pass", "reasons"),
    "warranty.claim.approved": ("claim_id", "approval_ref"),
    "warranty.claim.rejected": ("claim_id", "reason"),
    "warranty.claim.action.recorded": ("claim_id", "sequence", "action_type", "status"),
    "warranty.claim.completed": ("claim_id", "status", "notes"),
    "warranty.claim.cancelled": ("claim_id", "status"),
    "warranty.repair.created": ("repair_order_id", "claim_id", "item_id", "serial_id", "location_id", "costs"),
    "warranty.repair.updated": ("repair_order_id", "status", "costs"),
    "warranty.repair.completed": ("repair_order_id", "claim_id", "costs"),
    "warranty.repair.cancelled": ("repair_order_id", "claim_id"),
    "extensions.customer_part.created": ("mapping_id", "item_id", "customer_ref", "part_number"),
    "extensions.customer_part.updated": ("mapping_id", "item_id", "customer_ref", "part_number"),
    "extensions.customer_part.deactivated": ("mapping_id", "item_id", "customer_ref", "part_number"),
    "extensions.upc.assigned": ("registry_id", "item_id", "upc_code", "assignment_source", "status"),
    "extensions.upc.retired": ("registry_id", "item_id", "upc_code", "status"),
    "extensions.upc.batch.completed": ("job_run_id", "assigned", "skipped", "failed"),
}


def _plan_version() -> str:
    return str(json.loads((REPO_ROOT / "design" / "feature-sets.json").read_text(encoding="utf-8"))["plan_version"])


def _resolve_ref(obj: Any, components: dict[str, Any], depth: int = 0) -> Any:
    if depth > 32:
        return obj
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            target = components.get(ref.split("/")[-1])
            if target is None:
                return obj
            merged = _resolve_ref(target, components, depth + 1)
            if len(obj) > 1:
                merged = {
                    **merged,
                    **{key: _resolve_ref(value, components, depth + 1) for key, value in obj.items() if key != "$ref"},
                }
            return merged
        return {key: _resolve_ref(value, components, depth + 1) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_resolve_ref(item, components, depth + 1) for item in obj]
    return obj


def build_fragment() -> dict[str, Any]:
    from fastapi import FastAPI

    from app.api.errors import install_error_handlers
    from app.api.routers.extensions import ROUTE_SCOPES, router

    app = FastAPI(title="extensions contract builder", openapi_url="/openapi.json")
    install_error_handlers(app)
    app.include_router(router)
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
                f"fs14_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-14", "extensions.registry"]
            resolved["x-feature-set"] = "FS-14"
            resolved["x-contract"] = ["extensions.registry"]
            resolved["x-implemented"] = True
            resolved["x-skeleton"] = False
            resolved["x-required-scopes"] = [scope]
            if method in WRITE_METHODS:
                parameters = [p for p in resolved.get("parameters", []) if p.get("name") != "Idempotency-Key"]
                parameters.append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    }
                )
                resolved["parameters"] = parameters
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
            "title": "FS-14 extensions.registry builder fragment",
            "version": _plan_version(),
            "description": "Implementation-backed FS-14 Extensions registry operations (generated from the router).",
            "x-feature-set": "FS-14",
            "x-contract": "extensions.registry",
            "x-implemented-operations": sum(len(item) for item in paths.values()),
        },
        "paths": paths,
    }


def event_schema(event: str) -> dict[str, Any]:
    fields = EVENT_FIELDS[event]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://contracts.inventory-platform.local/extensions.registry/{event}/1.0.0",
        "title": event,
        "description": f"extensions.registry@1.0.0 {event} payload (final outbox envelope; implemented data fields).",
        "type": "object",
        "required": list(ENVELOPE_REQUIRED),
        "properties": {
            **ENVELOPE_PROPERTIES,
            "data": {
                "type": "object",
                "required": [field for field in fields if field != "tenant_id"],
                "properties": {field: {} for field in fields},
                "additionalProperties": True,
            },
        },
        "additionalProperties": False,
        "x-event-type": event,
        "x-event-version": "1.0.0",
        "x-contract": "extensions.registry",
        "x-feature-set": "FS-14",
        "x-payload-key-fields": list(fields),
    }


def build_outputs() -> dict[Path, dict[str, Any]]:
    return {
        HERE / "openapi.fragment.json": build_fragment(),
        **{HERE / "events" / f"{event}.schema.json": event_schema(event) for event in EVENT_FIELDS},
    }


def write_all() -> list[Path]:
    written = []
    for path, payload in build_outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def check() -> int:
    for path, payload in build_outputs().items():
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != payload:
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build FS-14 extensions.registry contract artefacts")
    parser.add_argument("--check", action="store_true", help="verify the committed artefacts are current")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    written = write_all()
    print(f"wrote {len(written)} artefacts under {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
