"""Typed extension-registry errors (SecurityError subclasses → problem+json)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class ExtensionError(SecurityError):
    code = "EXTENSION_ERROR"
    http_status = 409


class ValidationFailed(ExtensionError):
    code = "VALIDATION_FAILED"
    http_status = 422


class NotFound(ExtensionError):
    code = "NOT_FOUND"
    http_status = 404


class DomainDisabled(ExtensionError):
    code = "DOMAIN_DISABLED"
    http_status = 409


class InvalidStateTransition(ExtensionError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class SerialRequired(ExtensionError):
    code = "SERIAL_REQUIRED"
    http_status = 422


class SerialNotFound(ExtensionError):
    code = "SERIAL_NOT_FOUND"
    http_status = 422


class SerialAlreadyRegistered(ExtensionError):
    code = "SERIAL_ALREADY_REGISTERED"
    http_status = 409


class NotInWarranty(ExtensionError):
    code = "NOT_IN_WARRANTY"
    http_status = 409


class ValidationRequired(ExtensionError):
    code = "VALIDATION_REQUIRED"
    http_status = 409


class ClaimActionNotAllowed(ExtensionError):
    code = "CLAIM_ACTION_NOT_ALLOWED"
    http_status = 422


class RegistrationVoided(ExtensionError):
    code = "REGISTRATION_VOIDED"
    http_status = 409


class ItemNotUpcEligible(ExtensionError):
    code = "ITEM_NOT_UPC_ELIGIBLE"
    http_status = 422


class UpcChecksumInvalid(ExtensionError):
    code = "UPC_CHECKSUM_INVALID"
    http_status = 422


class UpcAlreadyAssigned(ExtensionError):
    code = "UPC_ALREADY_ASSIGNED"
    http_status = 409


class UpcRetiredNeverReusable(ExtensionError):
    code = "UPC_RETIRED_NEVER_REUSABLE"
    http_status = 409


class CustomerPartDuplicate(ExtensionError):
    code = "CUSTOMER_PART_DUPLICATE"
    http_status = 409


class CustomerPartNotFound(ExtensionError):
    code = "CUSTOMER_PART_NOT_FOUND"
    http_status = 404


__all__ = [
    "ClaimActionNotAllowed",
    "CustomerPartDuplicate",
    "CustomerPartNotFound",
    "DomainDisabled",
    "ExtensionError",
    "InvalidStateTransition",
    "ItemNotUpcEligible",
    "NotFound",
    "NotInWarranty",
    "RegistrationVoided",
    "SerialAlreadyRegistered",
    "SerialNotFound",
    "SerialRequired",
    "UpcAlreadyAssigned",
    "UpcChecksumInvalid",
    "UpcRetiredNeverReusable",
    "ValidationFailed",
    "ValidationRequired",
]
