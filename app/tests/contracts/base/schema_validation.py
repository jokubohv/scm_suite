"""Dependency-free structural validators for the committed contract artefacts.

Full JSON-Schema validation of *implemented* payloads belongs to FS provider tests (they may use
``jsonschema``). At S0 all payloads are TODO stubs, so validating the fixed envelope + OpenAPI
structure is the useful, deterministic gate.
"""

from __future__ import annotations

from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
ENVELOPE_REQUIRED = {"event_id", "event_type", "event_version", "tenant_id", "occurred_at", "aggregate", "data"}


def assert_openapi_document(document: dict[str, Any]) -> None:
    assert document.get("openapi") == "3.1.0", "OpenAPI version must be 3.1.0"
    info = document.get("info") or {}
    assert info.get("title"), "info.title required"
    assert info.get("version"), "info.version required"
    assert isinstance(document.get("paths"), dict) and document["paths"], "document must declare paths"
    components = document.get("components") or {}
    assert "Problem" in (components.get("schemas") or {}), "components.schemas.Problem required"


def assert_operation_shape(raw_path: str, method: str, operation: dict[str, Any]) -> None:
    assert method in HTTP_METHODS, f"invalid method {method} for {raw_path}"
    assert raw_path.startswith("/"), f"path must be absolute: {raw_path}"
    assert operation.get("operationId"), f"missing operationId: {method} {raw_path}"
    responses = operation.get("responses") or {}
    assert any(code in responses for code in ("200", "201", "204")), f"missing success response: {method} {raw_path}"
    has_scope = bool(operation.get("x-required-scopes"))
    has_allowlist = bool(operation.get("x-allowlist"))
    assert has_scope or has_allowlist, f"missing capability scope or allowlist: {method} {raw_path}"
    if operation.get("x-skeleton"):
        assert operation.get("x-todo"), f"skeleton operation must be marked x-todo: {method} {raw_path}"


def assert_event_schema(schema: dict[str, Any]) -> None:
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.get("type") == "object"
    assert ENVELOPE_REQUIRED <= set(schema.get("required") or []), "event envelope required fields missing"
    properties = schema.get("properties") or {}
    assert "data" in properties, "event schema must declare data"
    for field in ("x-event-type", "x-event-version", "x-contract", "x-feature-set"):
        assert schema.get(field), f"event schema missing {field}"
