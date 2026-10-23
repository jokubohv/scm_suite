"""FS-00 provider contract tests: app.openapi() ↔ builder fragment ↔ aggregate ↔ event registry.

These are the C0 provider checks: the implementation-backed builder fragment must be derived from the
mounted app without drift, the aggregate must merge it over the spec skeleton, and the FS-00 event
contracts must stay envelope-valid (payload TODO debt is reported, not silently changed).
"""

from __future__ import annotations

import json
from pathlib import Path

import contract_utils as utils

# Event types actually emitted by FS-00 implementation today (outbox.emits found by source audit):
# platform.approval.* (app/core/approvals.py) and platform.flag.changed (config repository).
IMPLEMENTED_EVENT_TYPES = (
    "platform.approval.requested",
    "platform.approval.approved",
    "platform.approval.rejected",
    "platform.flag.changed",
)


def test_builder_fragment_is_app_derived_and_complete() -> None:
    app = utils.load_app()
    spec = app.openapi()
    fragment = utils.platform_fragment()
    ops = utils.fragment_operations(fragment)
    spec_ops = {
        (method.upper(), path): operation
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method.upper() in utils.HTTP_METHODS
    }
    # Everything in the schema-derived contract is present (except intentionally skipped unscoped routes).
    skipped = set(fragment.get("x-skipped-unscoped-routes") or [])
    missing = sorted(f"{method} {path}" for method, path in spec_ops if (method, path) not in ops and f"{method} {path}" not in skipped)
    assert missing == [], missing
    assert fragment["x-implemented-operations"] == len(ops)
    assert fragment["info"]["x-status"] == "c0-candidate"
    assert fragment["info"]["x-feature-set"] == "FS-00"

    operation_ids = [operation["operationId"] for operation in ops.values()]
    assert len(operation_ids) == len(set(operation_ids)), "duplicate operationIds in builder fragment"

    aggregate = utils.aggregate_document()
    spec_operation_ids = set()
    for item in aggregate["paths"].values():
        for operation in item.values():
            if operation.get("x-implemented") is not True:
                spec_operation_ids.add(operation.get("operationId"))
    assert not (set(operation_ids) & spec_operation_ids), "builder operationId collides with a spec skeleton operation"


def test_implemented_operations_are_scope_covered_and_schema_rich() -> None:
    catalogue = utils.catalogue_scopes()
    registry = utils.scope_registry()
    ops = utils.fragment_operations(utils.platform_fragment())
    problems: list[str] = []
    for (method, path), operation in ops.items():
        scopes = list(operation.get("x-required-scopes") or [])
        allowlist = operation.get("x-allowlist")
        if not scopes and not allowlist:
            problems.append(f"{method} {path}: no scope/allowlist")
        if scopes and any(scope not in catalogue for scope in scopes):
            problems.append(f"{method} {path}: unknown scope {scopes}")
        if allowlist and allowlist not in {"public", "self-service"}:
            problems.append(f"{method} {path}: invalid allowlist {allowlist!r}")
        if scopes and allowlist:
            problems.append(f"{method} {path}: both scope and allowlist")
        if scopes and len(scopes) != 1:
            problems.append(f"{method} {path}: expected exactly one scope, got {scopes}")
        if not operation.get("x-implemented") or operation.get("x-skeleton") is not False:
            problems.append(f"{method} {path}: missing implementation markers")
        responses = operation.get("responses") or {}
        if not any(code in responses for code in ("200", "201", "204")):
            problems.append(f"{method} {path}: no success response")
        for code, response in responses.items():
            if code.startswith("2") and code != "204" and not response.get("content"):
                problems.append(f"{method} {path}: {code} has no response schema")
        # route classification: scoped routes must use exactly one catalogue scope
        if path not in {"/healthz", "/readyz"}:
            if not registry.is_public_route(method, path) and not registry.is_self_service_route(method, path):
                assert len(scopes) == 1, (method, path, scopes)
    assert problems == [], problems


def test_aggregate_merges_every_implemented_operation() -> None:
    fragment_ops = utils.fragment_operations(utils.platform_fragment())
    aggregate = utils.aggregate_document()
    aggregate_ops = {
        (method.upper(), path): operation
        for path, item in aggregate["paths"].items()
        for method, operation in item.items()
        if method.upper() in utils.HTTP_METHODS
    }
    missing: list[str] = []
    for key in fragment_ops:
        merged = aggregate_ops.get(key)
        if merged is None:
            missing.append(f"{key[0]} {key[1]}")
            continue
        if merged.get("x-implemented") is not True:
            missing.append(f"{key[0]} {key[1]} (skeleton not overridden)")
    assert missing == [], missing
    counts = aggregate["x-counts"]
    assert counts["implemented_operations"] == len(fragment_ops)
    assert counts["skeleton_operations"] + counts["implemented_operations"] == counts["operations"]
    fs00 = json.loads((utils.REPO_ROOT / "contracts/openapi/fragments/FS-00.openapi.json").read_text(encoding="utf-8"))
    fs00_ops = sum(len(item) for item in fs00["paths"].values())
    assert fs00["info"]["x-implemented-operations"] == len(fragment_ops)
    assert fs00_ops >= len(fragment_ops)


def test_fs00_event_contracts_are_registered_and_envelope_valid() -> None:
    entries = utils.fs00_event_registry_entries()
    names = {entry["contract"] for entry in entries}
    assert {"platform.core", "platform.events", "platform.finance", "order.demand", "order.supply"} <= names
    registered = {
        event["event"]: event
        for entry in entries
        for event in entry.get("events") or []
    }
    missing = [event for event in IMPLEMENTED_EVENT_TYPES if event not in registered]
    assert missing == [], f"implemented events missing from FS-00 contracts: {missing}"
    envelope_required = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}
    todo_payloads = 0
    for event_name in IMPLEMENTED_EVENT_TYPES:
        schema_path = utils.REPO_ROOT / registered[event_name]["schema"]
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert envelope_required <= set(schema.get("required") or []), event_name
        assert "data" in schema.get("properties", {}), event_name
        todo_payloads += 1 if schema.get("x-todo") is True else 0
    print(f"event payload TODO debt (implemented events): {todo_payloads}/{len(IMPLEMENTED_EVENT_TYPES)}")


def test_builder_fragment_files_are_deterministic_and_checked_by_script() -> None:
    """The committed builder fragments must match a fresh app-derived build (script --check)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(utils.REPO_ROOT / "app/contracts/platform/build_from_app.py"), "--check"],
        cwd=utils.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    order_fragment = Path(utils.REPO_ROOT / "app/contracts/order_boundary/openapi.fragment.json")
    document = json.loads(order_fragment.read_text(encoding="utf-8"))
    assert document["info"]["x-status"] == "pending-implementation"
    assert document["paths"] == {}
