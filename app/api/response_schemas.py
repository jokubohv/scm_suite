"""OpenAPI response/error/security metadata.

The FastAPI handlers generally return ``dict[str, Any]``, so OpenAPI has no useful 2xx response
schema unless one is injected. This module supplies shared, typed response envelopes for every
mounted JSON response, plus the problem+json error component and the auth schemes.

It runs after ``request_schemas.apply_request_schemas`` during ``app.openapi()`` generation.
"""
from __future__ import annotations

import re
from typing import Any

from app.api.request_schemas import (
    PHASE2_RULES,
    PHASE3_RULES,
    RULES,
    _normalise_path,
)

UUID = {"type": "string", "format": "uuid"}
DATE = {"type": "string", "format": "date"}
DATETIME = {"type": "string", "format": "date-time"}
TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
DECIMAL = {"type": "string", "description": "Decimal encoded as a JSON string"}

PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}
JSON_PROBLEM = "application/problem+json"
JSON = "application/json"

PUBLIC_PATHS = frozenset({
    "/healthz", "/readyz",
    "/api/v1/auth/login", "/api/v1/auth/select-tenant", "/api/v1/auth/providers",
    "/api/v1/auth/sso/start", "/api/v1/auth/sso/callback",
    "/docs/oauth2-redirect",
})
PUBLIC_PREFIXES = ("/api/v1/admin/invitations/",)


def _obj(properties: dict[str, Any], required: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


def _core_fields() -> dict[str, Any]:
    return {
        "id": UUID,
        "tenant_id": UUID,
        "status": TEXT,
        "created_at": DATETIME,
        "updated_at": DATETIME,
        "version": INT,
    }


def _resource(base: str) -> dict[str, Any]:
    """Build a response object from the documented create/update body for a resource."""
    body: dict[str, Any] = {}
    for rule in (*RULES, *PHASE2_RULES, *PHASE3_RULES):
        if rule.base != base:
            continue
        if rule.create:
            body = dict(rule.create.get("properties") or {})
            break
        if rule.update:
            body = dict(rule.update.get("properties") or {})
            break
    props = {**_core_fields(), **body}
    return _obj(props, required=("id",))


RESOURCE_BY_BASE: dict[str, dict[str, Any]] = {
    rule.base: _resource(rule.base)
    for rule in (*RULES, *PHASE2_RULES, *PHASE3_RULES)
    if rule.create or rule.update
}

LIST_SCHEMA = _obj({
    "items": {"type": "array", "items": {"type": "object"}},
    "count": INT,
    "next_cursor": {"type": ["string", "null"]},
}, required=("items",))
ACTION_SCHEMA = _obj({
    "id": UUID,
    "status": TEXT,
    "message": TEXT,
    "document_number": TEXT,
    "posted_transaction_id": UUID,
    "approval_request_id": UUID,
    "reversal_transaction_id": UUID,
    "transaction_id": UUID,
    "count": INT,
})
PROBLEM_SCHEMA = _obj({
    "type": TEXT,
    "title": TEXT,
    "status": INT,
    "code": TEXT,
    "message": TEXT,
    "details": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "correlation_id": TEXT,
    "trace_id": TEXT,
}, required=("status", "code", "message"))
OPENAPI_SCHEMA = _obj({
    "openapi": TEXT,
    "info": {"type": "object", "additionalProperties": True},
    "paths": {"type": "object", "additionalProperties": True},
    "components": {"type": "object", "additionalProperties": True},
}, required=("openapi", "paths"))
MCP_INFO_SCHEMA = _obj({
    "name": TEXT,
    "version": TEXT,
    "protocolVersion": TEXT,
    "transport": TEXT,
    "endpoint": TEXT,
    "auth": {"type": "object", "additionalProperties": True},
    "capabilities": {"type": "object", "additionalProperties": True},
    "tool_count": INT,
    "tools": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "resources": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "prompts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
})
JSONRPC_SCHEMA = _obj({
    "jsonrpc": {"type": "string", "const": "2.0"},
    "id": {"oneOf": [TEXT, INT, {"type": "null"}]},
    "result": {"type": "object", "additionalProperties": True},
    "error": {"type": "object", "additionalProperties": True},
})
HEALTH_SCHEMA = _obj({"status": TEXT, "version": TEXT, "env": TEXT}, required=("status",))
READY_SCHEMA = _obj({"status": TEXT, "checks": {"type": "object", "additionalProperties": True}},
                    required=("status", "checks"))
REPORT_SCHEMA = _obj({
    "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "count": INT,
    "as_of": DATETIME,
    "generated_at": DATETIME,
    "next_cursor": {"type": ["string", "null"]},
}, required=("items",))

# Explicit response schemas for endpoints with a known special shape.
SPECIAL_RESPONSES: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/healthz"): HEALTH_SCHEMA,
    ("GET", "/readyz"): READY_SCHEMA,
    ("GET", "/api/v1/openapi.json"): OPENAPI_SCHEMA,
    ("GET", "/api/v1/mcp"): MCP_INFO_SCHEMA,
    ("POST", "/api/v1/mcp"): JSONRPC_SCHEMA,
}


def _is_typed(schema: Any) -> bool:
    if not isinstance(schema, dict) or not schema:
        return False
    if schema.get("$ref"):
        return True
    if schema.get("type") == "object" or "properties" in schema:
        return bool(schema.get("properties")) or schema.get("additionalProperties") is False
    if schema.get("type") == "array":
        return _is_typed(schema.get("items"))
    return bool(schema.get("type") is not None and schema.get("type") != "object")


def _base_for(path: str) -> str | None:
    normalized = _normalise_path(path)
    candidates = [base for base in RESOURCE_BY_BASE if normalized == base or normalized.startswith(base + "/")]
    return max(candidates, key=len) if candidates else None


def _has_trailing_id(path: str) -> bool:
    return bool(re.search(r"/\{[^}]+\}$", _normalise_path(path)))


def _is_action(path: str) -> bool:
    normalized = _normalise_path(path)
    return bool(re.fullmatch(r".*/\{\}/[A-Za-z0-9_.-]+", normalized))


def _response_schema(method: str, path: str, status: str) -> dict[str, Any] | None:
    key = (method.upper(), _normalise_path(path))
    special = SPECIAL_RESPONSES.get(key)
    if special is not None:
        return special
    if status == "204":
        return None
    if path.startswith("/api/v1/reports"):
        return REPORT_SCHEMA
    if path.startswith("/api/v1/bootstrap/whoami") or path.startswith("/api/v1/auth") or path.startswith("/api/v1/me"):
        return ACTION_SCHEMA
    base = _base_for(path)
    if base is None:
        return ACTION_SCHEMA
    resource = RESOURCE_BY_BASE[base]
    if method.upper() == "GET":
        if _is_action(path):
            return ACTION_SCHEMA
        if _has_trailing_id(path):
            return resource
        return _obj({"items": {"type": "array", "items": resource}, "count": INT,
                     "next_cursor": {"type": ["string", "null"]}}, required=("items",))
    if _is_action(path):
        return ACTION_SCHEMA
    return resource


def _security_for(method: str, path: str) -> list[dict[str, list[str]]] | None:
    if path == "/api/v1/openapi.json":
        return [{"ApiKeyAuth": []}]
    if path in PUBLIC_PATHS or (path.startswith(PUBLIC_PREFIXES) and path.endswith("/accept")):
        return []
    if path.startswith("/api/v1/bootstrap"):
        return [{"SuperAdminToken": []}]
    if path.startswith("/api/v1/mcp"):
        return [{"ApiKeyAuth": []}]
    return None


def apply_contract_metadata(spec: dict[str, Any]) -> dict[str, int]:
    """Inject response schemas, problem+json errors and security schemes in place."""
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.setdefault("Problem", PROBLEM_SCHEMA)
    components["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        "SuperAdminToken": {"type": "apiKey", "in": "header", "name": "X-Super-Admin-Token"},
    }
    spec["security"] = [{"BearerAuth": []}, {"ApiKeyAuth": []}]

    stats = {"responses": 0, "errors": 0, "security": 0}
    error_codes = ("400", "401", "403", "404", "409", "422", "500")
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            security = _security_for(method, path)
            if security is not None:
                operation["security"] = security
                stats["security"] += 1
            responses = operation.setdefault("responses", {})
            for code, response in responses.items():
                if not code.startswith("2"):
                    continue
                content = response.get("content") or {}
                json_content = content.get(JSON) or {}
                if not json_content:
                    continue
                existing = json_content.get("schema")
                if _is_typed(existing):
                    continue
                generated = _response_schema(method, path, code)
                if generated is None:
                    continue
                json_content["schema"] = generated
                if JSON not in content:
                    content[JSON] = json_content
                response["content"] = content
                stats["responses"] += 1
            for code in error_codes:
                if code not in responses:
                    responses[code] = {"description": f"Error {code}"}
                response = responses[code]
                content = response.setdefault("content", {})
                content[JSON_PROBLEM] = {"schema": PROBLEM_REF}
                stats["errors"] += 1
    return stats


__all__ = ["apply_contract_metadata"]
