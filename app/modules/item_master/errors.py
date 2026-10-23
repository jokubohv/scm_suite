"""Typed FS-01 item-master errors (SecurityError subclass → platform problem+json envelope)."""

from __future__ import annotations

from app.core.security.errors import SecurityError


class ItemMasterError(SecurityError):
    code = "ITEM_MASTER_ERROR"


class ItemNotFound(ItemMasterError):
    code = "ITEM_NOT_FOUND"
    http_status = 404


class ItemNumberExists(ItemMasterError):
    code = "ITEM_NUMBER_EXISTS"
    http_status = 409


class ItemTypeNotFound(ItemMasterError):
    code = "ITEM_TYPE_NOT_FOUND"
    http_status = 404


class ItemLocationNotFound(ItemMasterError):
    code = "ITEM_LOCATION_NOT_FOUND"
    http_status = 404


class ItemTypeFlagMismatch(ItemMasterError):
    code = "TYPE_FLAG_MISMATCH"
    http_status = 422


class ItemInactive(ItemMasterError):
    code = "INACTIVE_REFERENCE"
    http_status = 422


class ItemHasActiveChildren(ItemMasterError):
    code = "ITEM_HAS_ACTIVE_CHILDREN"
    http_status = 409


class UomAssignmentInvalid(ItemMasterError):
    code = "UOM_ASSIGNMENT_INVALID"
    http_status = 422


class MatrixOptionInvalid(ItemMasterError):
    code = "MATRIX_OPTION_INVALID"
    http_status = 422


class MatrixOptionInUse(ItemMasterError):
    code = "MATRIX_OPTION_IN_USE"
    http_status = 422


class UomInUse(ItemMasterError):
    code = "UOM_IN_USE"
    http_status = 409


class TypeMigrationInvalid(ItemMasterError):
    code = "TYPE_MIGRATION_INVALID"
    http_status = 422


class MatrixTooManyCombinations(ItemMasterError):
    code = "MATRIX_TOO_MANY_COMBINATIONS"
    http_status = 422


class MatrixChildExists(ItemMasterError):
    code = "MATRIX_CHILD_EXISTS"
    http_status = 409


class KitMemberInvalid(ItemMasterError):
    code = "KIT_MEMBER_INVALID"
    http_status = 422


class BarcodeInvalid(ItemMasterError):
    code = "BARCODE_INVALID"
    http_status = 422


class PlanningValuesInvalid(ItemMasterError):
    code = "PLANNING_VALUES_INVALID"
    http_status = 422


class PlanningAutoCalcDisabled(ItemMasterError):
    code = "PLANNING_AUTO_CALC_DISABLED"
    http_status = 409


class IdempotencyKeyRequired(ItemMasterError):
    code = "IDEMPOTENCY_KEY_REQUIRED"
    http_status = 400


def item_error(message: str, *, code: str, http_status: int = 422) -> ItemMasterError:
    """Build an ItemMasterError with an explicit HTTP status (SecurityError sets code only)."""
    error = ItemMasterError(message, code=code)
    error.http_status = http_status
    return error


__all__ = [
    "BarcodeInvalid",
    "IdempotencyKeyRequired",
    "item_error",
    "ItemHasActiveChildren",
    "ItemInactive",
    "ItemLocationNotFound",
    "ItemMasterError",
    "ItemNotFound",
    "ItemNumberExists",
    "ItemTypeFlagMismatch",
    "ItemTypeNotFound",
    "KitMemberInvalid",
    "MatrixChildExists",
    "MatrixOptionInUse",
    "MatrixOptionInvalid",
    "MatrixTooManyCombinations",
    "TypeMigrationInvalid",
    "UomInUse",
    "PlanningAutoCalcDisabled",
    "PlanningValuesInvalid",
    "UomAssignmentInvalid",
]
