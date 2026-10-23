"""Typed inventory-ledger errors (SecurityError subclasses → platform problem+json envelope)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class LedgerError(SecurityError):
    code = "LEDGER_ERROR"


class ValidationFailed(LedgerError):
    code = "VALIDATION_FAILED"
    http_status = 422


class TransactionNotFound(LedgerError):
    code = "NOT_FOUND"
    http_status = 404


class InsufficientAvailable(LedgerError):
    code = "INSUFFICIENT_AVAILABLE"
    http_status = 409


class InsufficientOnHand(LedgerError):
    code = "INSUFFICIENT_ON_HAND"
    http_status = 409


class NegativeInventoryBlocked(LedgerError):
    code = "POLICY_VIOLATION"
    http_status = 409


class InvalidStateTransition(LedgerError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class DependentDocuments(LedgerError):
    code = "DEPENDENT_DOCUMENTS"
    http_status = 409


class IdempotencyConflict(LedgerError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class UnsupportedPostingType(LedgerError):
    code = "UNSUPPORTED_GENERIC_POSTING_TYPE"
    http_status = 422


class StaleVersion(LedgerError):
    code = "STALE_VERSION"
    http_status = 412


class IdempotencyKeyRequired(LedgerError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    http_status = 400


def ledger_error(message: str, *, code: str, http_status: int = 409) -> LedgerError:
    """Build a LedgerError with an explicit HTTP status (SecurityError sets code only)."""
    error = LedgerError(message, code=code)
    error.http_status = http_status
    return error


__all__ = [
    "ledger_error",
    "DependentDocuments",
    "IdempotencyConflict",
    "IdempotencyKeyRequired",
    "InsufficientAvailable",
    "InsufficientOnHand",
    "InvalidStateTransition",
    "StaleVersion",
    "LedgerError",
    "NegativeInventoryBlocked",
    "TransactionNotFound",
    "UnsupportedPostingType",
    "ValidationFailed",
]
