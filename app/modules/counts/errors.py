"""FS-07 counts domain errors (HTTP-free, stable machine-readable codes)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CountsError(Exception):
    code: str = "COUNTS_ERROR"
    status_code: int = 422

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(self.message)

    def to_problem(self) -> dict[str, Any]:
        return {
            "type": f"https://inventory-platform.example/errors/{self.code.lower()}",
            "title": self.code,
            "status": self.status_code,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class CountsValidationError(CountsError):
    code = "VALIDATION_FAILED"
    status_code = 422


class CountsNotFound(CountsError):
    code = "COUNT_NOT_FOUND"
    status_code = 404


class CountsConflict(CountsError):
    code = "COUNTS_CONFLICT"
    status_code = 409


class CountStateError(CountsError):
    code = "INVALID_STATE_TRANSITION"
    status_code = 409


class CountAlreadyPosted(CountsError):
    code = "DOCUMENT_ALREADY_POSTED"
    status_code = 409


class ProhibitedCountType(CountsError):
    """Consigned positions cannot post through T-20 (DM-CNT-06; routed to FS-13 later)."""

    code = "PROHIBITED_COUNT_TYPE"
    status_code = 409


class CountsDisabled(CountsError):
    code = "DOMAIN_DISABLED"
    status_code = 409


class SnapshotImmutable(CountsError):
    code = "COUNT_SNAPSHOT_IMMUTABLE"
    status_code = 409


__all__ = [
    "CountAlreadyPosted",
    "CountsConflict",
    "CountsDisabled",
    "CountsError",
    "CountsNotFound",
    "CountsValidationError",
    "CountStateError",
    "ProhibitedCountType",
    "SnapshotImmutable",
]
