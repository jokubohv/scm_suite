"""Typed errors for the traceability registry (FS-03).

Stable ``code``/``http_status`` pairs match `feature-sets/FS-03.md` §6.1 and the platform problem+json
envelope; routers map them without re-inventing codes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class TraceabilityError(Exception):
    code: str = "TRACEABILITY_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class ValidationError(TraceabilityError):
    code = "VALIDATION_FAILED"
    http_status = 422


class NotFoundError(TraceabilityError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(TraceabilityError):
    code = "CONFLICT"
    http_status = 409


class StaleVersion(TraceabilityError):
    code = "STALE_VERSION"
    http_status = 412


class InvalidStateTransition(TraceabilityError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class InvalidHierarchy(TraceabilityError):
    code = "INVALID_HIERARCHY"
    http_status = 422


class HierarchyCycle(TraceabilityError):
    code = "HIERARCHY_CYCLE"
    http_status = 422


class HierarchyDepthExceeded(TraceabilityError):
    code = "HIERARCHY_DEPTH_EXCEEDED"
    http_status = 422


class LocationRequired(TraceabilityError):
    code = "LOCATION_REQUIRED"
    http_status = 422


class BinsNotEnabled(TraceabilityError):
    code = "BINS_NOT_ENABLED"
    http_status = 422


class BinRequired(TraceabilityError):
    code = "BIN_REQUIRED"
    http_status = 422


class ZoneLocationMismatch(TraceabilityError):
    code = "ZONE_LOCATION_MISMATCH"
    http_status = 422


class BinsInUse(TraceabilityError):
    code = "BINS_IN_USE"
    http_status = 409


class InactiveReference(TraceabilityError):
    code = "INACTIVE_REFERENCE"
    http_status = 422


class StatusInUse(TraceabilityError):
    code = "STATUS_IN_USE"
    http_status = 409


class SystemStatusImmutable(TraceabilityError):
    code = "SYSTEM_STATUS_IMMUTABLE"
    http_status = 409


class DefaultStatusConflict(TraceabilityError):
    code = "DEFAULT_STATUS_CONFLICT"
    http_status = 422


class StatusesNotEnabled(TraceabilityError):
    code = "STATUSES_NOT_ENABLED"
    http_status = 422


class UnallocatedImmutable(TraceabilityError):
    code = "UNALLOCATED_LOCATION_IMMUTABLE"
    http_status = 409


class UnallocatedNotPostable(TraceabilityError):
    code = "UNALLOCATED_LOCATION_NOT_POSTABLE"
    http_status = 422


class DomainDisabled(TraceabilityError):
    code = "DOMAIN_DISABLED"
    http_status = 409


class StockGuardUnavailable(TraceabilityError):
    code = "LOCATION_HAS_STOCK_GUARD_UNAVAILABLE"
    http_status = 409


class LocationHasStock(TraceabilityError):
    code = "LOCATION_HAS_STOCK"
    http_status = 409


__all__ = [
    "BinRequired",
    "BinsInUse",
    "BinsNotEnabled",
    "ConflictError",
    "DefaultStatusConflict",
    "DomainDisabled",
    "HierarchyCycle",
    "HierarchyDepthExceeded",
    "InactiveReference",
    "InvalidHierarchy",
    "InvalidStateTransition",
    "LocationHasStock",
    "LocationRequired",
    "NotFoundError",
    "StaleVersion",
    "StatusInUse",
    "StatusesNotEnabled",
    "StockGuardUnavailable",
    "SystemStatusImmutable",
    "TraceabilityError",
    "UnallocatedImmutable",
    "UnallocatedNotPostable",
    "ValidationError",
    "ZoneLocationMismatch",
]
