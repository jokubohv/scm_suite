"""FS-00 provider contract gate (B3): provided contracts, OpenAPI coverage, events, determinism.

This is the root-level provider suite the FS-00 exit gate expects (``feature-sets.json``
``exit_checks``). It complements the deeper ``app/tests/contracts/platform`` freeze suite by asserting
the *provider guarantees* that dependants rely on:

* FS-00 publishes exactly the five manifest contracts at their declared versions
  (``platform.core@1.0.0``, ``platform.events@1.0.0``, ``platform.finance@1.0.0``,
  ``order.demand@0.1.0``, ``order.supply@0.1.0``) and every contract name/version is unique in the plan;
* the mounted app's OpenAPI carries every operation committed in the FS-00 builder fragment and every
  aggregate operation marked ``x-implemented`` from the FS-00 skeleton, each with exactly one
  catalogued capability scope (or an explicit allowlist);
* every implemented FS-00 event is registered under the FS-00 contracts and its JSON Schema carries the
  frozen envelope;
* generation is deterministic (repeated ``app.openapi()`` calls are byte-equal and operation ids are
  unique).

Static/pure: no database required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_PLATFORM_HELPERS = _HERE / "platform"
if str(_PLATFORM_HELPERS) not in sys.path:  # shared freeze-test helpers (top-level module by design)
    sys.path.insert(0, str(_PLATFORM_HELPERS))

import contract_utils as utils  # noqa: E402  (path shim above)

pytestmark = pytest.mark.contract

EXPECTED_FS00_PROVIDES = {
    "platform.core": "1.0.0",
    "platform.events": "1.0.0",
    "platform.finance": "1.0.0",
    "order.demand": "0.1.0",
    "order.supply": "0.1.0",
}

#: Event types the FS-00 implementation emits through the outbox today (source audit).
IMPLEMENTED_FS00_EVENT_TYPES = (
    "platform.approval.requested",
    "platform.approval.approved",
    "platform.approval.rejected",
    "platform.flag.changed",
)


def _manifest() -> dict:
    return json.loads((utils.REPO_ROOT / "design/feature-sets.json").read_text(encoding="utf-8"))


def _fs00() -> dict:
    for feature_set in _manifest()["feature_sets"]:
        if feature_set["id"] == "FS-00":
            return feature_set
    raise AssertionError("FS-00 missing from feature-set manifest")


def _openapi_operations(document: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in (document.get("paths") or {}).items()
        for method, operation in item.items()
        if isinstance(operation, dict) and method.upper() in utils.HTTP_METHODS
    }


def test_fs00_publishes_the_declared_contract_versions() -> None:
    fs00 = _fs00()
    provided = {entry["contract"]: entry["version"] for entry in fs00["provides"]}
    assert provided == EXPECTED_FS00_PROVIDES

    # Contract names are unique plan-wide and every version is a concrete semver.
    names: list[str] = []
    for feature_set in _manifest()["feature_sets"]:
        for entry in feature_set["provides"]:
            names.append(entry["contract"])
            parts = entry["version"].split(".")
            assert len(parts) == 3 and all(part.isdigit() for part in parts), entry
    assert len(names) == len(set(names)), "duplicate provided contract name in the plan"

    registry = utils.load_json("contracts/events/registry.json")
    by_name = {entry["contract"]: entry for entry in registry["contracts"]}
    for contract, version in EXPECTED_FS00_PROVIDES.items():
        assert contract in by_name, f"FS-00 contract {contract} missing from event registry"
        assert by_name[contract]["version"] == version, (
            f"event-registry version drift for {contract}: {by_name[contract]['version']} != manifest {version}"
        )


def test_app_openapi_covers_the_committed_builder_fragment() -> None:
    spec = utils.load_app().openapi()
    app_ops = _openapi_operations(spec)
    fragment = utils.platform_fragment()
    fragment_ops = utils.fragment_operations(fragment)

    missing = sorted(f"{method} {path}" for method, path in fragment_ops if (method, path) not in app_ops)
    assert missing == [], f"committed provider fragment operations missing from app OpenAPI: {missing}"
    assert fragment["x-implemented-operations"] == len(fragment_ops)
    assert fragment["info"]["x-feature-set"] == "FS-00"

    catalogue = utils.catalogue_scopes()
    problems: list[str] = []
    operation_ids: list[str] = []
    for (method, path), operation in fragment_ops.items():
        scopes = list(operation.get("x-required-scopes") or [])
        allowlist = operation.get("x-allowlist")
        operation_ids.append(operation.get("operationId"))
        if scopes and len(scopes) != 1:
            problems.append(f"{method} {path}: expected exactly one scope, got {scopes}")
        if scopes and scopes[0] not in catalogue:
            problems.append(f"{method} {path}: scope outside catalogue: {scopes}")
        if not scopes and allowlist not in {"public", "self-service"}:
            problems.append(f"{method} {path}: no scope and no valid allowlist ({allowlist!r})")
        if scopes and allowlist:
            problems.append(f"{method} {path}: declared both a scope and an allowlist")
        if operation.get("x-implemented") is not True:
            problems.append(f"{method} {path}: implemented operation must carry x-implemented=true")
    assert problems == [], problems
    assert len(operation_ids) == len(set(operation_ids)), "duplicate operationIds in provider fragment"


def test_app_openapi_covers_implemented_aggregate_operations() -> None:
    spec = utils.load_app().openapi()
    app_ops = _openapi_operations(spec)
    aggregate = utils.aggregate_document()
    aggregate_ops = _openapi_operations(aggregate)

    implemented = {key: operation for key, operation in aggregate_ops.items() if operation.get("x-implemented") is True}
    missing = sorted(f"{method} {path}" for method, path in implemented if (method, path) not in app_ops)
    assert missing == [], f"aggregate operations marked implemented are missing from app OpenAPI: {missing}"

    fragment_ops = utils.fragment_operations(utils.platform_fragment())
    counts = aggregate["x-counts"]
    implemented_total = sum(1 for operation in aggregate_ops.values() if operation.get("x-implemented") is True)
    assert counts["implemented_operations"] == implemented_total
    assert counts["skeleton_operations"] + counts["implemented_operations"] == counts["operations"]
    assert counts["operations"] == sum(len(item) for item in aggregate["paths"].values())

    fs00_fragment = utils.load_json("contracts/openapi/fragments/FS-00.openapi.json")
    fs00_ops = sum(len(item) for item in fs00_fragment["paths"].values())
    fs00_impl = sum(
        1 for item in fs00_fragment["paths"].values() for operation in item.values() if operation.get("x-implemented") is True
    )
    assert fs00_fragment["info"]["x-implemented-operations"] == fs00_impl
    assert fs00_ops >= fs00_impl


def test_implemented_fs00_events_carry_the_frozen_envelope() -> None:
    registry = utils.load_json("contracts/events/registry.json")
    entries = utils.fs00_event_registry_entries()
    names = {entry["contract"] for entry in entries}
    assert set(EXPECTED_FS00_PROVIDES) <= names

    registered = {event["event"]: event for entry in entries for event in (entry.get("events") or [])}
    missing = [event for event in IMPLEMENTED_FS00_EVENT_TYPES if event not in registered]
    assert missing == [], f"implemented FS-00 events missing from the registry: {missing}"

    envelope = registry["envelope"]
    required = set(envelope["required"])
    problems: list[str] = []
    for event in IMPLEMENTED_FS00_EVENT_TYPES:
        schema_path = utils.REPO_ROOT / registered[event]["schema"]
        assert schema_path.is_file(), f"missing event schema for {event}: {schema_path}"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not required <= set(schema.get("required") or []):
            problems.append(f"{event}: schema does not require the frozen envelope {sorted(required)}")
        if "data" not in (schema.get("properties") or {}):
            problems.append(f"{event}: schema has no data payload property")
        if schema.get("x-todo") is True:
            print(f"event payload TODO debt: {event} (registered, envelope valid)")
    assert problems == [], problems


def test_provider_generation_is_deterministic() -> None:
    first = json.dumps(utils.load_app().openapi(), sort_keys=True, default=str)
    second = json.dumps(utils.load_app().openapi(), sort_keys=True, default=str)
    assert first == second, "app.openapi() must be deterministic across calls"

    spec = json.loads(first)
    operation_ids = [
        operation.get("operationId")
        for item in spec["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict)
    ]
    assert len(operation_ids) == len(set(operation_ids)), "duplicate operationIds in app OpenAPI"

    fragment = utils.platform_fragment()
    assert fragment["openapi"].startswith("3.1")
    aggregate = utils.aggregate_document()
    assert aggregate["openapi"].startswith("3.1")
    registry = utils.load_json("contracts/events/registry.json")
    assert registry["plan_version"] == _manifest()["plan_version"]
