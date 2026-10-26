"""FS-09 manufacturing boundary DTOs (FastAPI-native response models).

Manufacturing routes return entity envelopes (``{"assembly": ...}``, ``{"items": [...]}``,
action results, inquiries). The boundary keeps those service shapes intact while exposing a typed
OpenAPI component; unknown future fields are preserved through ``extra="allow"``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from app.api.schemas.common import APIModel


class ManufacturingResponse(APIModel):
    """Generic boundary envelope for manufacturing reads/actions.

    ``extra="allow"`` preserves service-owned envelope keys (assembly/bom/revision/component/...)
    during the DTO migration without redefining the service payload shape as a request model.
    """

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)

    id: str | None = None
    status: str | None = None
    version: int | None = None
    items: list[Any] | None = None
    count: int | None = None
    next_cursor: str | None = None


__all__ = ["ManufacturingResponse"]
