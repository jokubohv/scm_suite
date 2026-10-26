"""FS-03/FS-06 storage, status, lot and serial boundary DTOs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict

from app.api.schemas.common import APIModel


class TraceabilityResponse(APIModel):
    """Storage/status/lot/serial entity or action envelope; unknown service fields are preserved."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)

    id: str | None = None
    status: str | None = None
    version: int | None = None
    items: list[Any] | None = None
    count: int | None = None
    next_cursor: str | None = None


class TraceabilityRequest(APIModel):
    """Flexible storage/status write payload; service validation is unchanged.

    Route handlers pass ``model_dump(exclude_unset=True)`` so idempotency fingerprints and service
    calls continue to see the same plain mapping as before.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    code: str | None = None
    name: str | None = None
    description: str | None = None
    location_type: str | None = None
    zone_type: str | None = None
    parent_id: str | None = None
    location_id: str | None = None
    warehouse_id: str | None = None
    is_active: bool | None = None
    is_pickable: bool | None = None
    is_receivable: bool | None = None
    allow_negative: bool | None = None
    status_code: str | None = None
    reason: str | None = None
    memo: str | None = None
    version: int | None = None


__all__ = ["TraceabilityRequest", "TraceabilityResponse"]


class LocationCreate(APIModel):
    code: str
    name: str
    location_type: Literal["WAREHOUSE", "STORE", "RECEIVING", "TRANSIT", "CONSUMPTION"]
    parent_id: str | None = None
    address: dict | None = None
    subsidiary_ref: str | None = None
    timezone: str | None = None
    is_bins_enabled: bool = False
    is_statuses_enabled: bool = False
    is_consignment_allowed: bool = False
    allow_commit_from_non_available: bool = False


class LocationUpdate(APIModel):
    name: str | None = None
    code: str | None = None
    location_type: str | None = None
    is_unallocated: bool | None = None
    parent_id: str | None = None
    address: dict | None = None
    subsidiary_ref: str | None = None
    timezone: str | None = None
    is_bins_enabled: bool | None = None
    is_statuses_enabled: bool | None = None
    is_consignment_allowed: bool | None = None
    allow_commit_from_non_available: bool | None = None


class InventoryStatusCreate(APIModel):
    code: str
    name: str
    is_available_for_allocation: bool = True
    is_committable: bool = True
    is_sellable: bool = True
    is_consigned: bool = False
    is_default: bool = False
    is_active: bool = True


class InventoryStatusUpdate(APIModel):
    name: str | None = None
    is_available_for_allocation: bool | None = None
    is_committable: bool | None = None
    is_sellable: bool | None = None
    is_consigned: bool | None = None
    is_default: bool | None = None
    is_active: bool | None = None
