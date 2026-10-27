"""500 responses must carry a correlation/trace id even from the generic exception handler."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware


def test_internal_error_has_correlation_and_trace() -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req_test_12345678"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["correlation_id"] == "req_test_12345678"
    assert body["trace_id"]
    assert response.headers["X-Request-ID"] == "req_test_12345678"
