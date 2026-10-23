"""Typed FS-09 manufacturing errors (stable codes -> RFC 9457 problem responses)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ManufacturingError(Exception):
    code: str = "MANUFACTURING_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})

    def to_problem(self) -> dict[str, Any]:
        return {
            "type": f"https://inventory-platform.example/errors/{self.code.lower()}",
            "title": self.code,
            "status": self.http_status,
            "code": self.code,
            "detail": self.message,
            "details": self.details,
        }


class NotFoundError(ManufacturingError):
    code = "NOT_FOUND"
    http_status = 404


class ValidationFailed(ManufacturingError):
    code = "VALIDATION_FAILED"
    http_status = 422


class InvalidStateTransition(ManufacturingError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class PolicyViolation(ManufacturingError):
    code = "POLICY_VIOLATION"
    http_status = 409


class WipMethodIncompatible(PolicyViolation):
    code = "WIP_METHOD_INCOMPATIBLE"


class BomCycleError(ValidationFailed):
    code = "BOM_CYCLE"


class BomRevisionOverlap(ValidationFailed):
    code = "BOM_REVISION_OVERLAP"


class InsufficientAvailable(ManufacturingError):
    code = "INSUFFICIENT_AVAILABLE"
    http_status = 409


class WoOverBuild(ManufacturingError):
    code = "WO_OVER_BUILD"
    http_status = 409


class CompatibilityViolation(ManufacturingError):
    code = "COMPATIBILITY_VIOLATION"
    http_status = 409


class UnbuildDispositionInvalid(ValidationFailed):
    code = "UNBUILD_DISPOSITION_INVALID"


class UnbuildNotReversible(ManufacturingError):
    code = "UNBUILD_NOT_REVERSIBLE"
    http_status = 409


class DomainDisabled(ManufacturingError):
    code = "DOMAIN_DISABLED"
    http_status = 409


__all__ = [
    name
    for name in dir()
    if name.endswith("Error")
    or name.endswith("Failed")
    or name.endswith("Violation")
    or name in {"PolicyViolation", "WipMethodIncompatible", "DomainDisabled"}
]
