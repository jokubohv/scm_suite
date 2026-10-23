"""Assertion helpers shared by unit/invariant/contract suites.

Rules encoded here (from the plan): quantities and money are decimal strings on the wire
(``NUMERIC(38,12)``), timestamps are UTC ISO-8601 with ``Z``, ids are UUIDs, errors use the platform
envelope, and RBAC denials are ``403 FORBIDDEN_SCOPE``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

QUANTITY_RE = re.compile(r"^-?\d{1,38}(?:\.\d{1,12})?$")
MONEY_RE = re.compile(r"^-?\d{1,38}(?:\.\d{1,12})?$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
DECIMAL_12 = Decimal("0.000000000001")


class AssertionDetailError(AssertionError):
    """Assertion failure carrying a short machine-readable detail string."""


def as_decimal(value: Decimal | int | str | float | None) -> Decimal:
    """Convert numeric input to Decimal WITHOUT going through float when avoidable."""
    if value is None:
        raise AssertionDetailError("expected a number, got None")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise AssertionDetailError(f"not a decimal value: {value!r}") from exc


def assert_decimal_equal(
    actual: Decimal | int | str | float,
    expected: Decimal | int | str | float,
    *,
    tolerance: Decimal | str = DECIMAL_12,
    label: str = "value",
) -> None:
    """Assert two decimals are equal within ``tolerance`` (default 1e-12, no drift)."""
    actual_d, expected_d, tolerance_d = as_decimal(actual), as_decimal(expected), as_decimal(tolerance)
    delta = abs(actual_d - expected_d)
    if delta > tolerance_d:
        raise AssertionDetailError(f"{label}: {actual_d} != {expected_d} (delta {delta} > {tolerance_d})")


def assert_quantity_string(value: Any, *, label: str = "quantity") -> None:
    if not isinstance(value, str) or not QUANTITY_RE.match(value):
        raise AssertionDetailError(f"{label} must be a decimal string with <=12 dp, got {value!r}")


def assert_money_string(value: Any, *, label: str = "amount") -> None:
    if not isinstance(value, str) or not MONEY_RE.match(value):
        raise AssertionDetailError(f"{label} must be a decimal string, got {value!r}")


def assert_utc_timestamp(value: Any, *, label: str = "timestamp") -> None:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.match(value):
        raise AssertionDetailError(f"{label} must be an ISO-8601 UTC timestamp ending in Z, got {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise AssertionDetailError(f"{label} must be UTC, got {value!r}")


def assert_uuid7(value: Any, *, label: str = "id") -> None:
    if isinstance(value, UUID):
        text = str(value)
    else:
        text = str(value)
    if not UUID_RE.match(text):
        raise AssertionDetailError(f"{label} must be a UUIDv7 string, got {value!r}")


def assert_contains_keys(payload: Mapping[str, Any], keys: Iterable[str], *, label: str = "payload") -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise AssertionDetailError(f"{label} missing keys: {missing}")


def assert_error_envelope(
    payload: Mapping[str, Any],
    *,
    code: str | None = None,
    status: int | None = None,
    label: str = "error",
) -> None:
    """Validate the platform error envelope (``code``, ``message``, ``details``, ``correlation_id``)."""
    required = ("code", "message", "details", "correlation_id")
    assert_contains_keys(payload, required, label=label)
    if code is not None and payload["code"] != code:
        raise AssertionDetailError(f"{label}.code: {payload['code']!r} != {code!r}")
    if status is not None:
        for key in ("status", "status_code"):
            if key in payload and payload[key] != status:
                raise AssertionDetailError(f"{label}.{key}: {payload[key]!r} != {status!r}")


def assert_scope_denied(response, *, scope: str | None = None) -> None:
    """Assert an HTTP response is an RBAC scope denial (403 or 404 for cross-tenant probes)."""
    if response.status_code not in (403, 404):
        raise AssertionDetailError(f"expected 403/404 scope denial, got {response.status_code}")
    body = _json_body(response)
    if response.status_code == 403:
        assert_error_envelope(body)
        if body.get("code") != "FORBIDDEN_SCOPE":
            raise AssertionDetailError(f"expected FORBIDDEN_SCOPE, got {body.get('code')!r}")
        if scope is not None and scope not in str(body.get("details", {})):
            raise AssertionDetailError(f"expected denied scope {scope!r} in error details")


def assert_sorted_unique(values: Iterable[Any], *, label: str = "values") -> None:
    items = list(values)
    if items != sorted(items):
        raise AssertionDetailError(f"{label} must be sorted")
    if len(set(items)) != len(items):
        raise AssertionDetailError(f"{label} must be unique")


def _json_body(response) -> dict[str, Any]:
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001 - test helper
        raise AssertionDetailError(f"response is not JSON: {exc}") from exc
