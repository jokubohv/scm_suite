#!/usr/bin/env python3
"""Deterministic C0 contract metrics snapshot (run from the repo root; no DB needed).

Prints + optionally writes JSON: app route counts, builder-fragment coverage, aggregate counts,
FS-00 fragment counts, skeleton/TODO debt (by feature set), FS-00 residual skeletons/additions,
event registry counts and implemented-vs-contracted outbox events, route-scope coverage summary,
and SHA-256 hashes of the freeze artefacts.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory")

METHODS = ("get", "post", "put", "patch", "delete")
HTTP = {method.upper() for method in METHODS}
HASHED_ARTEFACTS = [
    "contracts/openapi/openapi.json",
    "contracts/openapi/openapi.yaml",
    "contracts/openapi/contracts.json",
    "contracts/openapi/fragments/FS-00.openapi.json",
    "contracts/events/registry.json",
    "app/contracts/platform/openapi.fragment.json",
    "app/contracts/order_boundary/openapi.fragment.json",
]
FS00_CONTRACTS = {"platform.core", "platform.events", "platform.finance", "order.demand", "order.supply"}
# Outbox event types emitted by the FS-00 implementation today (source audit: app/core/approvals.py,
# app/core/repositories/config.py). Contract registry must already contain them (additive freeze rule).
IMPLEMENTED_OUTBOX_EVENTS = (
    "platform.approval.requested",
    "platform.approval.approved",
    "platform.approval.rejected",
    "platform.flag.changed",
)


def _load(relpath: str) -> dict:
    return json.loads((REPO_ROOT / relpath).read_text(encoding="utf-8"))


def _leaf_routes(routes):
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _leaf_routes(route.original_router.routes)
        elif hasattr(route, "path") and getattr(route, "methods", None):
            yield route


def _route_scopes(route):
    found: list[str] = []

    def add(function) -> None:
        if function is None:
            return
        try:
            values = inspect.getclosurevars(function).nonlocals
        except (TypeError, ValueError):
            return
        scope = values.get("scope")
        if isinstance(scope, str) and scope not in found:
            found.append(scope)

    for dependency in getattr(route, "dependencies", []) or []:
        add(getattr(dependency, "dependency", None))

    def walk(dependant) -> None:
        for dependency in getattr(dependant, "dependencies", []) or []:
            add(getattr(dependency, "call", None))
            walk(dependency)

    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        walk(dependant)
    return found


def collect() -> dict:
    from app.api.main import app
    from app.contracts.parse_specs import build_model
    from app.core.security.scopes import ScopeRegistry

    spec = app.openapi()
    registry = ScopeRegistry()
    app_ops = {(m.upper(), path) for path, item in spec["paths"].items() for m in item if m in METHODS}
    routes = list(_leaf_routes(app.routes))
    mounted: list[tuple[str, str, list[str], bool, bool, bool]] = []
    for route in routes:
        for method in sorted(route.methods or ()):
            if method not in HTTP:
                continue
            mounted.append(
                (
                    method,
                    route.path,
                    _route_scopes(route),
                    registry.is_public_route(method, route.path),
                    registry.is_self_service_route(method, route.path),
                    bool(getattr(route, "include_in_schema", True)),
                )
            )
    framework = {"/api/v1/openapi.json", "/api/v1/docs", "/docs/oauth2-redirect", "/internal/deps"}
    unresolved = [f"{m} {p}" for m, p, scopes, public, self_service, _ in mounted if not scopes and not public and not self_service and p not in framework]

    fragment = _load("app/contracts/platform/openapi.fragment.json")
    fragment_ops = {(m.upper(), path) for path, item in fragment["paths"].items() for m in item}
    aggregate = _load("contracts/openapi/openapi.json")
    aggregate_ops = {
        (m.upper(), path): operation
        for path, item in aggregate["paths"].items()
        for m, operation in item.items()
        if m in METHODS
    }
    model = build_model()
    fs00 = next(feature_set for feature_set in model.feature_sets if feature_set.id == "FS-00")
    spec_fs00 = {(endpoint.method.upper(), endpoint.path) for endpoint in fs00.endpoints}
    fs00_fragment = _load("contracts/openapi/fragments/FS-00.openapi.json")
    skeleton_by_fs: dict[str, int] = {}
    for operation in aggregate_ops.values():
        if operation.get("x-skeleton") is True:
            fs = operation.get("x-feature-set") or "?"
            skeleton_by_fs[fs] = skeleton_by_fs.get(fs, 0) + 1
    fs00_skeleton = sorted(
        f"{method} {path}"
        for (method, path), operation in aggregate_ops.items()
        if operation.get("x-feature-set") == "FS-00" and operation.get("x-skeleton") is True
    )
    added = sorted(f"{method} {path}" for method, path in (fragment_ops - spec_fs00))
    overridden = sorted(f"{method} {path}" for method, path in (fragment_ops & spec_fs00))

    event_registry = _load("contracts/events/registry.json")
    fs00_events = [
        event
        for contract in event_registry["contracts"]
        if contract["contract"] in FS00_CONTRACTS
        for event in contract.get("events") or []
    ]
    registered_event_names = {
        event["event"] for contract in event_registry["contracts"] for event in contract.get("events") or []
    }
    payload_todo = 0
    payload_todo_by_contract: dict[str, int] = {}
    for contract in event_registry["contracts"]:
        for event in contract.get("events") or []:
            schema = _load(event["schema"])
            if schema.get("x-todo") is True:
                payload_todo += 1
                payload_todo_by_contract[contract["contract"]] = payload_todo_by_contract.get(contract["contract"], 0) + 1

    hashes = {
        relpath: hashlib.sha256((REPO_ROOT / relpath).read_bytes()).hexdigest()
        for relpath in HASHED_ARTEFACTS
    }
    return {
        "plan_version": _load("design/feature-sets.json")["plan_version"],
        "app": {"mounted_operations": len(mounted), "openapi_paths": len(spec["paths"]), "openapi_operations": len(app_ops)},
        "route_scope": {
            "scoped": sum(1 for _, _, scopes, public, self_service, _ in mounted if scopes and not public and not self_service),
            "public": sum(1 for _, _, _, public, _, _ in mounted if public),
            "self_service": sum(1 for _, _, _, _, self_service, _ in mounted if self_service),
            "framework_operational": sum(1 for _, path, _, _, _, _ in mounted if path in framework),
            "unresolved": unresolved,
        },
        "builder_fragment": {
            "paths": len(fragment["paths"]),
            "operations": len(fragment_ops),
            "skipped_unscoped": fragment.get("x-skipped-unscoped-routes") or [],
            "hashed": hashes["app/contracts/platform/openapi.fragment.json"],
        },
        "aggregate": aggregate["x-counts"],
        "fs00": {
            "spec_operations": len(spec_fs00),
            "fragment_operations": sum(len(item) for item in fs00_fragment["paths"].values()),
            "fragment_paths": len(fs00_fragment["paths"]),
            "implemented_operations": fs00_fragment["info"].get("x-implemented-operations"),
            "overridden_spec_operations": len(overridden),
            "added_operations": len(added),
            "skeleton_operations": len(fs00_skeleton),
            "added_operation_list": added,
            "residual_skeleton_list": fs00_skeleton,
        },
        "skeleton_by_feature_set": dict(sorted(skeleton_by_fs.items())),
        "events": {
            "registry_contracts": len(event_registry["contracts"]),
            "registry_events": event_registry["counts"]["events"],
            "fs00_contract_events": len(fs00_events),
            "implemented_outbox_events": list(IMPLEMENTED_OUTBOX_EVENTS),
            "implemented_events_missing_from_registry": [
                name for name in IMPLEMENTED_OUTBOX_EVENTS if name not in registered_event_names
            ],
            "payload_todo_schemas": payload_todo,
            "payload_todo_by_contract": dict(sorted(payload_todo_by_contract.items())),
        },
        "hashes": hashes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C0 contract metrics")
    parser.add_argument("--json", type=Path, default=None, help="also write machine-readable JSON")
    args = parser.parse_args(argv)
    data = collect()
    text = json.dumps(data, indent=2, sort_keys=True)
    print(text)
    if args.json is not None:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
