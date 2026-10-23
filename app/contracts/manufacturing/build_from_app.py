#!/usr/bin/env python3
"""Derive the FS-09 ``manufacturing.execution@1.1.0`` builder contract artefacts from the implemented router.

Outputs (deterministic, no timestamps, all inside ``app/contracts/manufacturing/``):

* ``openapi.fragment.json`` — implementation-backed OpenAPI 3.1 operations for the mounted manufacturing
  router (real request/response schemas, refs inlined, exactly one catalogued scope, idempotency header
  on write operations, ``x-implemented`` markers) consumed by ``python -m app.contracts.cli generate``;
* ``events/<event>.schema.json`` — payload JSON Schemas for the 24 ``manufacturing.execution`` events with the
  final outbox envelope and the documented key fields.
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
    "causation_id": {"type": "string"},
    "data": {"type": "object", "additionalProperties": True},
}

# Payload data key fields per feature-sets/FS-09.md §7.
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "work_order.created": ("tenant_id", "work_order_id", "assembly_item_id", "qty", "wo_type", "lines"),
    "work_order.firmed": ("tenant_id", "work_order_id", "firm_planned_at"),
    "work_order.released": ("tenant_id", "work_order_id", "wip_location_id", "components"),
    "work_order.built": ("tenant_id", "work_order_id", "qty_built", "build_ref"),
    "work_order.closed": ("tenant_id", "work_order_id", "qty_built", "qty_scrapped", "variance_refs"),
    "work_order.voided": ("tenant_id", "work_order_id", "reason", "released_commitments"),
    "work_order.reopened": ("tenant_id", "work_order_id", "reason"),
    "work_order.rescheduled": ("tenant_id", "work_order_id", "operations"),
    "assembly.built": ("tenant_id", "build_ref", "assembly_item_id", "qty", "location_id", "cost_txn_ref"),
    "assembly.unbuilt": ("tenant_id", "unbuild_ref", "assembly_item_id", "qty", "components", "variance_value"),
    "component.consumed": ("tenant_id", "source_ref", "components"),
    "wip.issued": ("tenant_id", "wip_transaction_id", "work_order_id", "lines", "value_issued"),
    "wip.completed": ("tenant_id", "wip_transaction_id", "work_order_id", "qty", "value_completed", "variance"),
    "wip.backflushed": ("tenant_id", "wip_transaction_id", "components", "qty"),
    "wip.closed": ("tenant_id", "wip_transaction_id", "work_order_id", "residual_value", "variance_ref"),
    "operation.completed": (
        "tenant_id",
        "work_order_id",
        "operation_id",
        "qty_completed",
        "labor_hours",
        "machine_hours",
    ),
    "eco.implemented": ("tenant_id", "eco_id", "effective_date", "applied_lines"),
    "scheduling.run.completed": (
        "tenant_id",
        "scheduling_run_id",
        "status",
        "method",
        "operation_count",
        "policy_version",
    ),
    "available_to_build.calculated": ("tenant_id", "item_id", "location_id", "buildable_qty", "policy_version"),
    "manufacturing.compat.rule_set.activated": (
        "tenant_id",
        "rule_set_id",
        "scope_type",
        "scope_ref_id",
        "enforcement_mode",
        "rule_count",
    ),
    "manufacturing.config.validated": (
        "tenant_id",
        "configuration_id",
        "configuration_hash",
        "context",
        "assembly_item_id",
        "result",
    ),
    "manufacturing.config.violation": (
        "tenant_id",
        "configuration_id",
        "rule_set_code",
        "rule_code",
        "severity",
        "message_code",
    ),
    "assembly.unbuild.disposition.recorded": ("tenant_id", "unbuild_id", "lines", "scrap_value", "serial_lot_refs"),
    "manufacturing.preferences.changed": ("tenant_id", "changed_keys", "actor"),
}


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
    from app.api.routers.manufacturing import ROUTE_SCOPES, router

    app = FastAPI(title="procurement contract builder", openapi_url="/openapi.json")
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
                f"fs09_{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
            )
            resolved["tags"] = ["FS-09", "manufacturing.execution"]
            resolved["x-feature-set"] = "FS-09"
            resolved["x-contract"] = ["manufacturing.execution"]
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
            "title": "FS-09 manufacturing.execution builder fragment",
            "version": _plan_version(),
            "description": "Implementation-backed FS-09 Procurement P2P operations (generated from the router).",
            "x-feature-set": "FS-09",
            "x-contract": "manufacturing.execution",
            "x-implemented-operations": sum(len(item) for item in paths.values()),
        },
        "paths": paths,
    }


def event_schema(event: str) -> dict[str, Any]:
    fields = EVENT_FIELDS[event]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://contracts.inventory-platform.local/manufacturing.execution/{event}/1.0.0",
        "title": event,
        "description": f"manufacturing.execution@1.0.0 {event} payload (final outbox envelope; data fields implemented).",
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
        "x-contract": "manufacturing.execution",
        "x-feature-set": "FS-09",
        "x-payload-key-fields": list(fields),
    }


def build_event_schemas() -> dict[Path, dict[str, Any]]:
    return {HERE / "events" / f"{event}.schema.json": event_schema(event) for event in EVENT_FIELDS}


def build_outputs() -> dict[Path, dict[str, Any]]:
    return {HERE / "openapi.fragment.json": build_fragment(), **build_event_schemas()}


def write_all() -> list[Path]:
    written: list[Path] = []
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
    parser = argparse.ArgumentParser(description="Build the FS-09 procurement contract artefacts")
    parser.add_argument("--check", action="store_true", help="verify the committed artefacts are current")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    written = write_all()
    print(f"wrote {len(written)} artefacts under {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
