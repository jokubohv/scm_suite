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
    receipt = spec["paths"]["/api/v1/item-receipts"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert {"vendor_id", "lines", "receipt_type", "location_id"} <= set(receipt["properties"])
    assert "vendor_id" in receipt["required"] and "lines" in receipt["required"]
    adjustment = spec["paths"]["/api/v1/inventory-adjustments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "lines" in adjustment["properties"] and "transaction_date" in adjustment["required"]

    from app.api.main import create_app as _create

    tools = mcp_router.build_tool_registry(_create().openapi())
    by_name = {tool.name: tool for tool in tools}
    receipt_tool = by_name["item_receipts_create"]
    assert "vendor_id" in receipt_tool.input_schema["properties"]
    assert "lines" in receipt_tool.input_schema["properties"]
    assert receipt_tool.body_additional is False


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
