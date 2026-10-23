"""FS-10 `replenishment.planning@1.0.0` provider contract (implementation-backed builder artefacts)."""

from __future__ import annotations

import json
from pathlib import Path

from app.api.routers.replenishment import ROUTE_SCOPES, router
from app.contracts.replenishment import CONTRACT_VERSION, ReplenishmentPort, schemas

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "replenishment"
KNOWN_SCOPES = {
    "replenishment.read", "replenishment.write", "replenishment.run",
    "replenishment.approve", "replenishment.convert", "replenishment.admin",
}


def _fragment() -> dict:
    return json.loads((CONTRACT_DIR / "openapi.fragment.json").read_text(encoding="utf-8"))


def test_contract_version() -> None:
    assert CONTRACT_VERSION == "1.0.0"


def test_fragment_is_implementation_backed() -> None:
    fragment = _fragment()
    assert fragment["paths"]
    seen: set[str] = set()
    for path, operations in fragment["paths"].items():
        assert path.startswith("/api/v1/")
        for method, operation in operations.items():
            key = (method.upper(), path)
            assert key in ROUTE_SCOPES, f"{key} missing declared scope"
            assert operation["x-implemented"] is True
            assert operation["x-skeleton"] is False
            assert operation["x-feature-set"] == "FS-10"
            assert operation["x-contract"] == ["replenishment.planning"]
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
    assert event_names <= set(schemas.EVENT_MODELS)
    assert len(schemas.EVENT_MODELS) == 15
    for event in events:
        schema = schemas.event_payload_schema(event["event"])
        assert "properties" in schema
        on_disk = json.loads((CONTRACT_DIR / "events" / f"{event['event']}.schema.json").read_text(encoding="utf-8"))
        assert on_disk == schema


def test_port_surface_is_importable_without_db() -> None:
    methods = {
        "planning_preferences", "atp_query", "atp_projection", "list_supply_plans",
        "list_demand_plans", "supply_plan", "supply_plan_lines", "suggestions",
        "action_messages", "order_items", "workbench", "signals",
    }
    assert methods <= set(dir(ReplenishmentPort))
