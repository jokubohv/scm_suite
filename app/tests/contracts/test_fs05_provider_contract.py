"""FS-05 `costing.engine@1.0.0` provider contract (implementation-backed builder artefacts)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.api.routers.costing import ROUTE_SCOPES, router
from app.contracts.costing import CONTRACT_VERSION, CostingPort, schemas
from app.modules.costing.engine import CostingSettings, CostObject, ItemCostState, ValueMovement

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "costing"
FRAGMENT = CONTRACT_DIR / "openapi.fragment.json"
KNOWN_SCOPES = frozenset(Route if isinstance(Route, str) else "" for Route in []) | frozenset(
    {
        "costing.cost.read",
        "costing.cost.manage",
        "costing.cost.view",
        "costing.recalc.run",
        "costing.recalculate",
        "costing.standard.activate",
        "costing.standard.rollup",
        "costing.rollup",
        "costing.revaluation.create",
        "costing.revaluation.approve",
        "costing.revaluation.post",
        "costing.landed_cost.create",
        "costing.landed_cost.post",
        "costing.estimated_landed_cost.manage",
        "costing.cogs_adjustment.create",
        "costing.cogs_adjustment.post",
        "costing.variance.view",
        "costing.group.manage",
        "costing.method.manage",
        "costing.landedcost.manage",
        "costing.read",
    }
)


def _fragment() -> dict:
    return json.loads(FRAGMENT.read_text(encoding="utf-8"))


def test_contract_version() -> None:
    assert CONTRACT_VERSION == "1.0.0"


def test_fragment_is_implementation_backed_and_has_exactly_one_scope() -> None:
    fragment = _fragment()
    assert fragment["info"]["version"]
    assert fragment["paths"], "builder fragment must expose implemented operations"
    seen: set[str] = set()
    for path, operations in fragment["paths"].items():
        assert path.startswith("/api/v1/")
        for method, operation in operations.items():
            key = (method.upper(), path)
            assert key in ROUTE_SCOPES, f"fragment route {key} has no declared scope"
            assert operation["x-implemented"] is True
            assert operation["x-skeleton"] is False
            assert operation["x-feature-set"] == "FS-05"
            assert operation["x-contract"] == ["costing.engine"]
            assert operation["x-required-scopes"] == [ROUTE_SCOPES[key]]
            assert operation["operationId"] not in seen
            seen.add(operation["operationId"])
            if method in {"post", "patch", "put", "delete"}:
                names = {parameter["name"] for parameter in operation.get("parameters", [])}
                assert "Idempotency-Key" in names


def test_every_router_operation_has_a_scope_and_is_in_the_fragment() -> None:
    fragment = _fragment()
    declared = {(method.upper(), path) for path, item in fragment["paths"].items() for method in item}
    for route in router.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()):
            if path.startswith("/api/v1/"):
                assert (method, path) in ROUTE_SCOPES, f"{method} {path} missing scope"
                assert (method, path) in declared, f"{method} {path} missing from fragment"


def test_scopes_are_catalogued() -> None:
    assert set(ROUTE_SCOPES.values()) <= KNOWN_SCOPES


def test_value_result_schema_round_trips_port_output() -> None:
    port = CostingPort()
    result = port.value_inbound(
        ValueMovement(
            item_id="item-1", qty_delta=Decimal("2"), movement_type="IN",
            transaction_date=date(2026, 1, 2), unit_cost=Decimal("3.5"),
        ),
        CostObject(item_id="item-1", costing_method="AVERAGE", location_id="loc-1"),
        None,
        [],
        CostingSettings(),
    )
    payload = schemas.ValueResultPayload.model_validate(result.as_dict())
    assert payload.value_delta == "7.000000000000"
    assert payload.new_state is not None
    assert payload.new_state.qty_on_hand == "2.000000000000"
    assert schemas.value_result_schema()["title"] == "ValueResultPayload"
    on_disk = json.loads((CONTRACT_DIR / "value-result.schema.json").read_text(encoding="utf-8"))
    assert on_disk == schemas.value_result_schema()


def test_value_movement_schema_and_cost_object_artefacts_on_disk() -> None:
    for name, schema in (
        ("value-movement.schema.json", schemas.value_movement_schema()),
        ("cost-object.schema.json", schemas.cost_object_schema()),
    ):
        assert json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8")) == schema


def test_event_payload_schemas_exist_and_validate() -> None:
    contract = json.loads((CONTRACT_DIR / "contract.json").read_text(encoding="utf-8"))
    events = contract["contracts"][0]["events"]
    assert len(events) == 15
    for event in events:
        schema = schemas.event_payload_schema(event["event"])
        assert "properties" in schema
        on_disk = json.loads((CONTRACT_DIR / "events" / f"{event['event']}.schema.json").read_text(encoding="utf-8"))
        assert on_disk == schema


def test_costing_port_is_importable_without_database() -> None:
    # FS-04 imports this facade inside its posting transaction; it must not open I/O at import time.
    assert not hasattr(CostingPort(), "session")
    port = CostingPort()
    port.assert_postable(
        CostObject(item_id="i", costing_method="FIFO", location_id="l"),
        ItemCostState(),
        CostingSettings(),
    )
