"""Boundary-DTO migration acceptance checks for manufacturing/quality/traceability routers.

Uses only the four migrated routers in a local FastAPI app so it is independent of the global app's
router registration state. Asserts the named OpenAPI components exist and are referenced by migrated
operations; 204 operations intentionally have no response body schema.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.routers.manufacturing import router as manufacturing_router
from app.api.routers.quality import router as quality_router
from app.api.routers.traceability import router as traceability_router
from app.api.routers.traceability_numbered import router as numbered_router

RESPONSE_COMPONENTS = {"ManufacturingResponse", "QualityResponse", "TraceabilityResponse"}
REQUEST_COMPONENTS = {"QualityRequest", "TraceabilityRequest"}


def _app():
    app = FastAPI()
    for router in (manufacturing_router, quality_router, traceability_router, numbered_router):
        app.include_router(router)
    return app


def test_named_response_components_and_migrated_operations() -> None:
    app = _app()
    openapi = app.openapi()
    components = openapi["components"]["schemas"]
    assert RESPONSE_COMPONENTS <= set(components)
    assert REQUEST_COMPONENTS <= set(components)

    unmigrated: list[str] = []
    for router in (manufacturing_router, quality_router, traceability_router, numbered_router):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or set():
                if method.upper() in {"HEAD", "OPTIONS"}:
                    continue
                operation = openapi["paths"][route.path][method.lower()]
                response_schema = {}
                for status in ("200", "201", "202"):
                    content = (operation.get("responses", {}).get(status) or {}).get("content", {})
                    response_schema = content.get("application/json", {}).get("schema") or {}
                    if response_schema:
                        break
                if int(route.status_code or 200) == 204:
                    assert not response_schema
                    continue
                ref = str(response_schema.get("$ref") or "").rsplit("/", 1)[-1]
                if ref not in RESPONSE_COMPONENTS:
                    unmigrated.append(f"{method} {route.path} -> {ref!r}")
    assert unmigrated == [], unmigrated


def test_dict_request_bodies_are_named_components() -> None:
    openapi = _app().openapi()
    for path in ("/api/v1/quality/specs", "/api/v1/quality/inspections"):
        schema = openapi["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/QualityRequest")
    schema = openapi["paths"]["/api/v1/locations"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/TraceabilityRequest")
