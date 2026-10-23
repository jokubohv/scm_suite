"""Typed procurement errors (SecurityError subclasses → platform problem+json envelope)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class ProcurementError(SecurityError):
    code = "PROCUREMENT_ERROR"
    http_status = 409


class ValidationFailed(ProcurementError):
    code = "VALIDATION_FAILED"
    http_status = 422


class NotFound(ProcurementError):
    code = "NOT_FOUND"
    http_status = 404


class InvalidStateTransition(ProcurementError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class DomainDisabled(ProcurementError):
    code = "DOMAIN_DISABLED"
    http_status = 409


class DuplicateDocument(ProcurementError):
    code = "DUPLICATE_DOCUMENT"
    http_status = 409


class QuantityExceedsOrder(ProcurementError):
    code = "QUANTITY_EXCEEDS_ORDER"
    http_status = 409


class MatchException(ProcurementError):
    code = "MATCH_EXCEPTION"
    http_status = 409


class CreditExceedsOpen(ProcurementError):
    code = "CREDIT_EXCEEDS_OPEN"
    http_status = 409


class DependentDocuments(ProcurementError):
    code = "DEPENDENT_DOCUMENTS"
    http_status = 409


class ItemNotPurchasable(ProcurementError):
    code = "ITEM_NOT_PURCHASABLE"
    http_status = 422


class IdempotencyKeyRequired(ProcurementError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    http_status = 400


__all__ = [
    "CreditExceedsOpen",
    "DependentDocuments",
    "DomainDisabled",
    "DuplicateDocument",
    "IdempotencyKeyRequired",
    "InvalidStateTransition",
    "ItemNotPurchasable",
    "MatchException",
    "NotFound",
    "ProcurementError",
    "QuantityExceedsOrder",
    "ValidationFailed",
]
