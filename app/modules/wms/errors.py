"""Typed WMS domain errors (``SecurityError`` subclasses → platform problem+json envelope)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class WmsError(SecurityError):
    code = "WMS_ERROR"
    http_status = 409


class ValidationFailed(WmsError):
    code = "VALIDATION_FAILED"
    http_status = 422


class NotFound(WmsError):
    code = "NOT_FOUND"
    http_status = 404


class InvalidStateTransition(WmsError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class StaleVersion(WmsError):
    code = "STALE_VERSION"
    http_status = 412


class DomainDisabled(WmsError):
    code = "DOMAIN_DISABLED"
    http_status = 409


class DuplicateDocument(WmsError):
    code = "DUPLICATE_DOCUMENT"
    http_status = 409


class InsufficientStock(WmsError):
    code = "INSUFFICIENT_STOCK"
    http_status = 409


class BinBlocked(WmsError):
    code = "BIN_BLOCKED"
    http_status = 409


class ExpiredLot(WmsError):
    code = "EXPIRED_LOT"
    http_status = 409


class TaskConflict(WmsError):
    code = "TASK_STATUS_CONFLICT"
    http_status = 409


class WaveIncomplete(WmsError):
    code = "WAVE_INCOMPLETE"
    http_status = 409


class BridgeUnavailable(WmsError):
    """A lower-layer contract needed to post the task effect is not deployed in this environment."""

    code = "BRIDGE_UNAVAILABLE"
    http_status = 503


class CarrierBoundaryUnavailable(WmsError):
    code = "CARRIER_BOUNDARY_UNAVAILABLE"
    http_status = 409


class IdempotencyKeyRequired(WmsError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    http_status = 400


__all__ = [
    "BinBlocked",
    "BridgeUnavailable",
    "CarrierBoundaryUnavailable",
    "DomainDisabled",
    "DuplicateDocument",
    "ExpiredLot",
    "IdempotencyKeyRequired",
    "InsufficientStock",
    "InvalidStateTransition",
    "NotFound",
    "StaleVersion",
    "TaskConflict",
    "ValidationFailed",
    "WaveIncomplete",
    "WmsError",
]
