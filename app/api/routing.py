"""Route-level OpenAPI metadata for the API surface.

Per-operation security is attached at route construction (``openapi_extra``), not by rewriting the
generated document. Public/auth endpoints get explicit empty or bearer-only requirements.
"""
from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute

from app.core.security.scopes import ScopeRegistry

_REGISTRY = ScopeRegistry()

PUBLIC_PATHS = frozenset({
    "/healthz", "/readyz", "/api/v1/auth/login", "/api/v1/auth/select-tenant",
    "/api/v1/auth/providers", "/api/v1/auth/sso/start", "/api/v1/auth/sso/callback",
})
PUBLIC_PREFIXES = ("/api/v1/admin/invitations/",)
DEFAULT_SECURITY = [{"BearerAuth": []}, {"ApiKeyAuth": []}]


def security_for(method: str, path: str) -> list[dict[str, list[str]]]:
    if path.startswith("/api/v1/bootstrap"):
        return [{"SuperAdminToken": []}]
    if path.startswith("/api/v1/mcp") or path == "/api/v1/openapi.json":
        return [{"ApiKeyAuth": []}]
    if path in PUBLIC_PATHS or _REGISTRY.is_public_route(method, path) or (
        path.startswith(PUBLIC_PREFIXES) and path.endswith("/accept")
    ):
        return []
    if (
        _REGISTRY.is_self_service_route(method, path)
        or path.startswith("/api/v1/auth/")
        or path.startswith("/api/v1/me")
        or path.startswith("/api/v1/platform/")
    ):
        return [{"BearerAuth": []}]
    return DEFAULT_SECURITY


class SCMAPIRoute(APIRoute):
    def __init__(self, path: str, endpoint: Any, **kwargs: Any) -> None:
        super().__init__(path, endpoint, **kwargs)
        methods = sorted(self.methods or {"GET"})
        extra = dict(self.openapi_extra or {})
        extra.setdefault("security", security_for(methods[0], self.path))
        self.openapi_extra = extra
