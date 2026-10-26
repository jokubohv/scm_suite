"""Global OpenAPI security definitions.

Per-operation security requirements are attached natively by ``app.api.routing.SCMAPIRoute``.
FastAPI generates security schemes only from visible ``Security`` dependencies; because this API
uses alternative JWT/API-key requirements, the three scheme definitions and the default global
requirement are assembled here once.
"""
from __future__ import annotations

from typing import Any

SECURITY_SCHEMES = {
    "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
    "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
    "SuperAdminToken": {"type": "apiKey", "in": "header", "name": "X-Super-Admin-Token"},
}


def apply_security_schemes(spec: dict[str, Any]) -> None:
    components = spec.setdefault("components", {})
    components["securitySchemes"] = SECURITY_SCHEMES
    spec["security"] = [{"BearerAuth": []}, {"ApiKeyAuth": []}]
