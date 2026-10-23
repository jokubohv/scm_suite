"""Pure validation rules for the reference/config repository (no database needed)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.repositories.config import (
    ConfigConflict,
    ConfigError,
    ConfigStale,
    assert_period_transition_guarded,
    custom_field_change_required,
    flag_change_values,
    parse_if_match,
    periods_overlap,
    validate_currency_code,
    validate_currency_create,
    validate_currency_patch,
    validate_custom_field_create,
    validate_custom_field_patch,
    validate_fiscal_period_create,
    validate_flag_key,
    validate_flag_patch,
    validate_sequence_filter,
)


# ------------------------------------------------------------------------- currency ------------
def test_currency_code_normalises_case_and_rejects_bad_shapes() -> None:
    assert validate_currency_code("usd") == "USD"
    assert validate_currency_code(" eur ") == "EUR"
    for bad in ("US", "USDD", "1US", "", None, 123):
        with pytest.raises(ConfigError) as exc:
            validate_currency_code(bad)
        assert exc.value.code == "CURRENCY_CODE_INVALID"


def test_currency_create_defaults_and_bounds() -> None:
    values = validate_currency_create({"code": "xTs".upper()})
    assert values == {"code": "XTS", "numeric_code": None, "minor_units": 2, "is_active": True}
    values = validate_currency_create({"code": "ZZZ", "numeric_code": 999, "minor_units": 0, "is_active": False})
    assert values["numeric_code"] == 999 and values["minor_units"] == 0 and values["is_active"] is False
    with pytest.raises(ConfigError) as exc:
        validate_currency_create({"code": "ZZZ", "numeric_code": 1000})
    assert exc.value.code == "VALIDATION_ERROR"
    with pytest.raises(ConfigError):
        validate_currency_create({"code": "ZZZ", "minor_units": 5})
    with pytest.raises(ConfigError):
        validate_currency_create({"code": "ZZZ", "is_active": "yes"})
    with pytest.raises(ConfigError):
        validate_currency_create({"numeric_code": 1})  # missing code


def test_currency_patch_rejects_code_and_empty_payload() -> None:
    assert validate_currency_patch({"is_active": False}) == {"is_active": False}
    assert validate_currency_patch({"numeric_code": None}) == {"numeric_code": None}
    with pytest.raises(ConfigError) as exc:
        validate_currency_patch({"code": "USD"})
    assert exc.value.code == "CURRENCY_CODE_IMMUTABLE"
    with pytest.raises(ConfigError, match="at least one mutable"):
        validate_currency_patch({})


# --------------------------------------------------------------------- fiscal periods --------
def test_fiscal_period_create_validates_dates_and_status() -> None:
    values = validate_fiscal_period_create(
        {"period_key": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-31"}
    )
    assert values["status"] == "OPEN"
    assert values["start_date"] == date(2026, 10, 1) and values["end_date"] == date(2026, 10, 31)
    assert values["closed_at"] is None
    assert values["source_version"] == 1

    closed = validate_fiscal_period_create(
        {"period_key": "2026-09", "start_date": "2026-09-01", "end_date": "2026-09-30", "status": "closed"}
    )
    assert closed["status"] == "CLOSED"
    assert isinstance(closed["closed_at"], datetime) and closed["closed_at"].tzinfo is not None

    with pytest.raises(ConfigError) as exc:
        validate_fiscal_period_create(
            {"period_key": "2026-10", "start_date": date(2026, 10, 31), "end_date": date(2026, 10, 1)}
        )
    assert exc.value.code == "PERIOD_DATES_INVALID"
    with pytest.raises(ConfigError) as exc:
        validate_fiscal_period_create(
            {"period_key": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-31", "status": "OPENISH"}
        )
    assert exc.value.code == "PERIOD_STATUS_INVALID"
    with pytest.raises(ConfigError):
        validate_fiscal_period_create({"period_key": "", "start_date": "2026-10-01", "end_date": "2026-10-31"})


def test_period_overlap_predicate_is_inclusive() -> None:
    assert periods_overlap(date(2026, 10, 1), date(2026, 10, 31), date(2026, 10, 31), date(2026, 11, 30))
    assert periods_overlap(date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 30)) is False
    assert periods_overlap(date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 31)) is False


def test_period_status_transition_guards() -> None:
    assert_period_transition_guarded(
        current_status="OPEN", new_status="CLOSED", current_source_version=1, new_source_version=1
    )
    assert_period_transition_guarded(
        current_status="CLOSED", new_status="LOCKED", current_source_version=1, new_source_version=1
    )
    with pytest.raises(ConfigConflict) as exc:
        assert_period_transition_guarded(
            current_status="CLOSED", new_status="OPEN", current_source_version=1, new_source_version=1
        )
    assert exc.value.code == "PERIOD_REOPEN_REQUIRES_SOURCE_VERSION"
    # A newer finance source_version authorises the reopen.
    assert_period_transition_guarded(
        current_status="CLOSED", new_status="OPEN", current_source_version=1, new_source_version=2
    )
    with pytest.raises(ConfigConflict) as exc:
        assert_period_transition_guarded(
            current_status="LOCKED", new_status="CLOSED", current_source_version=1, new_source_version=2
        )
    assert exc.value.code == "PERIOD_TRANSITION_INVALID"
    with pytest.raises(ConfigError) as exc2:
        assert_period_transition_guarded(
            current_status="OPEN", new_status="BOGUS", current_source_version=1, new_source_version=1
        )
    assert exc2.value.code == "PERIOD_STATUS_INVALID"


# ---------------------------------------------------------------------- custom fields ---------
def _custom_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "entity_type": "ITEM",
        "field_code": "warranty_months",
        "label": "Warranty months",
        "data_type": "NUMBER",
    }
    payload.update(overrides)
    return payload


def test_custom_field_create_validates_types_and_entity_scope() -> None:
    values = validate_custom_field_create(_custom_payload())
    assert values["entity_type"] == "ITEM"
    assert values["data_type"] == "NUMBER"
    assert values["is_required"] is False and values["is_searchable"] is False
    assert values["options"] is None and values["validation"] is None

    select = validate_custom_field_create(
        _custom_payload(data_type="SELECT", options=["one", "two"])
    )
    assert select["options"] == ["one", "two"]

    with pytest.raises(ConfigError) as exc:
        validate_custom_field_create(_custom_payload(entity_type="SPACESHIP"))
    assert exc.value.code == "CUSTOM_FIELD_ENTITY_TYPE_INVALID"
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_create(_custom_payload(field_code="Bad Code"))
    assert exc.value.code == "CUSTOM_FIELD_CODE_INVALID"
    with pytest.raises(ConfigError):
        validate_custom_field_create(_custom_payload(data_type="MONEY"))
    with pytest.raises(ConfigError):
        validate_custom_field_create(_custom_payload(label=""))
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_create(_custom_payload(data_type="SELECT"))
    assert exc.value.code == "CUSTOM_FIELD_OPTIONS_REQUIRED"
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_create(_custom_payload(options=["one"]))
    assert exc.value.code == "CUSTOM_FIELD_OPTIONS_INVALID"
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_create(_custom_payload(data_type="SELECT", options=["a", "a"]))
    assert exc.value.code == "CUSTOM_FIELD_OPTIONS_INVALID"


def test_custom_field_validation_payload_shape() -> None:
    values = validate_custom_field_create(
        _custom_payload(validation={"min": 0, "max": 120, "regex": "^[0-9]+$"})
    )
    assert values["validation"] == {"min": 0, "max": 120, "regex": "^[0-9]+$"}
    for bad in ({"unknown": 1}, {"min": 5, "max": 1}, {"regex": "("}, {"max_length": "x"}):
        with pytest.raises(ConfigError) as exc:
            validate_custom_field_create(_custom_payload(validation=bad))
        assert exc.value.code == "CUSTOM_FIELD_VALIDATION_INVALID"


def test_custom_field_patch_immutable_type_and_option_rules() -> None:
    existing = {
        "data_type": "SELECT",
        "label": "Warranty months",
        "options": ["one"],
        "is_required": False,
        "is_searchable": False,
        "validation": None,
    }
    assert validate_custom_field_patch(existing, {"label": "Coverage"}) == {"label": "Coverage"}
    assert validate_custom_field_patch(existing, {"options": ["a", "b"]}) == {"options": ["a", "b"]}
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_patch(existing, {"data_type": "TEXT"})
    assert exc.value.code == "CUSTOM_FIELD_TYPE_IMMUTABLE"
    # Same data_type explicitly present is allowed (no-op for the immutable field).
    assert validate_custom_field_patch(existing, {"data_type": "select", "label": "New"}) == {"label": "New"}
    with pytest.raises(ConfigError):
        validate_custom_field_patch(existing, {})
    text_existing = {**existing, "data_type": "TEXT", "options": None}
    with pytest.raises(ConfigError) as exc:
        validate_custom_field_patch(text_existing, {"options": ["x"]})
    assert exc.value.code == "CUSTOM_FIELD_OPTIONS_INVALID"


def test_custom_field_change_required_is_merge_aware() -> None:
    existing = {"label": "Old", "options": None, "is_required": False}
    assert custom_field_change_required(existing, {"label": "Old"}) is False
    assert custom_field_change_required(existing, {"label": "New"}) is True
    assert custom_field_change_required(existing, {"is_required": False}) is False


# -------------------------------------------------------------------------- flags -------------
def test_flag_key_and_patch_validation() -> None:
    assert validate_flag_key("inventory.ledger.enabled") == "inventory.ledger.enabled"
    for bad in ("invalid", "Inventory.Ledger.Enabled", "a..b", "", None):
        with pytest.raises(ConfigError) as exc:
            validate_flag_key(bad)
        assert exc.value.code == "FLAG_KEY_INVALID"
    assert validate_flag_patch({"is_enabled": True}) == {"is_enabled": True, "notes": None}
    assert validate_flag_patch({"is_enabled": False, "notes": "kill switch"})["notes"] == "kill switch"
    with pytest.raises(ConfigError):
        validate_flag_patch({"notes": "missing is_enabled"})
    with pytest.raises(ConfigError):
        validate_flag_patch({"is_enabled": 1})


def test_flag_change_values_insert_change_and_noop() -> None:
    inserted = flag_change_values(None, {"is_enabled": True, "notes": "enable"}, actor_id="actor-1")
    assert inserted is not None
    assert inserted["is_enabled"] is True
    assert inserted["source"] == "OVERRIDE"
    assert inserted["changed_by"] == "actor-1"
    assert inserted["changed_at"] is not None

    existing = {"is_enabled": True, "notes": "enable"}
    assert flag_change_values(existing, {"is_enabled": True, "notes": "enable"}, actor_id="actor-1") is None
    changed = flag_change_values(existing, {"is_enabled": False, "notes": None}, actor_id="actor-2")
    assert changed is not None and changed["is_enabled"] is False and changed["notes"] is None
    notes_only = flag_change_values(existing, {"is_enabled": True, "notes": "updated"}, actor_id="actor-2")
    assert notes_only is not None and notes_only["notes"] == "updated"


# --------------------------------------------------------------- sequences / If-Match --------
def test_sequence_filter_validation() -> None:
    assert validate_sequence_filter(None) is None
    assert validate_sequence_filter("PO") == "PO"
    for bad in ("po", "p0", "", 12):
        with pytest.raises(ConfigError):
            validate_sequence_filter(bad)


def test_parse_if_match() -> None:
    assert parse_if_match(None) is None
    assert parse_if_match("") is None
    assert parse_if_match("*") is None
    assert parse_if_match("3") == 3
    assert parse_if_match('"7"') == 7
    with pytest.raises(ConfigError) as exc:
        parse_if_match("abc")
    assert exc.value.code == "PRECONDITION_INVALID"


def test_config_error_status_codes() -> None:
    assert ConfigError("x").status_code == 422
    assert ConfigConflict("x").status_code == 409
    assert ConfigStale("x").status_code == 412


def test_fiscal_closed_at_is_timezone_aware() -> None:
    values = validate_fiscal_period_create(
        {"period_key": "2026-11", "start_date": "2026-11-01", "end_date": "2026-11-30", "status": "LOCKED"}
    )
    assert values["closed_at"].tzinfo == UTC
