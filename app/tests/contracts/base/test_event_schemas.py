"""Event-contract registry + JSON-Schema envelope tests (S0-04)."""

from __future__ import annotations

import json

from app.contracts import paths

from . import fixtures
from .schema_validation import assert_event_schema


def test_registry_contracts_and_counts() -> None:
    registry = fixtures.committed_event_registry()
    assert registry["plan_version"] == fixtures.manifest()["plan_version"]
    assert len(registry["contracts"]) == 21
    assert registry["counts"]["contracts"] == 21
    assert registry["counts"]["feature_sets"] == 16
    assert registry["counts"]["event_schemas"] == len(list(paths.EVENTS_DIR.rglob("*.schema.json")))
    assert isinstance(registry["event_families"], list)


def test_every_registered_event_has_a_valid_schema() -> None:
    registry = fixtures.committed_event_registry()
    checked = 0
    for entry in registry["contracts"]:
        assert entry["version"], entry["contract"]
        for event in entry["events"]:
            schema_path = paths.REPO_ROOT / event["schema"]
            assert schema_path.exists(), event["schema"]
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            assert_event_schema(schema)
            assert schema["x-event-type"] == event["event"]
            assert schema["x-event-version"] == event["version"]
            assert schema["x-contract"] == entry["contract"]
            assert schema["x-feature-set"] == entry["feature_set"]
            if schema.get("x-todo") is True:
                assert "TODO" in schema["properties"]["data"].get("description", schema.get("description", "TODO"))
            else:  # implementation-backed payload folded into the canonical envelope
                data = schema["properties"]["data"]
                assert isinstance(data.get("properties"), dict), f"{event['event']} payload schema is empty"
            checked += 1
    assert checked == registry["counts"]["event_schemas"]


def test_event_schema_ids_are_unique() -> None:
    ids: set[str] = set()
    for schema_path in fixtures.event_schema_paths():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema["$id"] not in ids, f"duplicate $id {schema['$id']}"
        ids.add(schema["$id"])
    assert len(ids) == len(fixtures.event_schema_paths())


def test_required_event_versions_present() -> None:
    registry = fixtures.committed_event_registry()
    names = {event["event"] for entry in registry["contracts"] for event in entry["events"]}
    # Spot-check the S0-critical contracts: outbox/order/finance/cost/manufacturing events.
    for expected in (
        "platform.flag.changed",
        "order.demand.changed",
        "finance.journal_intent.recorded",
        "inventory.receipt.posted",
        "cost.layer.created",
        "assembly.built",
        "work_order.created",
    ):
        assert expected in names, f"missing event contract {expected}"
