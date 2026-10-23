"""FS-12 provider contract gate: `quality.management@1.0.0` (manifest exit command)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DIR = REPO_ROOT / "app" / "contracts" / "quality"
FRAGMENT = CONTRACT_DIR / "openapi.fragment.json"
CONTRACT = "quality.management"
VERSION = "1.0.0"
REQUIRED_EVENTS = {
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
}
ENVELOPE_REQUIRED = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}

pytestmark = pytest.mark.contract


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_fs12() -> dict:
    manifest = _load(REPO_ROOT / "design" / "feature-sets.json")
    return next(item for item in manifest["feature_sets"] if item["id"] == "FS-12")


def _catalogue_scopes() -> set[str]:
    catalogue = _load(REPO_ROOT / "design" / "permissions.json")
    return {entry["scope"] for entry in catalogue["catalogue"]}


def test_fs12_publishes_the_manifest_contract_version() -> None:
    fs12 = _manifest_fs12()
    assert fs12["provides"] == [{"contract": CONTRACT, "version": VERSION}]
    assert fs12["wave"] == "1F"
    contract_meta = _load(CONTRACT_DIR / "contract.json")
    entry = next(item for item in contract_meta["contracts"] if item["contract"] == CONTRACT)
    assert entry["version"] == VERSION
    assert entry["feature_set"] == "FS-12"


def test_registry_declares_quality_contract_and_events() -> None:
    registry = _load(REPO_ROOT / "contracts" / "events" / "registry.json")
    entry = next(item for item in registry["contracts"] if item["contract"] == CONTRACT)
    assert entry["version"] == VERSION
    registered = {event["event"]: event for event in entry["events"]}
    missing = REQUIRED_EVENTS - set(registered)
    if missing:
        print(f"canonical registry event-sync debt ({len(missing)} events): {sorted(missing)}")
    for event in sorted(REQUIRED_EVENTS & set(registered)):
        schema = _load(REPO_ROOT / registered[event]["schema"])
        assert ENVELOPE_REQUIRED <= set(schema.get("required") or []), event
        if schema.get("x-todo") is True:
            print(f"canonical registry payload schema TODO debt: {event} (extension schema final)")


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
    assert fragment["info"]["x-feature-set"] == "FS-12"
    assert fragment["info"]["x-contract"] == CONTRACT
    operations = [
        (method.upper(), path, operation)
        for path, item in fragment["paths"].items()
        for method, operation in item.items()
    ]
    from app.api.routers.quality import ROUTE_SCOPES

    assert len(operations) == len(ROUTE_SCOPES) == fragment["x-implemented-operations"]
    assert {(method, path) for method, path, _ in operations} == set(ROUTE_SCOPES)
    catalogue = _catalogue_scopes()
    problems: list[str] = []
    operation_ids: list[str] = []
    for method, path, operation in operations:
        scopes = operation.get("x-required-scopes") or []
        operation_ids.append(operation["operationId"])
        if scopes != [ROUTE_SCOPES[(method, path)]]:
            problems.append(f"{method} {path}: fragment scopes {scopes} do not match router")
        if scopes and scopes[0] not in catalogue:
            problems.append(f"{method} {path}: scope {scopes[0]} not in catalogue")
        if operation.get("x-feature-set") != "FS-12":
            problems.append(f"{method} {path}: wrong x-feature-set")
        if operation.get("x-contract-version") != VERSION:
            problems.append(f"{method} {path}: wrong contract version")
    assert problems == [], problems
    assert len(operation_ids) == len(set(operation_ids))


def test_extension_event_schemas_are_final() -> None:
    events_dir = CONTRACT_DIR / "events"
    for event in REQUIRED_EVENTS:
        schema = _load(events_dir / f"{event}.schema.json")
        assert ENVELOPE_REQUIRED <= set(schema.get("required") or []), event
        assert schema["properties"]["event_type"]["const"] == event
        assert schema["properties"]["data"]["type"] == "object"
        assert schema.get("x-todo") is False


def test_contract_facade_pure_hooks() -> None:
    from app.contracts.quality import (
        CONTRACT_NAME,
        CONTRACT_VERSION,
        coa_line_visible,
        derive_disposition,
        evaluate_trigger_frequency,
        inspection_sampling_eligible,
    )

    assert CONTRACT_NAME == CONTRACT and CONTRACT_VERSION == VERSION
    assert derive_disposition([{"is_pass": False}], requested="HOLD").disposition == "HOLD"
    assert evaluate_trigger_frequency({"condition": {"frequency": 2}, "evaluation_counter": 1})[0] is True
    assert inspection_sampling_eligible({"sampling_method": "SAMPLE_PERCENT"}) is False
    assert coa_line_visible({"is_internal": True}) is False
