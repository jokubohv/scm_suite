"""Unit tests for the platform-admin tenant validators/transition matrix (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.repositories.platform_admin import (
    ALLOWED_TRANSITIONS,
    PATCHABLE_FIELDS,
    TenantTransitionError,
    TenantValidationError,
    decode_cursor,
    encode_cursor,
    is_system_tenant,
    normalise_code,
    payload_hash,
    settings_summary,
    tenant_view,
    transition_audit_action,
    validate_create,
    validate_idempotency_key,
    validate_name,
    validate_patch,
    validate_reason,
    validate_status_transition,
    validate_timezone,
)

SYSTEM_ID = "00000000-0000-0000-0000-000000000000"


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "acme",
        "name": "Acme Corporation",
        "timezone": "UTC",
        "base_currency_code": "usd",
        "negative_inventory_policy": "BLOCK",
        "rounding_mode": "PER_MOVEMENT",
        "retention_ledger_years": 7,
    }
    payload.update(overrides)
    return payload


class TestCodeAndNameValidation:
    def test_normalise_code_upper_cases_and_strips(self) -> None:
        assert normalise_code("  acme-1 ") == "ACME-1"

    @pytest.mark.parametrize("code", ["acme", "ACME", "ACME_1", "A1-B2", "ab"])
    def test_valid_codes(self, code: str) -> None:
        assert validate_create(_create_payload(code=code))["code"] == code.upper()

    @pytest.mark.parametrize("code", ["1ACME", "-ACME", "AC ME", "A", "", "A" * 41])
    def test_invalid_codes(self, code: str) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(code=code))

    def test_name_is_whitespace_collapsed_and_bounded(self) -> None:
        assert validate_name("  Acme   Corporation ") == "Acme Corporation"
        with pytest.raises(TenantValidationError):
            validate_name("   ")
        with pytest.raises(TenantValidationError):
            validate_name("x" * 201)

    def test_reason_bounds(self) -> None:
        assert validate_reason("  customer   request ") == "customer request"
        with pytest.raises(TenantValidationError):
            validate_reason("no")


class TestCreateValidation:
    def test_defaults_are_applied(self) -> None:
        data = validate_create({"code": "acme", "name": "Acme"})
        assert data["status"] == "ACTIVE"
        assert data["timezone"] == "UTC"
        assert data["negative_inventory_policy"] == "BLOCK"
        assert data["rounding_mode"] == "PER_MOVEMENT"
        assert data["retention_ledger_years"] == 7
        assert data["settings"] == {}
        assert data["base_currency_code"] is None

    def test_settings_defaults_merge(self) -> None:
        data = validate_create(
            {"code": "acme", "name": "Acme", "settings": {"a": 1}, "settings_defaults": {"a": 2, "b": 3}}
        )
        assert data["settings"] == {"a": 1, "b": 3}

    def test_bootstrap_email_is_normalised(self) -> None:
        data = validate_create(_create_payload(bootstrap_admin_email=" Admin@ACME.TEST "))
        assert data["bootstrap_admin_email"] == "admin@acme.test"

    @pytest.mark.parametrize("email", ["not-an-email", "a@b", "@acme.test"])
    def test_bad_bootstrap_email(self, email: str) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(bootstrap_admin_email=email))

    def test_offboarding_cannot_be_provisioned(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(status="OFFBOARDING"))

    def test_invalid_policy_and_rounding(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(negative_inventory_policy="MAYBE"))
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(rounding_mode="BANKERS"))

    def test_invalid_retention(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(retention_ledger_years=0))
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(retention_ledger_years=51))

    def test_settings_must_be_object(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(settings=["not", "a", "mapping"]))

    def test_invalid_timezone_and_currency(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(timezone="Mars/Olympus_Mons"))
        with pytest.raises(TenantValidationError):
            validate_create(_create_payload(base_currency_code="US"))
        assert validate_timezone("America/New_York") == "America/New_York"


class TestPatchValidation:
    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_patch({"code": "NEW"}, current_status="ACTIVE")
        assert "code" not in PATCHABLE_FIELDS

    def test_empty_patch_rejected(self) -> None:
        with pytest.raises(TenantValidationError):
            validate_patch({}, current_status="ACTIVE")

    def test_profile_fields_are_normalised(self) -> None:
        patch = validate_patch(
            {
                "name": "  New  Name ",
                "timezone": "America/New_York",
                "base_currency_code": "eur",
                "negative_inventory_policy": "warn",
                "rounding_mode": "per_transaction",
                "retention_ledger_years": 10,
            },
            current_status="ACTIVE",
        )
        assert patch["name"] == "New Name"
        assert patch["base_currency_code"] == "EUR"
        assert patch["negative_inventory_policy"] == "WARN"
        assert patch["rounding_mode"] == "PER_TRANSACTION"

    def test_status_transition_patch_validated(self) -> None:
        assert validate_patch({"status": "SUSPENDED"}, current_status="ACTIVE") == {"status": "SUSPENDED"}
        with pytest.raises(TenantTransitionError):
            validate_patch({"status": "ACTIVE"}, current_status="OFFBOARDING")

    def test_same_status_allowed_for_idempotent_patch(self) -> None:
        assert validate_patch({"status": "ACTIVE"}, current_status="ACTIVE") == {"status": "ACTIVE"}


class TestTransitionMatrix:
    @pytest.mark.parametrize(
        ("current", "target", "allowed"),
        [
            ("ACTIVE", "SUSPENDED", True),
            ("ACTIVE", "OFFBOARDING", True),
            ("SUSPENDED", "ACTIVE", True),
            ("SUSPENDED", "OFFBOARDING", True),
            ("ACTIVE", "ACTIVE", True),
            ("SUSPENDED", "SUSPENDED", True),
            ("OFFBOARDING", "ACTIVE", False),
            ("OFFBOARDING", "SUSPENDED", False),
            ("OFFBOARDING", "OFFBOARDING", True),
            ("ACTIVE", "UNKNOWN", False),
        ],
    )
    def test_transitions(self, current: str, target: str, allowed: bool) -> None:
        if allowed:
            validate_status_transition(current, target)
        else:
            with pytest.raises(TenantTransitionError):
                validate_status_transition(current, target)

    def test_terminal_offboarding_has_no_targets(self) -> None:
        assert ALLOWED_TRANSITIONS["OFFBOARDING"] == frozenset()

    def test_audit_actions(self) -> None:
        assert transition_audit_action("ACTIVE", "SUSPENDED") == "tenant.suspended"
        assert transition_audit_action("SUSPENDED", "ACTIVE") == "tenant.reactivated"
        assert transition_audit_action("ACTIVE", "OFFBOARDING") == "tenant.offboarded"
        assert transition_audit_action("ACTIVE", "ACTIVE") is None


class TestCursorAndSerialisation:
    def test_cursor_roundtrip(self) -> None:
        created_at = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
        row_id = uuid4()
        decoded_at, decoded_id = decode_cursor(encode_cursor(created_at, row_id))
        assert decoded_at == created_at
        assert decoded_id == row_id

    @pytest.mark.parametrize("cursor", ["", "!!!", "e30", "bm90LWpzb24"])
    def test_invalid_cursor(self, cursor: str) -> None:
        with pytest.raises(TenantValidationError):
            decode_cursor(cursor)

    def test_tenant_view_and_settings_summary(self) -> None:
        row_id = uuid4()
        row = {
            "id": row_id,
            "tenant_id": row_id,
            "code": "ACME",
            "name": "Acme",
            "base_currency_code": "USD",
            "timezone": "UTC",
            "status": "ACTIVE",
            "negative_inventory_policy": "BLOCK",
            "rounding_mode": "PER_MOVEMENT",
            "retention_ledger_years": 7,
            "settings": {"rbac.sod_enforcement": "HARD"},
            "version": 2,
            "created_at": datetime(2026, 10, 23, tzinfo=UTC),
            "updated_at": datetime(2026, 10, 23, tzinfo=UTC),
            "created_by": None,
            "updated_by": None,
        }
        view = tenant_view(row)
        assert view["is_system"] is False
        assert view["settings"] == {"rbac.sod_enforcement": "HARD"}
        assert view["id"] == str(row_id)
        assert view["created_at"] == "2026-10-23T00:00:00+00:00"
        summary = settings_summary(row)
        assert summary["settings_keys"] == ["rbac.sod_enforcement"]

    def test_is_system_tenant(self) -> None:
        assert is_system_tenant(SYSTEM_ID) is True
        assert is_system_tenant(uuid4()) is False


class TestIdempotencyHelpers:
    def test_key_validation(self) -> None:
        assert validate_idempotency_key("abc-123:KEY") == "abc-123:KEY"
        for key in ("", " bad", "x" * 256, "bad key"):
            with pytest.raises(TenantValidationError):
                validate_idempotency_key(key)

    def test_payload_hash_is_deterministic_and_order_insensitive(self) -> None:
        left = payload_hash({"b": 2, "a": 1})
        right = payload_hash({"a": 1, "b": 2})
        assert left == right
        assert left != payload_hash({"a": 1, "b": 3})
