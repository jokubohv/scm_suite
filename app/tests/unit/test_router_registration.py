"""FS-00 routers are registered and protected by the auth/scope guard."""

from fastapi.testclient import TestClient

from app.api.main import create_app

PROTECTED_PATHS = [
    "/api/v1/me",
    "/api/v1/me/tenants",
    "/api/v1/me/effective-permissions",
    "/api/v1/admin/permissions",
    "/api/v1/admin/roles",
    "/api/v1/admin/users",
    "/api/v1/admin/audit-events",
    "/api/v1/admin/approval-definitions",
    "/api/v1/approvals",
    "/api/v1/admin/sso-connections",
]


def test_protected_routes_require_auth() -> None:
    client = TestClient(create_app())
    for path in PROTECTED_PATHS:
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text)
        body = response.json()
        assert body["code"] == "TOKEN_INVALID"
        assert body["correlation_id"]


def test_unknown_route_is_problem_json() -> None:
    response = TestClient(create_app()).get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "HTTP_ERROR"
