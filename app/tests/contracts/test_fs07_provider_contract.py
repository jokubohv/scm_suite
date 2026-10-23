"""FS-07 `counts.engine@1.0.0` provider contract (implementation-backed builder artefacts)."""

from __future__ import annotations

import json
from pathlib import Path

from app.api.routers.counts import ROUTE_SCOPES, router
from app.contracts.counts import CONTRACT_VERSION, CountsPort, schemas

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "counts"
KNOWN_SCOPES = {
    "inventory.count.read", "inventory.count.create", "inventory.count.capture",
    "inventory.count.approve", "inventory.count.post", "inventory.count.plan.manage",
    "inventory.count.recount",
}


def _fragment() -> dict:
    return json.loads((CONTRACT_DIR / "openapi.fragment.json").read_text(encoding="utf-8"))


def test_contract_version() -> None:
    assert CONTRACT_VERSION == "1.0.0"


def test_fragment_is_implementation_backed() -> None:
    fragment = _fragment()
    assert fragment["paths"]
    assert fragment["info"]["version"]
    seen: set[str] = set()
    for path, operations in fragment["paths"].items():
        assert path.startswith("/api/v1/")
        for method, operation in operations.items():
            key = (method.upper(), path)
            assert key in ROUTE_SCOPES, f"{key} missing declared scope"
            assert operation["x-implemented"] is True
            assert operation["x-skeleton"] is False
            assert operation["x-feature-set"] == "FS-07"
            assert operation["x-contract"] == ["counts.engine"]
            assert operation["x-required-scopes"] == [ROUTE_SCOPES[key]]
            assert operation["operationId"] not in seen
            seen.add(operation["operationId"])
            if method in {"post", "patch", "put", "delete"}:
                assert "Idempotency-Key" in {parameter["name"] for parameter in operation.get("parameters", [])}


def test_router_operations_all_declared() -> None:
    fragment = _fragment()
    declared = {(method.upper(), path) for path, item in fragment["paths"].items() for method in item}
    for route in router.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()):
            if path.startswith("/api/v1/"):
                assert (method, path) in ROUTE_SCOPES
                assert (method, path) in declared


def test_scopes_are_catalogued() -> None:
    assert set(ROUTE_SCOPES.values()) <= KNOWN_SCOPES


def test_event_payload_schemas_exist() -> None:
    contract = json.loads((CONTRACT_DIR / "contract.json").read_text(encoding="utf-8"))
    events = contract["contracts"][0]["events"]
    event_names = {event["event"] for event in events}
    assert event_names <= set(schemas.EVENT_MODELS)  # contract subset of implemented events
    assert len(schemas.EVENT_MODELS) == 6  # + additive count.recounted/count.snapshot.recalculated
    for event in events:
        schema = schemas.event_payload_schema(event["event"])
        assert "properties" in schema
        on_disk = json.loads((CONTRACT_DIR / "events" / f"{event['event']}.schema.json").read_text(encoding="utf-8"))
        assert on_disk == schema


def test_counts_port_surface_is_importable_without_db() -> None:
    method_names = {
        "generate_from_plan", "create_count", "start_count", "capture_lines", "submit",
        "approve", "reject", "post_variance", "recount", "get_next_count_date",
    }
    assert method_names <= set(dir(CountsPort))
    assert schemas.count_ref_schema()["title"]
