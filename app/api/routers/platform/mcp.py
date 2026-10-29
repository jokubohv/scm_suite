"""API-key-only MCP (Model Context Protocol) gateway.

MCP is exposed over Streamable HTTP JSON-RPC 2.0 at ``/api/v1/mcp``. Every request must carry a
tenant API key in ``X-API-Key``; JWT bearer tokens are deliberately rejected. Tool execution is
delegated to the mounted REST API with the caller's API key, so tenant RLS, scopes, expiry/revocation
and audit behaviour are identical to direct API use.

The catalogue is curated from the runtime OpenAPI document: CRUD on the main supply-chain resources
plus the important lifecycle actions. A generic ``scm_api_request`` tool covers the remaining
business endpoints without duplicating business logic.

No secrets are ever returned. This module must not import the CLI or any external MCP SDK.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import require_api_key_auth
from app.api.routing import SCMAPIRoute
from app.api.schemas.common import PROBLEM_RESPONSES
from app.api.schemas.platform import JSONRPCRequest, PlatformObject

router = APIRouter(prefix="/api/v1/mcp", responses=PROBLEM_RESPONSES, tags=["mcp"], route_class=SCMAPIRoute)

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "scm-inventory-mcp"

#: Main resources exposed as curated tools. Paths are matched exactly, one level of ``/{id}`` and
#: ``/{id}/<action>``; deep sub-resources remain available through ``scm_api_request``.
CURATED_BASES: tuple[str, ...] = (
    "/api/v1/items",
    "/api/v1/item-types",
    "/api/v1/uoms",
    "/api/v1/uom-conversions",
    "/api/v1/item-categories",
    "/api/v1/locations",
    "/api/v1/bins",
    "/api/v1/inventory-statuses",
    "/api/v1/inventory/balances/search",
    "/api/v1/inventory-adjustments",
    "/api/v1/inventory-transfers",
    "/api/v1/inventory-counts",
    "/api/v1/inventory/commitments",
    "/api/v1/count-plans",
    "/api/v1/vendors",
    "/api/v1/purchase-orders",
    "/api/v1/purchase-requests",
    "/api/v1/purchase-requisitions",
    "/api/v1/rfqs",
    "/api/v1/vendor-contracts",
    "/api/v1/item-receipts",
    "/api/v1/lots",
    "/api/v1/fulfillments",
    "/api/v1/inventory-transactions",
    "/api/v1/vendor-bills",
    "/api/v1/vendor-credits",
    "/api/v1/vendor-returns",
    "/api/v1/warehouses",
    "/api/v1/inbound-shipments",
    "/api/v1/receiving-orders",
    "/api/v1/putaway-tasks",
    "/api/v1/waves",
    "/api/v1/pick-tasks",
    "/api/v1/pack-tasks",
    "/api/v1/shipments",
    "/api/v1/assemblies",
    "/api/v1/boms",
    "/api/v1/work-orders",
    "/api/v1/routings",
    "/api/v1/work-centers",
    "/api/v1/quality/specs",
    "/api/v1/quality/inspection-plans",
    "/api/v1/quality/inspections",
    "/api/v1/consignment-agreements",
    "/api/v1/consignment-transfers",
    "/api/v1/warranty/registrations",
    "/api/v1/upc-registry",
    "/api/v1/price-levels",
    "/api/v1/item-prices",
    "/api/v1/allocation-runs",
    "/api/v1/reports",
)

#: Tools whose only "input" is the empty JSON object: they duplicate a plain list read that
#: ``scm_api_request`` already covers, so they are not worth a curated entry (P1 residual cleanup).
EMPTY_TOOL_EXCLUSIONS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/reports"),
    ("GET", "/api/v1/work-centers"),
})

#: POST lifecycle suffixes worth exposing as named tools.
ACTION_TAILS: frozenset[str] = frozenset({
    "submit", "post", "approve", "reject", "release", "start", "complete", "capture", "cancel",
    "activate", "inactivate", "match", "arrive", "build", "issue", "close", "receive", "recount",
    "putaway", "pick", "pack", "ship", "confirm", "assign", "reverse", "void",
    "lines", "recalculate-snapshot",
})

EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth", "/api/v1/me", "/api/v1/admin", "/api/v1/platform", "/api/v1/bootstrap",
    "/api/v1/internal", "/api/v1/mcp", "/api/v1/integrations",
)

_SAFE_NAME = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    method: str
    path: str
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    header_params: tuple[str, ...] = ()
    body_properties: tuple[str, ...] = ()
    body_single: bool = False
    body_additional: bool = False
    has_body: bool = False
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    input_schema: dict[str, Any] = field(default_factory=dict)
    source_operation_id: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
            "x-http-method": self.method,
            "x-http-path": self.path,
            "x-source-operation-id": self.source_operation_id,
        }


_TOOL_CACHE: dict[int, list[ToolSpec]] = {}


def _resolve_ref(spec: dict[str, Any], value: Any) -> Any:
    seen: set[str] = set()
    while isinstance(value, dict) and "$ref" in value:
        ref = value["$ref"]
        if ref in seen or not ref.startswith("#/"):
            return value
        seen.add(ref)
        node: Any = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        value = node
    return value


def _matches_curated(path: str) -> tuple[bool, str]:
    for base in CURATED_BASES:
        if path == base:
            return True, "collection"
        match = re.fullmatch(re.escape(base) + r"/\{([^}]+)\}", path)
        if match:
            return True, "detail"
        match = re.fullmatch(re.escape(base) + r"/\{([^}]+)\}/([A-Za-z0-9_.-]+)", path)
        if match and match.group(2) in ACTION_TAILS:
            return True, "action"
    return False, ""


def _tool_name(method: str, path: str) -> str:
    relative = path.removeprefix("/api/v1/").strip("/")
    segments = relative.split("/") if relative else []
    stem_parts: list[str] = []
    tail = segments[-1] if segments else ""
    for segment in segments:
        if segment.startswith("{") and segment.endswith("}"):
            continue
        if tail in ACTION_TAILS and segment == tail and segment is segments[-1]:
            continue
        stem_parts.append(segment.replace("-", "_"))
    stem = "_".join(stem_parts) or "root"
    if tail.startswith("{"):
        suffix = {"get": "get", "patch": "update", "delete": "delete"}.get(method.lower(), method.lower())
    elif tail in ACTION_TAILS:
        suffix = tail.replace("-", "_")
    elif method.lower() == "get":
        suffix = "list"
    elif method.lower() == "post":
        suffix = "create"
    else:
        suffix = method.lower()
    name = _SAFE_NAME.sub("_", f"{stem}_{suffix}".lower()).strip("_")
    return name or f"{method.lower()}_root"


def _schema_for_operation(spec: dict[str, Any], path: str, operation: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    header_params: list[str] = []

    path_names = re.findall(r"\{([^}]+)\}", path)
    param_list: list[dict[str, Any]] = []
    for raw in operation.get("parameters", []) or []:
        param = _resolve_ref(spec, raw)
        if isinstance(param, dict):
            param_list.append(param)

    by_name: dict[str, dict[str, Any]] = {}
    for param in param_list:
        name = str(param.get("name") or "")
        where = str(param.get("in") or "")
        if not name:
            continue
        field_schema = _resolve_ref(spec, param.get("schema") or {})
        if where == "path":
            path_params.append(name)
            properties[name] = field_schema or {"type": "string"}
            required.append(name)
            by_name[name] = param
        elif where == "query":
            query_params.append(name)
            properties[name] = field_schema or {"type": "string"}
            if param.get("required"):
                required.append(name)
            by_name[name] = param
        elif where == "header" and param.get("required"):
            header_params.append(name)
            properties[name] = field_schema or {"type": "string"}
            required.append(name)
            by_name[name] = param

    for name in path_names:
        if name not in properties:
            properties[name] = {"type": "string"}
            required.append(name)
            path_params.append(name)

    body_properties: list[str] = []
    body_single = False
    body_additional = False
    has_body = False
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        content = (request_body.get("content") or {}).get("application/json") or {}
        body_schema = _resolve_ref(spec, content.get("schema") or {})
        # FastAPI emits optional Pydantic bodies as anyOf[Model, null] (count approve, work-order
        # actions, etc.). For tooling, use the object branch's fields directly rather than wrapping
        # them under a synthetic "body" property.
        if isinstance(body_schema, dict) and (body_schema.get("anyOf") or body_schema.get("oneOf")):
            branches = body_schema.get("anyOf") or body_schema.get("oneOf") or []
            object_branches = []
            for branch in branches:
                resolved = _resolve_ref(spec, branch)
                if isinstance(resolved, dict) and (
                    resolved.get("properties") or resolved.get("type") == "object"
                ):
                    object_branches.append(resolved)
            if object_branches:
                body_schema = object_branches[0]
        if body_schema:
            has_body = True
            if body_schema.get("type") == "object" or "properties" in body_schema:
                props = body_schema.get("properties") or {}
                for name, child in props.items():
                    if name in properties:
                        continue
                    properties[name] = _resolve_ref(spec, child)
                    body_properties.append(name)
                for name in body_schema.get("required") or []:
                    if name not in required:
                        required.append(name)
                body_additional = bool(body_schema.get("additionalProperties")) or not props
            else:
                body_single = True
                has_body = True
                properties["body"] = body_schema
                if request_body.get("required"):
                    required.append("body")
        elif request_body.get("required"):
            has_body = True
            body_additional = True

    if body_additional:
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": True}
    else:
        schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = sorted(dict.fromkeys(required))
    schema["additionalProperties"] = body_additional
    return {
        "input_schema": schema,
        "path_params": tuple(dict.fromkeys(path_params)),
        "query_params": tuple(dict.fromkeys(query_params)),
        "header_params": tuple(dict.fromkeys(header_params)),
        "body_properties": tuple(dict.fromkeys(body_properties)),
        "body_single": body_single,
        "body_additional": body_additional,
        "has_body": has_body,
    }


def build_tool_registry(spec: dict[str, Any]) -> list[ToolSpec]:
    """Build the curated MCP tool list from a runtime OpenAPI document."""
    paths = spec.get("paths") or {}
    selected: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}
    for path, item in paths.items():
        if any(path == prefix or path.startswith(prefix + "/") for prefix in EXCLUDED_PREFIXES):
            continue
        matched, shape = _matches_curated(path)
        if not matched:
            continue
        normalized = re.sub(r"\{[^}]+\}", "{}", path)
        for method, operation in item.items():
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PATCH", "DELETE"}:
                continue
            if (method_upper, path) in EMPTY_TOOL_EXCLUSIONS:
                continue
            if shape == "collection" and method_upper not in {"GET", "POST"}:
                continue
            if shape == "detail" and method_upper not in {"GET", "PATCH", "DELETE"}:
                continue
            if shape == "action" and method_upper != "POST":
                continue
            key = (normalized, method_upper)
            existing = selected.get(key)
            op_id = str(operation.get("operationId") or "")
            if existing is None or ("alias" in existing[2].lower() and "alias" not in op_id.lower()):
                selected[key] = (path, operation, op_id)

    tools: list[ToolSpec] = []
    used_names: set[str] = set()
    for (_, method), (path, operation, op_id) in sorted(selected.items(), key=lambda row: (row[0][0], row[0][1])):
        name = _tool_name(method, path)
        if name in used_names:
            name = f"{name}_{method.lower()}"
        used_names.add(name)
        schema_data = _schema_for_operation(spec, path, operation)
        summary = str(operation.get("summary") or operation.get("description") or "").strip()
        description = summary or f"{method} {path}"
        if len(description) > 400:
            description = description[:397] + "..."
        tools.append(
            ToolSpec(
                name=name,
                description=description,
                method=method,
                path=path,
                path_params=schema_data["path_params"],
                query_params=schema_data["query_params"],
                header_params=schema_data["header_params"],
                body_properties=schema_data["body_properties"],
                body_single=schema_data["body_single"],
                body_additional=schema_data["body_additional"],
                has_body=schema_data["has_body"],
                read_only=method == "GET",
                destructive=method == "DELETE",
                idempotent=method in {"GET", "PUT", "DELETE"},
                input_schema=schema_data["input_schema"],
                source_operation_id=op_id,
            )
        )

    # Generic escape hatch for operations outside the curated catalogue.
    tools.append(
        ToolSpec(
            name="scm_api_request",
            description=(
                "Call any SCM business REST endpoint with this API key. Path must start /api/v1/ and "
                "auth/admin/internal/MCP paths are blocked. The key's scopes and tenant RLS apply."
            ),
            method="CALL",
            path="",
            input_schema={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                    "path": {"type": "string", "pattern": "^/api/v1/"},
                    "query": {"type": "object", "additionalProperties": True},
                    "body": {"type": "object", "additionalProperties": True},
                },
                "required": ["method", "path"],
                "additionalProperties": False,
            },
            read_only=False,
        )
    )
    return tools


def _tools_for_app(app: Any) -> list[ToolSpec]:
    cache_key = id(app)
    cached = _TOOL_CACHE.get(cache_key)
    if cached is None:
        cached = build_tool_registry(app.openapi())
        _TOOL_CACHE[cache_key] = cached
    return cached


def _mcp_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tool_result(status: int, data: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"status_code": status, "data": data}, default=str, ensure_ascii=False),
            }
        ],
        "isError": status >= 400,
    }


def _jsonrpc_payload(payload: Any, request_id: Any) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, None, _mcp_error(request_id, -32600, "Request must be a JSON object")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return None, None, _mcp_error(request_id, -32600, "Invalid JSON-RPC request")
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None, None, _mcp_error(request_id, -32602, "params must be an object")
    return payload, payload["method"], params


async def _call_tool(tool: ToolSpec, arguments: dict[str, Any], request: Request) -> dict[str, Any]:
    api_key = request.headers.get("x-api-key", "")
    if tool.name == "scm_api_request":
        method = str(arguments.get("method") or "").upper()
        path = str(arguments.get("path") or "")
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("method must be GET/POST/PATCH/PUT/DELETE")
        if not path.startswith("/api/v1/"):
            raise ValueError("path must start with /api/v1/")
        blocked = ("/api/v1/auth", "/api/v1/me", "/api/v1/admin", "/api/v1/platform", "/api/v1/internal",
                   "/api/v1/bootstrap", "/api/v1/mcp")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in blocked):
            raise ValueError("path is not available through MCP")
        proxy_query = arguments.get("query") or None
        proxy_body = arguments.get("body")
        return await _proxy_http(request, method, path, api_key=api_key, query=proxy_query, body=proxy_body)

    args = dict(arguments)
    path = tool.path
    for name in tool.path_params:
        if name not in args:
            raise ValueError(f"missing path parameter {name!r}")
        path = path.replace("{" + name + "}", str(args.pop(name)))
    query_params: dict[str, Any] = {}
    for name in tool.query_params:
        if name in args:
            query_params[name] = args.pop(name)
    headers: dict[str, str] = {}
    for name in tool.header_params:
        if name in args:
            headers[name] = str(args.pop(name))
    tool_body: Any = None
    if tool.body_single:
        tool_body = args.pop("body", None)
    elif tool.body_properties:
        tool_body = {name: args.pop(name) for name in tool.body_properties if name in args}
        if not tool_body and not tool.body_additional:
            tool_body = None
    elif tool.body_additional:
        tool_body = dict(args) or None
        args.clear()
    if method_requires_idempotency(tool.method):
        headers.setdefault("Idempotency-Key", secrets.token_hex(16))
    return await _proxy_http(
        request, tool.method, path, api_key=api_key, query=query_params or None, body=tool_body, headers=headers
    )


def method_requires_idempotency(method: str) -> bool:
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


async def _proxy_http(
    request: Request,
    method: str,
    path: str,
    *,
    api_key: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError("API key missing from request")
    request_headers = {"X-API-Key": api_key, **(headers or {})}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.internal", timeout=30) as client:
        response = await client.request(
            method.upper(),
            path,
            params=query,
            json=body if method.upper() in {"POST", "PUT", "PATCH"} else None,
            headers=request_headers,
        )
    try:
        data: Any = response.json()
    except Exception:
        data = {"text": response.text[:4000]}
    return _tool_result(response.status_code, data)


def _prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": "inventory_adjustment",
            "description": "Create, submit and post an inventory adjustment safely.",
            "arguments": [
                {"name": "item_id", "description": "Item UUID", "required": True},
                {"name": "location_id", "description": "Location UUID", "required": True},
                {"name": "inventory_status_id", "description": "Status UUID", "required": True},
                {"name": "qty_delta", "description": "Signed quantity delta", "required": True},
            ],
        },
        {
            "name": "procure_to_pay",
            "description": "Create a PO, receive it and post the vendor bill.",
            "arguments": [
                {"name": "vendor_id", "description": "Vendor UUID", "required": True},
                {"name": "item_id", "description": "Item UUID", "required": True},
            ],
        },
        {
            "name": "cycle_count",
            "description": "Create, start, capture and submit a cycle count.",
            "arguments": [
                {"name": "item_id", "description": "Item UUID", "required": True},
                {"name": "location_id", "description": "Location UUID", "required": False},
            ],
        },
    ]


def _resources(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "uri": "scm://openapi",
            "name": "Runtime OpenAPI",
            "description": "Mounted runtime OpenAPI 3.1 document.",
            "mimeType": "application/json",
        },
        {
            "uri": "scm://mcp/info",
            "name": "MCP server information",
            "description": "Server identity, protocol and capabilities.",
            "mimeType": "application/json",
        },
        {
            "uri": "scm://mcp/tools",
            "name": "MCP tool catalogue",
            "description": "Curated tools with input schemas and HTTP mappings.",
            "mimeType": "application/json",
        },
    ]


@router.get(
    "",
    response_model=PlatformObject,
    dependencies=[Depends(require_api_key_auth)],
    summary="MCP server information",
    description=(
        "API-key-only discovery document for the MCP gateway. Returns protocol, transport, auth, "
        "tool count and resource/prompt catalogues. Requires X-API-Key; JWT is rejected."
    ),
)
async def mcp_info(request: Request) -> dict[str, Any]:
    tools = _tools_for_app(request.app)
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "protocolVersion": PROTOCOL_VERSION,
        "transport": "streamable-http-jsonrpc",
        "endpoint": "/api/v1/mcp",
        "auth": {"header": "X-API-Key", "jwt_allowed": False},
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "tool_count": len(tools),
        "tools": [tool.public() for tool in tools],
        "resources": _resources(request),
        "prompts": _prompts(),
    }


@router.post(
    "",
    response_model=PlatformObject,
    dependencies=[Depends(require_api_key_auth)],
    summary="MCP Streamable HTTP JSON-RPC endpoint",
    description=(
        "API-key-only MCP JSON-RPC 2.0 gateway. Supports initialize, ping, tools/list, tools/call, "
        "resources/list, resources/read, prompts/list and prompts/get. Every tool call is executed "
        "against the REST API with the caller's X-API-Key, so scopes/tenant RLS are enforced. "
        "JWT bearer tokens are rejected."
    ),
)
async def mcp_endpoint(request: Request, body: JSONRPCRequest) -> Response:
    payload = body.model_dump()
    request_id = payload.get("id")
    parsed, method, params = _jsonrpc_payload(payload, request_id)
    if parsed is None or method is None:
        return JSONResponse(_mcp_error(request_id, -32600, str(params)), status_code=400)
    if request_id is None and method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": "Use X-API-Key. Tools inherit the calling key's tenant and scopes.",
            },
        })

    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})

    if method == "tools/list":
        tools = _tools_for_app(request.app)
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [tool.public() for tool in tools]}})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return JSONResponse(_mcp_error(request_id, -32602, "tools/call requires name and object arguments"))
        tools_by_name = {tool.name: tool for tool in _tools_for_app(request.app)}
        tool = tools_by_name.get(name)
        if tool is None:
            return JSONResponse(_mcp_error(request_id, -32602, f"unknown tool {name!r}"))
        try:
            result = await _call_tool(tool, arguments, request)
        except ValueError as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps({"status_code": 422, "data": {"code": "TOOL_ARGUMENT_ERROR",
                                                                                                 "message": str(exc)}}) }],
                "isError": True,
            }})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    if method == "resources/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"resources": _resources(request)}})

    if method == "resources/read":
        uri = params.get("uri")
        if uri == "scm://openapi":
            text = json.dumps(request.app.openapi(), default=str)
            mime = "application/json"
        elif uri == "scm://mcp/tools":
            text = json.dumps([tool.public() for tool in _tools_for_app(request.app)], default=str)
            mime = "application/json"
        elif uri == "scm://mcp/info":
            text = json.dumps(await mcp_info(request), default=str)
            mime = "application/json"
        else:
            return JSONResponse(_mcp_error(request_id, -32602, f"unknown resource {uri!r}"))
        return JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"contents": [{"uri": uri, "mimeType": mime, "text": text}]},
        })

    if method == "prompts/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"prompts": _prompts()}})

    if method == "prompts/get":
        name = params.get("name")
        prompt = next((item for item in _prompts() if item["name"] == name), None)
        if prompt is None:
            return JSONResponse(_mcp_error(request_id, -32602, f"unknown prompt {name!r}"))
        args = params.get("arguments") or {}
        return JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "description": prompt["description"],
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": f"Use the SCM tools for {name}. Arguments: {json.dumps(args, default=str)}"},
                }],
            },
        })

    return JSONResponse(_mcp_error(request_id, -32601, f"method not found: {method}"))
