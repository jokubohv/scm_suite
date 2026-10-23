"""Pure unit tests for custom-field definition/value validation (no database)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.custom_fields import (
    ALLOWED_ENTITY_TYPES,
    CustomFieldDefinition,
    CustomFieldDefinitionError,
    CustomFieldRequiredError,
    CustomFieldTypeMismatchError,
    CustomFieldUnknownError,
    CustomFieldValidationError,
    UnknownEntityTypeError,
    assert_required_present,
    coerce_value,
    definition_from_row,
    normalise_entity_type,
    validate_value,
)


def _definition(**overrides: object) -> CustomFieldDefinition:
    base: dict[str, object] = {
        "id": uuid4(),
        "entity_type": "ITEM",
        "field_code": "warranty_months",
        "label": "Warranty months",
        "data_type": "NUMBER",
        "options": (),
        "is_required": False,
        "is_searchable": False,
        "validation": {},
    }
    base.update(overrides)
    return CustomFieldDefinition(**base)  # type: ignore[arg-type]


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid4(),
        "entity_type": "ITEM",
        "field_code": "warranty_months",
        "label": "Warranty months",
        "data_type": "NUMBER",
        "options": None,
        "is_required": True,
        "is_searchable": False,
        "validation": {"min": 0, "max": 120},
    }
    base.update(overrides)
    return base


class TestEntityAllowlist:
    def test_phase_one_allowlist_matches_fs00(self) -> None:
        assert ALLOWED_ENTITY_TYPES == frozenset(
            {
                "ITEM",
                "LOT",
                "SERIAL",
                "WORK_ORDER",
                "VENDOR",
                "PURCHASE_ORDER",
                "ITEM_RECEIPT",
                "VENDOR_BILL",
                "CUSTOMER_PART",
                "UPC",
            }
        )

    def test_normalise_entity_type(self) -> None:
        assert normalise_entity_type(" item ") == "ITEM"
        with pytest.raises(UnknownEntityTypeError) as excinfo:
            normalise_entity_type("CUSTOMER")
        assert excinfo.value.code == "CUSTOM_FIELD_ENTITY_TYPE_INVALID"


class TestDefinitionFromRow:
    def test_valid_definition(self) -> None:
        definition = definition_from_row(_row())
        assert definition.entity_type == "ITEM"
        assert definition.validation == {"min": 0, "max": 120}
        assert definition.is_required is True

    def test_select_options_required_and_unique(self) -> None:
        with pytest.raises(CustomFieldDefinitionError):
            definition_from_row(_row(data_type="SELECT", options=None, validation=None))
        with pytest.raises(CustomFieldDefinitionError):
            definition_from_row(_row(data_type="SELECT", options=["a", "a"], validation=None))
        definition = definition_from_row(_row(data_type="SELECT", options=["a", "b"], validation=None))
        assert definition.options == ("a", "b")

    def test_non_select_definition_cannot_have_options(self) -> None:
        with pytest.raises(CustomFieldDefinitionError):
            definition_from_row(_row(data_type="TEXT", options=["a"]))

    def test_unsupported_data_type_and_entity_type(self) -> None:
        with pytest.raises(CustomFieldDefinitionError):
            definition_from_row(_row(data_type="JSON"))
        with pytest.raises(CustomFieldDefinitionError):
            definition_from_row(_row(entity_type="CUSTOMER"))

    def test_malformed_validation_fails_closed(self) -> None:
        for invalid in (
            "not-a-object",
            {"unknown_rule": 1},
            {"min": "low"},
            {"min": 5, "max": 1},
            {"regex": "("},
            {"allowed_values": "nope"},
            {"min_length": -1},
        ):
            with pytest.raises(CustomFieldDefinitionError):
                definition_from_row(_row(validation=invalid))


class TestCoercion:
    def test_text(self) -> None:
        definition = _definition(data_type="TEXT")
        assert coerce_value(definition, " hello ") == (" hello ", "value_text", " hello ")
        with pytest.raises(CustomFieldTypeMismatchError):
            coerce_value(definition, 5)

    def test_number(self) -> None:
        definition = _definition()
        typed, column, value = coerce_value(definition, "12.5")
        assert column == "value_number"
        assert typed == Decimal("12.5")
        assert value == Decimal("12.5")
        for invalid in (True, "abc", float("nan"), float("inf"), "1e40"):
            with pytest.raises(CustomFieldTypeMismatchError):
                coerce_value(definition, invalid)

    def test_date(self) -> None:
        definition = _definition(data_type="DATE")
        assert coerce_value(definition, "2026-10-23")[0] == date(2026, 10, 23)
        assert coerce_value(definition, date(2026, 10, 23))[0] == date(2026, 10, 23)
        with pytest.raises(CustomFieldTypeMismatchError):
            coerce_value(definition, "23/10/2026")

    def test_bool(self) -> None:
        definition = _definition(data_type="BOOL")
        assert coerce_value(definition, "true")[0] is True
        assert coerce_value(definition, "0")[0] is False
        assert coerce_value(definition, False)[0] is False
        with pytest.raises(CustomFieldTypeMismatchError):
            coerce_value(definition, "maybe")

    def test_select(self) -> None:
        definition = _definition(data_type="SELECT", options=("painted", "raw"))
        assert coerce_value(definition, "painted")[0] == "painted"
        with pytest.raises(CustomFieldValidationError):
            coerce_value(definition, "chrome")


class TestRules:
    def test_number_min_max(self) -> None:
        definition = _definition(validation={"min": 0, "max": 120})
        validate_value(definition, "0")
        validate_value(definition, "120")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "-1")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "121")

    def test_text_length_regex_and_allowed_values(self) -> None:
        definition = _definition(
            data_type="TEXT",
            validation={"min_length": 2, "max_length": 4, "regex": "[A-Z]+", "allowed_values": ["AB", "ABC"]},
        )
        validate_value(definition, "AB")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "A")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "ABCDE")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "ab")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "XYZ")  # matches regex but not allowed_values

    def test_regex_must_match_whole_value(self) -> None:
        definition = _definition(data_type="TEXT", validation={"regex": "AB"})
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "XABX")

    def test_select_allowed_values_subset(self) -> None:
        definition = _definition(data_type="SELECT", options=("a", "b", "c"), validation={"allowed_values": ["a", "b"]})
        validate_value(definition, "a")
        with pytest.raises(CustomFieldValidationError):
            validate_value(definition, "c")


class TestRequired:
    def test_required_detection(self) -> None:
        required = _definition(field_code="req", is_required=True)
        optional = _definition(field_code="opt", is_required=False)
        definitions = {"req": required, "opt": optional}
        assert_required_present(definitions, {"req": "x"})
        with pytest.raises(CustomFieldRequiredError) as excinfo:
            assert_required_present(definitions, {"opt": "x"})
        assert excinfo.value.details["missing"] == ["req"]
        with pytest.raises(CustomFieldRequiredError):
            assert_required_present(definitions, {"req": None})


class TestUnknownFieldError:
    def test_unknown_error_contract(self) -> None:
        error = CustomFieldUnknownError("unknown", details={"unknown": ["x"]})
        assert error.code == "CUSTOM_FIELD_UNKNOWN"
        assert error.http_status == 422
