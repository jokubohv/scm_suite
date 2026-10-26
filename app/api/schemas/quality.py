"""FS-12 quality boundary DTOs (FastAPI-native response/request models)."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from app.api.schemas.common import APIModel


class QualityResponse(APIModel):
    """Quality entity/action envelope; unknown service fields are preserved."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)

    id: str | None = None
    status: str | None = None
    version: int | None = None
    items: list[Any] | None = None
    count: int | None = None
    next_cursor: str | None = None


class QualityRequest(APIModel):
    """Flexible quality write payload; validation remains in the quality service.

    The quality API accepts heterogeneous per-operation JSON payloads. The DTO makes the request
    body a named OpenAPI component (instead of an untyped object) while ``extra="allow"`` preserves
    every operation-specific field; route handlers pass ``model_dump(exclude_unset=True)`` to the
    unchanged service layer.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    reason: str | None = None
    memo: str | None = None
    comment: str | None = None
    status: str | None = None
    notes: str | None = None
    result: str | None = None
    results: list[Any] | None = None
    lines: list[Any] | None = None
    item_id: str | None = None
    location_id: str | None = None
    lot_id: str | None = None
    serial_id: str | None = None
    qty: Any | None = None
    version: int | None = None


__all__ = ["QualityRequest", "QualityResponse"]
