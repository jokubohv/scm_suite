"""Platform boundary DTOs (auth/admin/tenant/RBAC/approval/API surfaces).

These models are the FastAPI response boundaries for ``app/api/routers/platform/*``. Service/repository
payloads are dicts by design; the boundary models below keep the documented envelope keys typed while
preserving additive service fields (``extra="allow"``) so the API contract is not tightened accidentally.
Request bodies in the platform routers are already typed Pydantic inputs; response DTOs live here so the
OpenAPI document references stable ``$ref`` schemas instead of anonymous ``{...}`` response objects.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from app.api.schemas.common import APIModel, Page


class PlatformObject(APIModel):
    """Generic platform resource/envelope: documented keys are optional and additive fields pass through."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)

    id: Any | None = None
    status: Any | None = None
    message: Any | None = None
    item: Any | None = None
    items: Any | None = None
    count: Any | None = None
    next_cursor: Any | None = None


class PlatformPage(Page[Any]):
    """List envelope used by platform admin/RBAC/reference list endpoints."""


class PlatformError(APIModel):
    """Problem+json error boundary (documented on routes via ``responses={...}``)."""

    type: str | None = None
    title: str | None = None
    status: int
    code: str
    message: str
    details: list[dict[str, Any]] | None = None
    correlation_id: str | None = None
    trace_id: str | None = None


class JSONRPCRequest(APIModel):
    """MCP JSON-RPC 2.0 request envelope."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = {}


class PlatformAck(PlatformObject):
    """Small acknowledgement response (retain platform resource fields for compatibility)."""


__all__ = [
    "PlatformAck",
    "PlatformError",
    "PlatformObject",
    "PlatformPage",
    "JSONRPCRequest",
]
