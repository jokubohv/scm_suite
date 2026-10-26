"""MCP gateway: API-key-only auth, protocol surface and curated tool catalogue."""

from __future__ import annotations

import re

import httpx
import pytest

from app.api.deps import AuthContext, require_api_key_auth
from app.api.main import create_app
from app.api.routers.platform import mcp as mcp_router

pytestmark = pytest.mark.asyncio

EXCLUDED = ("/api/v1/auth", "/api/v1/admin", "/api/v1/me", "/api/v1/platform", "/api/v1/internal", "/api/v1/bootstrap")


def _fake_auth() -> AuthContext:
    return AuthContext(
        kind="api_key",
        identity_id="00000000-0000-0000-0000-000000000001",
        app_user_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000003",
        token_version=0,
        scopes=frozenset({"items.read"}),
    )


def _app():
    app = create_app()
    app.dependency_overrides[require_api_key_auth] = _fake_auth
    return app


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_tool_registry_is_derived_from_runtime_openapi() -> None:
    app = create_app()
    spec = app.openapi()
    tools = mcp_router.build_tool_registry(spec)
    names = [tool.name for tool in tools]
    assert len(tools) >= 200
    assert len(names) == len(set(names))
    assert "scm_api_request" in names
    assert any(name.startswith("inventory_adjustments_") for name in names)
    assert any(name.startswith("purchase_orders_") for name in names)
    assert any(name.startswith("work_orders_") for name in names)
    opaque_body_tools = [tool.name for tool in tools
                         if tool.has_body and tool.body_additional and not tool.body_properties]
    assert not opaque_body_tools, opaque_body_tools
    for tool in tools:
        if tool.name == "scm_api_request":
            continue
        assert tool.path.startswith("/api/v1/")
        assert not any(tool.path == prefix or tool.path.startswith(prefix + "/") for prefix in EXCLUDED)
        assert tool.method.lower() in spec["paths"][tool.path]
        assert tool.input_schema.get("type") == "object"
        # Path parameters must be required in the generated schema.
        for parameter in tool.path_params:
            assert parameter in tool.input_schema.get("properties", {})
            assert parameter in tool.input_schema.get("required", [])
        assert re.match(r"^[a-z0-9_]+$", tool.name)


async def test_api_request_schemas_are_typed_and_injected() -> None:
    spec = create_app().openapi()

    def schemas_is_opaque(schema: dict) -> bool:
        return schema.get("type") == "object" and not (schema.get("properties") or {})

    opaque = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in {"post", "put", "patch"}:
                continue
            request_body = operation.get("requestBody")
            if not request_body:
                continue
            schema = (request_body.get("content") or {}).get("application/json", {}).get("schema") or {}
            properties = schema.get("properties") or {}
            if schema.get("additionalProperties") is True or (schema.get("type") == "object" and not properties):
                opaque.append(f"{method.upper()} {path}")
    assert not opaque, opaque
    receipt = spec["paths"]["/api/v1/item-receipts"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"vendor_id", "lines", "receipt_type", "location_id"} <= set(receipt["properties"])
    assert "lines" in receipt["required"]
    assert "STANDALONE" in receipt["properties"]["receipt_type"]["enum"]
    adjustment = spec["paths"]["/api/v1/inventory-adjustments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "lines" in adjustment["properties"] and "transaction_date" in adjustment["required"]

    from app.api.main import create_app as _create

    tools = mcp_router.build_tool_registry(_create().openapi())
    by_name = {tool.name: tool for tool in tools}
    receipt_tool = by_name["item_receipts_create"]
    assert "vendor_id" in receipt_tool.input_schema["properties"]
    assert "lines" in receipt_tool.input_schema["properties"]
    assert "STANDALONE" in receipt_tool.input_schema["properties"]["receipt_type"]["enum"]
    assert receipt_tool.body_additional is False
    count_approve = by_name["inventory_counts_approve"]
    assert "comment" in count_approve.input_schema["properties"]
    assert "body" not in count_approve.input_schema["properties"]
    count_capture = by_name["inventory_counts_lines"]
    assert "lines" in count_capture.input_schema["properties"]
    count_capture_line = count_capture.input_schema["properties"]["lines"]["items"]
    if "$ref" in count_capture_line:
        count_capture_line = spec["components"]["schemas"][count_capture_line["$ref"].rsplit("/", 1)[-1]]
    assert "line_id" in (count_capture_line.get("properties") or {})
    commitment_release = by_name["inventory_commitments_release"]
    assert "qty" in commitment_release.input_schema["properties"]


async def test_mcp_info_and_tools_list_require_api_key_dependency() -> None:
    app = _app()
    async with await _client(app) as client:
        info = await client.get("/api/v1/mcp")
        assert info.status_code == 200
        body = info.json()
        assert body["auth"] == {"header": "X-API-Key", "jwt_allowed": False}
        assert body["tool_count"] >= 200
        assert body["tools"]
        tools = await client.post(
            "/api/v1/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert tools.status_code == 200
        payload = tools.json()
        assert payload["result"]["tools"]
        assert payload["result"]["tools"][0]["inputSchema"]["type"] == "object"


async def test_mcp_protocol_initialize_resources_prompts_and_notifications() -> None:
    app = _app()
    async with await _client(app) as client:
        initialized = await client.post(
            "/api/v1/mcp",
            json={"jsonrpc": "2.0", "id": "init", "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test"}}},
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "scm-inventory-mcp"
        assert initialized.json()["result"]["capabilities"]["tools"]

        resources = await client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}})
        assert resources.status_code == 200
        uris = [row["uri"] for row in resources.json()["result"]["resources"]]
        assert "scm://openapi" in uris

        read = await client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "scm://mcp/tools"}})
        assert read.status_code == 200
        assert read.json()["result"]["contents"][0]["mimeType"] == "application/json"

        prompts = await client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}})
        assert prompts.status_code == 200
        names = [row["name"] for row in prompts.json()["result"]["prompts"]]
        assert "inventory_adjustment" in names
        assert "procure_to_pay" in names

        notification = await client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert notification.status_code == 202

        unknown = await client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "not/a/method", "params": {}})
        assert unknown.status_code == 200
        assert unknown.json()["error"]["code"] == -32601


async def test_generic_tool_blocks_auth_and_admin_paths() -> None:
    app = _app()
    async with await _client(app) as client:
        blocked = await client.post(
            "/api/v1/mcp",
            json={"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "scm_api_request", "arguments": {"method": "GET", "path": "/api/v1/auth/login"}}},
        )
        assert blocked.status_code == 200
        result = blocked.json()["result"]
        assert result["isError"] is True
        assert "TOOL_ARGUMENT_ERROR" in result["content"][0]["text"]


async def test_mcp_rejects_jwt_bearer() -> None:
    app = create_app()  # no dependency override
    async with await _client(app) as client:
        missing = await client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        assert missing.status_code == 401
        jwt = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": "Bearer not-a-valid-jwt"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        assert jwt.status_code == 401


async def test_openapi_contract_is_complete() -> None:
    from app.api.main import create_app as _create

    spec = _create().openapi()

    def deref(schema):
        seen = set()
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                break
            seen.add(ref)
            node = spec
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            schema = node
        return schema

    def typed(schema) -> bool:
        schema = deref(schema)
        if not isinstance(schema, dict) or not schema:
            return False
        if schema.get("type") == "object" or "properties" in schema:
            return bool(schema.get("properties")) or schema.get("additionalProperties") is False
        if schema.get("type") == "array":
            return typed(schema.get("items") or {})
        return bool(schema.get("type") or schema.get("anyOf") or schema.get("oneOf"))

    opaque_requests = []
    opaque_2xx = []
    missing_problem = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            request_body = operation.get("requestBody")
            if request_body:
                schema = deref((request_body.get("content") or {}).get("application/json", {}).get("schema") or {})
                props = schema.get("properties") or {}
                if schema.get("type") == "object" and not props:
                    opaque_requests.append(f"{method.upper()} {path}")
            responses = operation.get("responses") or {}
            for code, response in responses.items():
                if code.startswith("2"):
                    for media, content in (response.get("content") or {}).items():
                        if "json" not in media:
                            continue
                        if not typed(content.get("schema") or {}):
                            opaque_2xx.append(f"{method.upper()} {path} {code}")
            for code in ("400", "401", "403", "404", "409", "412", "422", "500", "503"):
                if "application/problem+json" not in (responses.get(code, {}).get("content") or {}):
                    missing_problem.append(f"{method.upper()} {path} {code}")

    assert not opaque_requests, opaque_requests[:10]
    assert not opaque_2xx, opaque_2xx[:10]
    assert not missing_problem, missing_problem[:10]
    detail = spec["paths"]["/api/v1/items/{item_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "item_number" in detail["properties"] and "items" not in detail["properties"]
    listing = spec["paths"]["/api/v1/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "items" in listing["properties"]
    assert {"BearerAuth", "ApiKeyAuth", "SuperAdminToken"} <= set(
        spec["components"]["securitySchemes"]
    )
    assert spec.get("security")
    reports = [p for p in spec["paths"] if p.startswith("/api/v1/reports")]
    assert len(reports) == 22
