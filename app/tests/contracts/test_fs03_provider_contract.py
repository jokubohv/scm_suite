"""FS-03 provider contract tests (`traceability.registry@1.0.0`).

Validates the implementation-backed OpenAPI fragment, the in-process resolver contract surface used by
FS-04/FS-05/FS-10/FS-11, the canonical location/bootstrap JSON Schemas and the required traceability event
payload schemas. These are provider-side guarantees: consumers can pin the fragment and call the resolver
methods without reaching into traceability internals.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.api.routers.traceability import ROUTE_SCOPES
from app.contracts.traceability.schemas import (
    EVENT_MODELS,
    REQUIRED_EVENT_MODELS,
    RESOLVER_METHODS,
    BootstrapState,
    Location,
    PostingEligibility,
    StatusFlags,
    bootstrap_schema,
    event_schemas,
    location_schema,
)
from app.modules.traceability.resolver import LocationResolver

HERE = Path(__file__).resolve().parents[3]
CONTRACT_DIR = HERE / "app" / "contracts" / "traceability"


def _fragment() -> dict:
    return json.loads((CONTRACT_DIR / "openapi.fragment.json").read_text(encoding="utf-8"))


def test_builder_fragment_covers_all_implemented_routes() -> None:
    fragment = _fragment()
    operations = {
        (method.upper(), path)
        for path, item in fragment["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert operations == set(ROUTE_SCOPES)
    assert fragment["info"]["x-endpoint-count"] == 29
    assert fragment["info"]["x-feature-set"] == "FS-03"
    assert fragment["info"]["x-contract"] == ["traceability.registry@1.0.0"]
    assert all(operation.get("x-implemented") for item in fragment["paths"].values() for operation in item.values())


def test_parameterised_paths_use_frozen_id_placeholder() -> None:
    fragment = _fragment()
    for path, item in fragment["paths"].items():
        placeholders = [
            segment.strip("{}") for segment in path.split("/") if segment.startswith("{") and segment.endswith("}")
        ]
        if placeholders:
            assert placeholders == ["id"], path
            for method in item:
                if method in {"get", "post", "put", "patch", "delete"}:
                    assert path in {
                        operation_path
                        for operation_path, operations in fragment["paths"].items()
                        if method in operations
                    }


def test_every_fragment_scope_is_catalogued_and_expected() -> None:
    catalogue = json.loads((HERE / "design" / "permissions.json").read_text(encoding="utf-8"))
    known = {row["scope"] for row in catalogue["catalogue"]}
    fragment = _fragment()
    scopes = {operation["x-required-scopes"][0] for item in fragment["paths"].values() for operation in item.values()}
    assert scopes <= known
    assert scopes == {
        "inventory.location.read",
        "inventory.location.manage",
        "inventory.bin.read",
        "inventory.bin.manage",
        "inventory.status.read",
        "inventory.status.manage",
    }


def test_resolver_contract_surface_is_callable() -> None:
    for method in RESOLVER_METHODS:
        assert hasattr(LocationResolver, method), f"missing resolver method {method}"
        assert callable(getattr(LocationResolver, method))
    # location resolution/bootstrap signatures accept the documented kwargs
    import inspect

    assert "allow_unallocated" in inspect.signature(LocationResolver.resolve).parameters
    assert "include_descendants" in inspect.signature(LocationResolver.expand_location_scope).parameters
    assert "ownership" in inspect.signature(LocationResolver.resolve_default_status).parameters


def test_required_event_payload_schemas_present() -> None:
    required = {
        "traceability.location.created",
        "traceability.location.updated",
        "traceability.location.inactivated",
        "traceability.bin.changed",
        "traceability.status.changed",
    }
    assert set(REQUIRED_EVENT_MODELS) == required
    assert required <= set(EVENT_MODELS) == set(event_schemas())
    on_disk = {path.name.removesuffix(".schema.json") for path in (CONTRACT_DIR / "events").glob("*.schema.json")}
    assert set(EVENT_MODELS) == on_disk
    assert event_schemas()["traceability.bin.changed"]["required"] == ["bin_id", "change"]


def test_location_and_bootstrap_schemas_on_disk() -> None:
    assert json.loads((CONTRACT_DIR / "location.schema.json").read_text(encoding="utf-8")) == location_schema()
    assert json.loads((CONTRACT_DIR / "bootstrap-state.schema.json").read_text(encoding="utf-8")) == bootstrap_schema()
    location_props = location_schema()["properties"]
    for key in ("code", "location_type", "is_bins_enabled", "is_statuses_enabled", "is_unallocated", "is_inactive"):
        assert key in location_props
    assert "sentinel_present" in bootstrap_schema()["properties"]


def test_consumer_payloads_roundtrip() -> None:
    location = Location.model_validate(
        {
            "id": "loc-1",
            "code": "WH1",
            "name": "Main",
            "location_type": "WAREHOUSE",
            "is_bins_enabled": True,
            "is_statuses_enabled": True,
            "is_consignment_allowed": False,
            "allow_commit_from_non_available": False,
            "is_unallocated": False,
            "is_inactive": False,
            "timezone": "UTC",
            "version": 3,
        }
    )
    status = StatusFlags(
        status_id="st-1",
        code="AVAILABLE",
        available_for_allocation=True,
        committable=True,
        sellable=True,
        consigned=False,
        default=True,
        active=True,
    )
    eligibility = PostingEligibility(
        item_id="item-1",
        bin_id="bin-1",
        location_id="loc-1",
        eligible=True,
        normalized={"inventory_status_id": "st-1"},
        errors=[],
        warnings=[],
        bins_enabled=True,
        statuses_enabled=True,
    )
    boot = BootstrapState(
        tenant_id="tenant-1",
        sentinel_present=True,
        sentinel_location_id="loc-sentinel",
        status_count=7,
        default_status_code="AVAILABLE",
        seeded=True,
    )
    for payload in (location.model_dump(), status.model_dump(), eligibility.model_dump(), boot.model_dump()):
        assert json.loads(json.dumps(payload, sort_keys=True))
