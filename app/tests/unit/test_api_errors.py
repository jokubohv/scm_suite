"""FS-00 API conventions: problem+json errors, correlation ids, middleware."""

from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from app.api.main import create_app


def _app() -> TestClient:
    app = create_app()
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=409, detail={"code": "TEST_CONFLICT", "message": "conflict"})

    @router.get("/validate")
    async def validate(required: int) -> dict[str, int]:
        return {"required": required}

    app.include_router(router)
    return TestClient(app)


def test_correlation_id_echoed() -> None:
    client = _app()
    response = client.get("/healthz", headers={"X-Request-ID": "test-correlation-01"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-correlation-01"


def test_problem_json_http_error() -> None:
    response = _app().get("/boom")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "TEST_CONFLICT"
    assert body["correlation_id"]


def test_validation_problem() -> None:
    response = _app().get("/validate")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"]
