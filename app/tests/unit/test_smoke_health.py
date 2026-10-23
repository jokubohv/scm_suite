"""S0-01 smoke tests: app factory and platform endpoints import and respond."""

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_healthz() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"


def test_readyz_and_internal_deps() -> None:
    with TestClient(create_app()) as client:
        ready = client.get("/readyz")
        deps = client.get("/internal/deps")
    assert ready.status_code == 200
    assert "checks" in ready.json()
    # /internal/deps is a catalogued scoped operation (platform.internal); deny-by-default applies.
    assert deps.status_code == 401
    assert deps.headers.get("content-type", "").startswith("application/problem+json")
