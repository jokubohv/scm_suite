#!/usr/bin/env python3
"""Export the mounted FastAPI OpenAPI document as a deployable, deterministic artifact.

The committed ``contracts/openapi/openapi.json`` is the contract-first aggregate (spec surface plus
implementation overlays). The runtime document is the authoritative client-facing spec: it contains
only operations the service actually mounts and it is what ``/api/v1/openapi.json`` serves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deployment/runtime.openapi.json"


def _validate(spec: dict) -> list[str]:
    errors: list[str] = []
    if not str(spec.get("openapi", "")).startswith("3.1"):
        errors.append("runtime spec must be OpenAPI 3.1")
    paths = spec.get("paths") or {}
    operations = 0
    operation_ids: set[str] = set()
    todo: list[str] = []
    components = spec.get("components", {}).get("schemas", {})
    for path, item in paths.items():
        for method, operation in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            operations += 1
            operation_id = operation.get("operationId")
            if not operation_id:
                errors.append(f"{method.upper()} {path}: missing operationId")
            elif operation_id in operation_ids:
                errors.append(f"duplicate operationId {operation_id}")
            else:
                operation_ids.add(operation_id)
            if not operation.get("responses"):
                errors.append(f"{method.upper()} {path}: no responses")
            serialized = json.dumps(operation)
            if "TODO" in serialized or '"x-todo": true' in serialized:
                todo.append(f"{method.upper()} {path}")
            for body in (operation.get("requestBody"), *operation.get("responses", {}).values()):
                for ref in _refs(body):
                    name = ref.rsplit("/", 1)[-1]
                    if name not in components:
                        errors.append(f"{method.upper()} {path}: unresolved component {ref}")
    if operations < 900:
        errors.append(f"runtime spec unexpectedly small ({operations} operations)")
    if todo:
        errors.append(f"runtime spec contains TODO operations: {todo[:5]}")
    return errors


def _refs(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str) and child.startswith("#/components/schemas/"):
                yield child
            else:
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def build() -> dict:
    sys.path.insert(0, str(ROOT))
    from app.api.main import app

    return app.openapi()


def main() -> int:
    spec = build()
    errors = _validate(spec)
    if errors:
        print("runtime OpenAPI validation failed:")
        print("\n".join(f" - {error}" for error in errors))
        return 1
    payload = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
    if "--check" in sys.argv:
        if existing != payload:
            print(f"runtime OpenAPI artifact is stale: {OUTPUT.relative_to(ROOT)}")
            return 1
        print(f"runtime OpenAPI artifact is current: {OUTPUT.relative_to(ROOT)}")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(payload, encoding="utf-8")
    paths = len(spec.get("paths", {}))
    operations = sum(
        len([m for m in item if m in ("get", "post", "put", "patch", "delete")])
        for item in spec.get("paths", {}).values()
    )
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {paths} paths / {operations} operations, no TODO/unresolved refs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
