"""FS-01 provider contract: `item.master@1.0.0` facade, OpenAPI fragment and event catalogue."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.outbox import event_envelope
from app.modules.item_master import facade

REPO_ROOT = Path(__file__).resolve().parents[3]
FRAGMENT = REPO_ROOT / "app" / "contracts" / "item_master" / "openapi.fragment.json"
CONTRACT_META = REPO_ROOT / "app" / "contracts" / "item_master" / "contract.json"


def _fragment() -> dict:
    return json.loads(FRAGMENT.read_text(encoding="utf-8"))


def test_provider_contract_version_and_metadata() -> None:
    assert facade.CONTRACT_VERSION == "1.0.0"
    meta = json.loads(CONTRACT_META.read_text(encoding="utf-8"))
    contract = meta["contracts"][0]
    assert contract["contract"] == "item.master" and contract["version"] == "1.0.0"
    assert contract["feature_set"] == "FS-01"
    event_names = {event["event"] for event in contract["events"]}
    assert event_names == {
        "item.created",
        "item.updated",
        "item.inactivated",
        "item.uom.changed",
        "item.matrix.changed",
        "item.barcode.added",
        "item.planning_values.updated",
    }
    for event in contract["events"]:
        assert event["version"] == "1.0.0"


def test_facade_surface_is_callable() -> None:
    expected = {
        "get_item", "require_active_item", "posting_eligibility", "convert_quantity", "base_uom",
        "get_item_location_settings", "apply_purchase_price_update", "validate_item_references",
        "apply_planning_values", "get_kit_members", "mirror_upc_code",
    }
    for name in expected:
        assert callable(getattr(facade, name)), name


def test_openapi_fragment_is_implementation_backed() -> None:
    fragment = _fragment()
    assert fragment["openapi"].startswith("3.1")
    assert fragment["info"]["x-contract"] == "item.master"
    paths = fragment["paths"]
    assert len(paths) >= 35
    required = {
        ("/api/v1/items", "post"),
        ("/api/v1/items", "get"),
        ("/api/v1/items/{item_id}", "get"),
        ("/api/v1/items/{item_id}", "patch"),
        ("/api/v1/items/{item_id}/uoms", "put"),
        ("/api/v1/items/{item_id}/inventory", "get"),
        ("/api/v1/items/{item_id}/barcodes", "post"),
        ("/api/v1/items/{item_id}/matrix-children/generate", "post"),
        ("/api/v1/item-types", "get"),
        ("/api/v1/item-categories", "post"),
        ("/api/v1/uom-types", "post"),
        ("/api/v1/uom-conversions/resolve", "post"),
    }
    for path, method in required:
        assert path in paths, path
        assert method in paths[path], (path, method)
    operations = [operation for methods in paths.values() for operation in methods.values()]
    assert all(operation.get("x-implemented") is True for operation in operations)
    assert all("TODO" not in json.dumps(operation) for operation in operations)


def test_event_envelope_matches_platform_contract() -> None:
    envelope = event_envelope(
        event_type="item.created",
        tenant_id="11111111-1111-1111-1111-111111111111",
        aggregate_type="item",
        aggregate_id="22222222-2222-2222-2222-222222222222",
        payload={"item_id": "22222222-2222-2222-2222-222222222222", "item_number": "ITEM-1"},
    )
    payload = envelope["payload"]
    for key in ("event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"):
        assert key in payload
    assert payload["event_type"] == "item.created"
    assert payload["aggregate"] == {"type": "item", "id": "22222222-2222-2222-2222-222222222222", "version": 1}
    assert payload["data"]["item_number"] == "ITEM-1"
