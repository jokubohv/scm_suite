"""FS-05 costing domain errors (HTTP-free, stable machine-readable codes).

The API layer maps :class:`CostingError` to RFC 9457 problem+json; the in-process contract raises the
same errors so FS-04 can translate them inside a posting transaction (all errors roll the posting back).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CostingError(Exception):
    """Base class for costing domain errors."""

    code: str = "COSTING_ERROR"
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


class CostingValidationError(CostingError):
    code = "VALIDATION_FAILED"
    status_code = 422


class CostingNotFound(CostingError):
    code = "COSTING_NOT_FOUND"
    status_code = 404


class CostingConflict(CostingError):
    code = "COSTING_CONFLICT"
    status_code = 409


class CostingIncomplete(CostingError):
    """No usable cost exists and estimation is not allowed (ARCHITECTURE §3.1)."""

    code = "COSTING_INCOMPLETE"
    status_code = 409


class CostingRecalcRequired(CostingError):
    """D5 block: pending recalculation blocks a provisional projection (manifest acceptance)."""

    code = "COSTING_RECALC_REQUIRED"
    status_code = 409


class CostingDependencyNotDeployed(CostingError):
    """Feature identity/ownership source not deployed yet (specific/lot identity, WIP/FS-09, FS-08)."""

    code = "DEPENDENCY_NOT_DEPLOYED"
    status_code = 409


class CostingPolicyViolation(CostingError):
    """Documented incompatibility (e.g. group-average worksheet prohibition, D6/INV-28)."""

    code = "POLICY_VIOLATION"
    status_code = 422


class CostingStateError(CostingError):
    """Invalid document state-machine transition (INV-26)."""

    code = "INVALID_STATE_TRANSITION"
    status_code = 409


__all__ = [
    "CostingConflict",
    "CostingDependencyNotDeployed",
    "CostingError",
    "CostingIncomplete",
    "CostingNotFound",
    "CostingPolicyViolation",
    "CostingRecalcRequired",
    "CostingStateError",
    "CostingValidationError",
]
