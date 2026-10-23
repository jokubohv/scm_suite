"""Typed consigned-inventory errors (SecurityError subclasses → problem+json)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class ConsignedError(SecurityError):
    code = "CONSIGNED_ERROR"
    http_status = 409


class ValidationFailed(ConsignedError):
    code = "VALIDATION_FAILED"
    http_status = 422


class NotFound(ConsignedError):
    code = "NOT_FOUND"
    http_status = 404


class DomainDisabled(ConsignedError):
    code = "DOMAIN_DISABLED"
    http_status = 409


class InvalidStateTransition(ConsignedError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class AgreementNotActive(ConsignedError):
    code = "AGREEMENT_NOT_ACTIVE"
    http_status = 409


class ItemNotConsignable(ConsignedError):
    code = "ITEM_NOT_CONSIGNABLE"
    http_status = 422


class AssemblyNotConsignable(ConsignedError):
    code = "ASSEMBLY_NOT_CONSIGNABLE"
    http_status = 422


class AssemblyIncompatible(ConsignedError):
    code = "CONSIGNED_ASSEMBLY_INCOMPATIBLE"
    http_status = 409


class ConsignedQtyExceeded(ConsignedError):
    code = "CONSIGNED_QTY_EXCEEDED"
    http_status = 409


class ProhibitedOperation(ConsignedError):
    code = "PROHIBITED_CONSIGNED_OPERATION"
    http_status = 409


class StockRemains(ConsignedError):
    code = "CONSIGNED_STOCK_REMAINS"
    http_status = 409


class DependentDocuments(ConsignedError):
    code = "DEPENDENT_DOCUMENTS"
    http_status = 409


__all__ = [
    "AgreementNotActive",
    "AssemblyIncompatible",
    "AssemblyNotConsignable",
    "ConsignedError",
    "ConsignedQtyExceeded",
    "DependentDocuments",
    "DomainDisabled",
    "InvalidStateTransition",
    "ItemNotConsignable",
    "NotFound",
    "ProhibitedOperation",
    "StockRemains",
    "ValidationFailed",
]
