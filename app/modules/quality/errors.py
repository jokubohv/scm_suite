"""FS-12 quality-management domain errors (stable codes / HTTP statuses)."""

from __future__ import annotations

from typing import Any


class QualityError(Exception):
    code = "QUALITY_ERROR"
    http_status = 409

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(QualityError):
    code = "NOT_FOUND"
    http_status = 404


class SpecNotFound(NotFoundError):
    code = "SPEC_NOT_FOUND"


class PlanNotFound(NotFoundError):
    code = "INSPECTION_PLAN_NOT_FOUND"


class TriggerNotFound(NotFoundError):
    code = "INSPECTION_TRIGGER_NOT_FOUND"


class QueueNotFound(NotFoundError):
    code = "INSPECTION_QUEUE_NOT_FOUND"


class InspectionNotFound(NotFoundError):
    code = "INSPECTION_NOT_FOUND"


class CoaNotFound(NotFoundError):
    code = "COA_NOT_FOUND"


class ValidationError(QualityError):
    code = "VALIDATION_FAILED"
    http_status = 422


class SpecLineLimit(ValidationError):
    code = "SPEC_LINE_LIMIT"


class TriggerTargetRequired(ValidationError):
    code = "TRIGGER_TARGET_REQUIRED"


class CoaSamplingIneligible(ValidationError):
    code = "COA_SAMPLING_INELIGIBLE"


class SpecVersionImmutable(QualityError):
    code = "SPEC_VERSION_IMMUTABLE"


class InspectionNotPassed(QualityError):
    code = "INSPECTION_NOT_PASSED"


class InvalidStateTransition(QualityError):
    code = "INVALID_STATE_TRANSITION"


class HoldTargetNotFound(QualityError):
    code = "HOLD_TARGET_NOT_FOUND"


class HoldReleaseDenied(QualityError):
    code = "HOLD_RELEASE_DENIED"


class QualityHoldStatusInvalid(QualityError):
    code = "QUALITY_HOLD_STATUS_INVALID"


class VersionConflict(QualityError):
    code = "VERSION_CONFLICT"
    http_status = 412


class DomainDisabled(QualityError):
    code = "DOMAIN_DISABLED"


__all__ = [
    "CoaNotFound",
    "CoaSamplingIneligible",
    "DomainDisabled",
    "HoldReleaseDenied",
    "HoldTargetNotFound",
    "InspectionNotFound",
    "InspectionNotPassed",
    "InvalidStateTransition",
    "NotFoundError",
    "PlanNotFound",
    "QualityError",
    "QualityHoldStatusInvalid",
    "QueueNotFound",
    "SpecLineLimit",
    "SpecNotFound",
    "SpecVersionImmutable",
    "TriggerNotFound",
    "TriggerTargetRequired",
    "ValidationError",
    "VersionConflict",
]
