"""FS-06 numbered-inventory domain errors (stable API codes / HTTP statuses)."""

from __future__ import annotations

from typing import Any


class NumberedError(Exception):
    code = "NUMBERED_ERROR"
    http_status = 409

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(NumberedError):
    code = "NOT_FOUND"
    http_status = 404


class LotNotFound(NotFoundError):
    code = "LOT_NOT_FOUND"


class SerialNotFound(NotFoundError):
    code = "SERIAL_NOT_FOUND"


class ValidationError(NumberedError):
    code = "VALIDATION_FAILED"
    http_status = 422


class ItemNotNumbered(ValidationError):
    code = "ITEM_NOT_NUMBERED"


class LotExpired(ValidationError):
    code = "LOT_EXPIRED"


class LotRequired(ValidationError):
    code = "LOT_REQUIRED"


class ValidationFailed(ValidationError):
    code = "NUMBERED_VALIDATION_FAILED"


class ConflictError(NumberedError):
    http_status = 409


class LotConflict(ConflictError):
    code = "LOT_CONFLICT"


class SerialConflict(ConflictError):
    code = "SERIAL_CONFLICT"


class LotInUse(ConflictError):
    code = "LOT_IN_USE"


class VersionConflict(ConflictError):
    code = "VERSION_CONFLICT"
    http_status = 412


class DomainDisabled(ConflictError):
    code = "DOMAIN_DISABLED"


class IdentityImmutable(ConflictError):
    code = "IDENTITY_IMMUTABLE"


__all__ = [
    "ConflictError",
    "DomainDisabled",
    "IdentityImmutable",
    "ItemNotNumbered",
    "LotConflict",
    "LotExpired",
    "LotInUse",
    "LotNotFound",
    "LotRequired",
    "NotFoundError",
    "NumberedError",
    "SerialConflict",
    "SerialNotFound",
    "ValidationError",
    "ValidationFailed",
    "VersionConflict",
]
