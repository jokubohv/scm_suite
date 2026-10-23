"""FS-06 provider contract gate: `traceability.numbered@1.0.0` (manifest exit command).

Validates the implementation-backed provider surface against the plan manifest and the authoritative
permission catalogue:

* FS-06 publishes exactly `traceability.numbered@1.0.0` (manifest + `contract.json` + registry agree);
* the committed OpenAPI fragment is derived deterministically from the mounted FS-06 router, covers all
  14 routes, and every operation declares exactly one catalogued capability scope;
* the five required v1 events are registered with final envelope + payload schemas;
* the facade's pure hooks (validation, FEFO ordering, event mapping) are deterministic — consumers rely
  on these contracts, not on module internals.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "traceability_numbered"
FRAGMENT = CONTRACT_DIR / "openapi.fragment.json"
CONTRACT = "traceability.numbered"
VERSION = "1.0.0"
REQUIRED_EVENTS = {
    "traceability.lot.created",
    "traceability.lot.inactivated",
    "traceability.serial.created",
    "traceability.serial.state_changed",
    "traceability.expiry.warning",
}
ENVELOPE_REQUIRED = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}

pytestmark = pytest.mark.contract


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_fs06() -> dict:
    manifest = _load(REPO_ROOT / "design" / "feature-sets.json")
    return next(item for item in manifest["feature_sets"] if item["id"] == "FS-06")


def _catalogue_scopes() -> set[str]:
    catalogue = _load(REPO_ROOT / "design" / "permissions.json")
    return {entry["scope"] for entry in catalogue["catalogue"]}


def test_fs06_publishes_the_manifest_contract_version() -> None:
    fs06 = _manifest_fs06()
    assert fs06["provides"] == [{"contract": CONTRACT, "version": VERSION}]
    assert fs06["wave"] == "1A"
    contract_meta = _load(CONTRACT_DIR / "contract.json")
    entry = next(item for item in contract_meta["contracts"] if item["contract"] == CONTRACT)
    assert entry["version"] == VERSION
    assert entry["feature_set"] == "FS-06"
    assert set(entry["depends_on"]) == {
        "costing.engine",
        "inventory.ledger",
        "item.master",
        "platform.core",
        "platform.events",
        "traceability.registry",
    }


def test_registry_declares_the_fs06_contract_and_events() -> None:
    registry = _load(REPO_ROOT / "contracts" / "events" / "registry.json")
    entry = next(item for item in registry["contracts"] if item["contract"] == CONTRACT)
    assert entry["version"] == VERSION
    registered = {event["event"]: event for event in entry["events"]}
    assert REQUIRED_EVENTS <= set(registered)
    for event in REQUIRED_EVENTS:
        schema_path = REPO_ROOT / registered[event]["schema"]
        assert schema_path.is_file(), f"missing canonical schema for {event}"
        schema = _load(schema_path)
        assert ENVELOPE_REQUIRED <= set(schema.get("required") or []), event
        assert "data" in (schema.get("properties") or {}), event
        if schema.get("x-todo") is True:
            print(f"canonical registry payload schema TODO debt: {event} (envelope final; extension schema final)")


def test_committed_fragment_is_router_derived_and_deterministic() -> None:
    result = subprocess.run(
        [sys.executable, str(CONTRACT_DIR / "build_from_app.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    fragment = _load(FRAGMENT)
    assert fragment["info"]["x-feature-set"] == "FS-06"
    assert fragment["info"]["x-contract"] == CONTRACT

    from app.api.routers.traceability_numbered import ROUTE_SCOPES

    operations = [
        (method.upper(), path, operation)
        for path, item in fragment["paths"].items()
        for method, operation in item.items()
    ]
    assert fragment["x-implemented-operations"] == len(ROUTE_SCOPES)
    assert len(operations) == len(ROUTE_SCOPES)
    assert {(method, path) for method, path, _ in operations} == set(ROUTE_SCOPES)
    catalogue = _catalogue_scopes()
    problems: list[str] = []
    operation_ids: list[str] = []
    for method, path, operation in operations:
        scopes = operation.get("x-required-scopes") or []
        operation_ids.append(operation["operationId"])
        if scopes != [ROUTE_SCOPES[(method, path)]]:
            problems.append(f"{method} {path}: fragment scopes {scopes} do not match router declaration")
        if scopes and scopes[0] not in catalogue:
            problems.append(f"{method} {path}: scope {scopes[0]} is not in design/permissions.json")
        if operation.get("x-feature-set") != "FS-06":
            problems.append(f"{method} {path}: wrong x-feature-set marker")
        if operation.get("x-contract-version") != VERSION:
            problems.append(f"{method} {path}: wrong x-contract-version")
        if operation.get("x-implemented") is not True:
            problems.append(f"{method} {path}: missing x-implemented marker")
    assert problems == [], problems
    assert len(operation_ids) == len(set(operation_ids))


def test_extension_event_schemas_are_final_and_envelope_valid() -> None:
    events_dir = CONTRACT_DIR / "events"
    for event in REQUIRED_EVENTS:
        schema = _load(events_dir / f"{event}.schema.json")
        assert ENVELOPE_REQUIRED <= set(schema.get("required") or []), event
        assert schema["properties"]["event_type"]["const"] == event
        assert schema["properties"]["event_version"]["const"] == VERSION
        assert schema["properties"]["data"]["type"] == "object"
        assert schema.get("x-contract") == CONTRACT
        assert schema.get("x-todo") is False


def test_facade_pure_hooks_are_deterministic() -> None:
    from datetime import date

    from app.contracts.traceability_numbered import (
        CONTRACT_NAME,
        CONTRACT_VERSION,
        NumberingPolicy,
        fefo_order_key,
        map_ledger_event,
        validate_numbered_details,
    )

    assert CONTRACT_NAME == CONTRACT and CONTRACT_VERSION == VERSION
    rows = [
        {"lot_id": "b", "lot_number": "B", "expiry_date": date(2026, 12, 1)},
        {"lot_id": "a", "lot_number": "A", "expiry_date": date(2026, 11, 1)},
    ]
    assert [row["lot_id"] for row in sorted(rows, key=fefo_order_key)] == ["a", "b"]
    assert map_ledger_event("FULFILLMENT") == "FULFILLED"
    policy = NumberingPolicy(
        item_id="i",
        item_type_code="SERIALIZED",
        requires_numbering=True,
        requires_lot=False,
        requires_serial=True,
    )
    violations = validate_numbered_details([{"serial_id": "s", "qty_base": "2"}], policy, qty_base="2")
    assert [violation.code for violation in violations] == ["SERIAL_QTY_MISMATCH"]
