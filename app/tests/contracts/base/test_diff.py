"""Additive-only contract diff tests (framework §5 L1 compatibility gate)."""

from __future__ import annotations

import json

from app.contracts.diff import compare_versions, diff_event_schema, diff_openapi, is_major_bump


def _openapi(paths_: dict, schemas: dict | None = None) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1.0.0"},
        "paths": paths_,
        "components": {"schemas": schemas or {}},
    }


def _op(operation_id: str, scope: str = "items.read") -> dict:
    return {
        "operationId": operation_id,
        "responses": {"200": {"description": "ok"}},
        "x-required-scopes": [scope],
    }


def test_additive_openapi_change_is_not_breaking() -> None:
    base = _openapi({"/api/v1/a": {"get": _op("aGet")}})
    current = _openapi({"/api/v1/a": {"get": _op("aGet"), "post": _op("aPost")}, "/api/v1/b": {"get": _op("bGet")}})
    result = diff_openapi(base, current)
    assert result["breaking_count"] == 0
    assert result["additive_count"] >= 2


def test_removed_path_or_operation_is_breaking() -> None:
    base = _openapi({"/api/v1/a": {"get": _op("aGet"), "post": _op("aPost")}})
    current = _openapi({"/api/v1/a": {"get": _op("aGet")}})
    result = diff_openapi(base, current)
    assert result["breaking_count"] == 1
    assert "removed operation" in result["breaking"][0]


def test_operation_id_and_scope_removal_is_breaking() -> None:
    base = _openapi({"/api/v1/a": {"get": _op("aGet", "items.read")}})
    current = _openapi({"/api/v1/a": {"get": _op("aGetRenamed")}})
    result = diff_openapi(base, current)
    assert result["breaking_count"] >= 1


def test_event_required_field_removal_is_breaking_but_addition_is_additive() -> None:
    base = {
        "type": "object",
        "required": ["event_id", "event_version"],
        "properties": {"event_id": {"type": "string"}, "event_version": {"type": "string"}},
        "additionalProperties": False,
        "x-event-version": "1.0.0",
    }
    additive = json.loads(json.dumps(base))
    additive["properties"]["extra"] = {"type": "object"}
    additive["x-event-version"] = "1.1.0"
    assert diff_event_schema(base, additive)["breaking_count"] == 0
    breaking = json.loads(json.dumps(additive))
    breaking["required"] = ["event_id"]
    breaking["x-event-version"] = "1.1.0"
    assert diff_event_schema(additive, breaking)["breaking_count"] >= 1


def test_semver_helpers() -> None:
    assert compare_versions("1.0.0", "1.1.0") == -1
    assert is_major_bump("1.4.0", "2.0.0") is True
    assert is_major_bump("1.4.0", "1.5.0") is False
