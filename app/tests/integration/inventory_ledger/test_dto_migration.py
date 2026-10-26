"""Native-boundary DTO migration guard for the inventory-ledger router.

Every mounted ledger route must declare a Pydantic ``response_model`` (so OpenAPI responses are
component ``$ref``s rather than injected inline objects) and every body-bearing operation must use a
typed ``app.api.schemas.inventory`` request model.
"""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

from app.api.main import app
from app.api.routers.inventory_ledger.router import router

METHODS = ("get", "post", "put", "patch", "delete")


def _apis() -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_every_ledger_route_has_native_response_model() -> None:
    routes = _apis()
    assert len(routes) == 74
    missing = [f"{sorted(route.methods)} {route.path}" for route in routes if route.response_model is None]
    assert missing == []
    for route in routes:
        model = route.response_model
        assert getattr(model, "__module__", "").startswith("app.api.schemas") or model.__module__ == "app.api.schemas.common", (
            route.path,
            model,
        )


def test_every_body_operation_uses_inventory_schema_models() -> None:
    body_routes = []
    for route in _apis():
        for method in route.methods:
            if method not in {"POST", "PUT", "PATCH"} or route.body_field is None:
                continue
            body_routes.append((method, route.path, route.body_field.field_info.annotation))
    assert len(body_routes) == 34
    untyped = []
    for method, path, annotation in body_routes:
        candidates = getattr(annotation, "__args__", (annotation,))
        modules = {getattr(candidate, "__module__", "") for candidate in candidates if candidate is not None}
        if "app.api.schemas.inventory" not in modules:
            untyped.append((method, path, annotation))
    assert untyped == []


def test_ledger_openapi_responses_are_component_refs_and_bodies_typed() -> None:
    spec = app.openapi()
    ledger_paths = {route.path for route in _apis()}
    checked = 0
    opaque_bodies = []
    for path, item in spec["paths"].items():
        if path not in ledger_paths:
            continue
        for method, operation in item.items():
            if method not in METHODS:
                continue
            checked += 1
            responses = {
                code: response
                for code, response in operation.get("responses", {}).items()
                if code.startswith("2")
            }
            assert responses, (method, path)
            for response in responses.values():
                schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
                assert "$ref" in schema or "allOf" in schema or "anyOf" in schema, (method, path, schema)
            if method in {"post", "put", "patch"} and "requestBody" in operation:
                schema = ((operation["requestBody"].get("content") or {}).get("application/json") or {}).get("schema") or {}
                if "$ref" not in schema and "anyOf" not in schema and not schema.get("properties"):
                    opaque_bodies.append((method, path, schema))
    assert checked >= 74
    assert opaque_bodies == []


def test_migrated_response_components_are_present() -> None:
    schemas: dict[str, Any] = app.openapi().get("components", {}).get("schemas", {})
    for name in (
        "TransactionOut",
        "TransactionPage",
        "WorksheetOut",
        "TransferOrderOut",
        "CommitmentOut",
        "AvailabilityOut",
        "DistributionOut",
        "ImportBatchOut",
        "ReasonCodeOut",
    ):
        assert name in schemas, name
