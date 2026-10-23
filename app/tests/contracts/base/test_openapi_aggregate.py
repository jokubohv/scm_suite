"""Aggregated OpenAPI skeleton tests + builder-extension harness proof (S0-04)."""

from __future__ import annotations

from app.contracts import paths

from . import fixtures
from .schema_validation import assert_openapi_document, assert_operation_shape


def test_aggregate_is_valid_openapi_31_and_has_problem_schema() -> None:
    document = fixtures.committed_openapi()
    assert_openapi_document(document)
    assert document["info"]["x-status"] == "skeleton"
    assert document["info"]["x-plan-version"] == fixtures.manifest()["plan_version"]


def test_aggregate_operations_shape_unique_and_scoped() -> None:
    document = fixtures.committed_openapi()
    operation_ids: set[str] = set()
    for raw_path, item in document["paths"].items():
        for method, operation in item.items():
            assert_operation_shape(raw_path, method, operation)
            operation_id = operation["operationId"]
            assert operation_id not in operation_ids, f"duplicate operationId: {operation_id}"
            operation_ids.add(operation_id)
    assert len(operation_ids) == document["x-counts"]["operations"]


def test_no_legacy_v1_base_paths() -> None:
    document = fixtures.committed_openapi()
    legacy = [path for path in document["paths"] if path.startswith("/v1/") or path == "/v1"]
    assert legacy == [], f"legacy /v1 paths present: {legacy[:5]}"


def test_every_feature_set_contributes_operations() -> None:
    document = fixtures.committed_openapi()
    assert len(document["x-feature-sets"]) == 16
    for entry in document["x-feature-sets"]:
        assert entry["endpoint_count"] > 0, entry["id"]
        assert entry["contracts"], entry["id"]
        assert (paths.REPO_ROOT / entry["fragment"]).exists(), entry["fragment"]
    assert len({c["contract"] for c in document["x-contracts"]}) == 21


def test_builder_extension_fragment_is_merged(monkeypatch, tmp_path) -> None:
    fragment = {
        "openapi": "3.1.0",
        "info": {"title": "test fragment", "version": "1.5.0"},
        "paths": {
            "/api/v1/test-extension": {
                "get": {
                    "operationId": "testExtensionGet",
                    "summary": "extension probe",
                    "responses": {"200": {"description": "ok"}},
                    "x-required-scopes": ["items.read"],
                    "x-skeleton": False,
                }
            }
        },
    }
    aggregate = fixtures.aggregate_with_builder_fragment(monkeypatch, tmp_path, fragment)
    assert "/api/v1/test-extension" in aggregate["paths"]
    assert aggregate["paths"]["/api/v1/test-extension"]["get"]["operationId"] == "testExtensionGet"
