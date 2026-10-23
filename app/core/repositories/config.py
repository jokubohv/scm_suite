"""FS-00 reference/configuration repository (task-41).

Async SQLAlchemy 2.0 Core access to the platform reference/configuration tables created by migrations
0001–0003. The repository is **policy-free about HTTP** (the router renders problem+json) and
**tenant-scoped by construction** for tenant-owned tables; ``platform.currency`` is global reference
data (``tenant_id IS NULL`` rows) per DATA-MODEL §3 / FS-00 §4.

Tables in scope
---------------
``platform.currency``, ``platform.fiscal_period``, ``platform.custom_field_def``,
``platform.feature_flag``, ``platform.number_sequence``.

This module also owns the *pure* validation/merge helpers used by the API and unit-tested in
``app/tests/unit/services_config``:

* currency create/patch validation (ISO-4217 shape, minor units, numeric code),
* fiscal-period creation validation, overlap detection (DM-PLAT-08) and status-transition guard,
* custom-field definition validation (``data_type`` closed enum, SELECT options, code/label/validation
  shape) and immutable-``data_type`` patch merge,
* feature-flag key/patch validation + idempotent change detection (``flag_change_values`` returns
  ``None`` when the patch is a no-op, so no audit/event is emitted),
* sequence read filters and row serialisation.

Every write appends a ``platform.audit_event`` row; feature-flag changes additionally append a
transactional ``platform.outbox_event`` (``platform.flag.changed`` v1) per DM-PLAT-05.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p

# ----------------------------------------------------------------------------------------------
# Domain constants / error model
# ----------------------------------------------------------------------------------------------
CURRENCY_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")
CUSTOM_FIELD_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
FLAG_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PERIOD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
DOC_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")

CUSTOM_FIELD_DATA_TYPES = frozenset({"TEXT", "NUMBER", "DATE", "BOOL", "SELECT"})
CUSTOM_FIELD_ENTITY_TYPES = frozenset({"ITEM", "LOT", "SERIAL", "WORK_ORDER"})
FISCAL_PERIOD_STATUSES = frozenset({"OPEN", "CLOSED", "LOCKED"})
RESET_POLICIES = frozenset({"NEVER", "ANNUAL", "MONTHLY"})
GAP_POLICIES = frozenset({"NON_GAPLESS", "GAPLESS"})
MAX_SELECT_OPTIONS = 200

FLAG_CHANGED_EVENT = "platform.flag.changed"
FLAG_AGGREGATE_TYPE = "feature_flag"

# Closed sets used by the legacy compact currency codes (kept for validation symmetry).
ISO_NUMERIC_MIN = 1
ISO_NUMERIC_MAX = 999
MINOR_UNITS_MIN = 0
MINOR_UNITS_MAX = 4


class ConfigError(ValueError):
    """Domain error mapped to a problem+json response by the router (HTTP-free here)."""

    code: str = "CONFIG_INVALID"
    status_code: int = 422

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        if code:
            self.code = code
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class ConfigNotFound(ConfigError):
    code = "NOT_FOUND"
    status_code = 404


class ConfigConflict(ConfigError):
    code = "CONFLICT"
    status_code = 409


class ConfigStale(ConfigError):
    """``If-Match`` preconditions failed (HTTP 412)."""

    code = "PRECONDITION_FAILED"
    status_code = 412


# ----------------------------------------------------------------------------------------------
# Pure validation helpers (unit-tested without a database)
# ----------------------------------------------------------------------------------------------
def _require_mapping(payload: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    if payload is None:
        raise ConfigError(f"{label} payload is required", code="VALIDATION_ERROR")
    if not isinstance(payload, Mapping):
        raise ConfigError(f"{label} payload must be an object", code="VALIDATION_ERROR")
    return payload


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field} must be a boolean", code="VALIDATION_ERROR", details={"field": field})


def _as_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer", code="VALIDATION_ERROR", details={"field": field})
    if value < minimum or value > maximum:
        raise ConfigError(
            f"{field} must be between {minimum} and {maximum}",
            code="VALIDATION_ERROR",
            details={"field": field, "min": minimum, "max": maximum},
        )
    return value


def _as_optional_str(value: Any, field: str, *, max_length: int, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string", code="VALIDATION_ERROR", details={"field": field})
    if not allow_empty and not value.strip():
        raise ConfigError(f"{field} must not be empty", code="VALIDATION_ERROR", details={"field": field})
    if len(value) > max_length:
        raise ConfigError(
            f"{field} must be at most {max_length} characters",
            code="VALIDATION_ERROR",
            details={"field": field, "max_length": max_length},
        )
    return value


def _coerce_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(
                f"{field} must be an ISO date (YYYY-MM-DD)",
                code="VALIDATION_ERROR",
                details={"field": field},
            ) from exc
    raise ConfigError(f"{field} must be an ISO date (YYYY-MM-DD)", code="VALIDATION_ERROR", details={"field": field})


# ---- currency --------------------------------------------------------------------------------
def validate_currency_code(code: Any) -> str:
    if not isinstance(code, str) or not CURRENCY_CODE_PATTERN.match(code.strip().upper()):
        raise ConfigError(
            "currency code must be a 3-letter ISO-4217 code",
            code="CURRENCY_CODE_INVALID",
            details={"field": "code"},
        )
    return code.strip().upper()


def validate_currency_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(payload, "currency create")
    return {
        "code": validate_currency_code(payload.get("code")),
        "numeric_code": (
            None
            if payload.get("numeric_code") is None
            else _as_int(payload["numeric_code"], "numeric_code", minimum=ISO_NUMERIC_MIN, maximum=ISO_NUMERIC_MAX)
        ),
        "minor_units": (
            2
            if payload.get("minor_units") is None
            else _as_int(payload["minor_units"], "minor_units", minimum=MINOR_UNITS_MIN, maximum=MINOR_UNITS_MAX)
        ),
        "is_active": True if payload.get("is_active") is None else _as_bool(payload["is_active"], "is_active"),
    }


def validate_currency_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(payload, "currency patch")
    if "code" in payload:
        raise ConfigError(
            "currency code is the primary key and cannot be changed",
            code="CURRENCY_CODE_IMMUTABLE",
            details={"field": "code"},
        )
    changes: dict[str, Any] = {}
    if "numeric_code" in payload:
        value = payload["numeric_code"]
        changes["numeric_code"] = (
            None
            if value is None
            else _as_int(value, "numeric_code", minimum=ISO_NUMERIC_MIN, maximum=ISO_NUMERIC_MAX)
        )
    if "minor_units" in payload:
        changes["minor_units"] = _as_int(
            payload["minor_units"], "minor_units", minimum=MINOR_UNITS_MIN, maximum=MINOR_UNITS_MAX
        )
    if "is_active" in payload:
        changes["is_active"] = _as_bool(payload["is_active"], "is_active")
    if not changes:
        raise ConfigError(
            "at least one mutable currency field is required (numeric_code, minor_units, is_active)",
            code="VALIDATION_ERROR",
        )
    return changes


# ---- fiscal periods --------------------------------------------------------------------------
def validate_fiscal_period_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(payload, "fiscal period create")
    raw_key = payload.get("period_key")
    if not isinstance(raw_key, str) or not PERIOD_KEY_PATTERN.match(raw_key):
        raise ConfigError(
            "period_key must match an accounting period key (e.g. 2026-10)",
            code="PERIOD_KEY_INVALID",
            details={"field": "period_key"},
        )
    start = _coerce_date(payload.get("start_date"), "start_date")
    end = _coerce_date(payload.get("end_date"), "end_date")
    if start > end:
        raise ConfigError(
            "start_date must be on or before end_date",
            code="PERIOD_DATES_INVALID",
            details={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
    status = str(payload.get("status", "OPEN")).upper()
    if status not in FISCAL_PERIOD_STATUSES:
        raise ConfigError(
            f"status must be one of {sorted(FISCAL_PERIOD_STATUSES)}",
            code="PERIOD_STATUS_INVALID",
            details={"field": "status"},
        )
    source_version = _as_int(
        payload.get("source_version", 1), "source_version", minimum=1, maximum=2_147_483_647
    )
    return {
        "period_key": raw_key,
        "start_date": start,
        "end_date": end,
        "status": status,
        "source_version": source_version,
        "closed_at": datetime.now(UTC) if status in {"CLOSED", "LOCKED"} else None,
    }


def periods_overlap(
    start_a: date, end_a: date, start_b: date, end_b: date
) -> bool:
    """Half-open-safe inclusive overlap test for fiscal periods (DM-PLAT-08)."""
    return start_a <= end_b and start_b <= end_a


def assert_period_transition_guarded(
    *,
    current_status: str,
    new_status: str,
    current_source_version: int,
    new_source_version: int,
) -> None:
    """DM-PLAT-08 guard: CLOSED/LOCKED cannot reopen without a newer finance ``source_version``."""
    if current_status not in FISCAL_PERIOD_STATUSES or new_status not in FISCAL_PERIOD_STATUSES:
        raise ConfigError("unknown fiscal period status", code="PERIOD_STATUS_INVALID")
    if current_status == new_status:
        return
    allowed = {
        "OPEN": {"CLOSED", "LOCKED"},
        "CLOSED": {"LOCKED", "OPEN"},
        "LOCKED": {"OPEN"},
    }
    if new_status not in allowed[current_status]:
        raise ConfigConflict(
            f"fiscal period transition {current_status}->{new_status} is not permitted",
            code="PERIOD_TRANSITION_INVALID",
        )
    if current_status in {"CLOSED", "LOCKED"} and new_status == "OPEN":
        if new_source_version <= current_source_version:
            raise ConfigConflict(
                "reopening a closed/locked period requires a newer source_version from finance",
                code="PERIOD_REOPEN_REQUIRES_SOURCE_VERSION",
                details={
                    "current_source_version": current_source_version,
                    "required_source_version_gt": current_source_version,
                },
            )


# ---- custom-field definitions ----------------------------------------------------------------
def _validate_validation_payload(validation: Any) -> dict[str, Any] | None:
    if validation is None:
        return None
    if not isinstance(validation, Mapping):
        raise ConfigError(
            "validation must be a JSON object",
            code="CUSTOM_FIELD_VALIDATION_INVALID",
            details={"field": "validation"},
        )
    normalized = dict(validation)
    allowed = {"min", "max", "max_length", "min_length", "regex", "allowed_values"}
    unknown = sorted(set(normalized) - allowed)
    if unknown:
        raise ConfigError(
            f"unsupported validation keys: {unknown}",
            code="CUSTOM_FIELD_VALIDATION_INVALID",
            details={"unsupported_keys": unknown, "allowed_keys": sorted(allowed)},
        )
    if "min" in normalized or "max" in normalized:
        for key in ("min", "max"):
            if key in normalized and not isinstance(normalized[key], int | float):
                raise ConfigError(
                    f"validation.{key} must be numeric",
                    code="CUSTOM_FIELD_VALIDATION_INVALID",
                    details={"field": f"validation.{key}"},
                )
        if "min" in normalized and "max" in normalized and normalized["min"] > normalized["max"]:
            raise ConfigError(
                "validation.min must be <= validation.max",
                code="CUSTOM_FIELD_VALIDATION_INVALID",
            )
    if "regex" in normalized:
        if not isinstance(normalized["regex"], str):
            raise ConfigError(
                "validation.regex must be a string", code="CUSTOM_FIELD_VALIDATION_INVALID"
            )
        try:
            re.compile(normalized["regex"])
        except re.error as exc:
            raise ConfigError(
                f"validation.regex is not a valid regular expression: {exc}",
                code="CUSTOM_FIELD_VALIDATION_INVALID",
            ) from exc
    for key in ("min_length", "max_length"):
        if key in normalized:
            if not isinstance(normalized[key], int) or isinstance(normalized[key], bool) or normalized[key] < 0:
                raise ConfigError(
                    f"validation.{key} must be a non-negative integer",
                    code="CUSTOM_FIELD_VALIDATION_INVALID",
                )
    if "min_length" in normalized and "max_length" in normalized:
        if normalized["min_length"] > normalized["max_length"]:
            raise ConfigError(
                "validation.min_length must be <= validation.max_length",
                code="CUSTOM_FIELD_VALIDATION_INVALID",
            )
    if "allowed_values" in normalized:
        values = normalized["allowed_values"]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ConfigError(
                "validation.allowed_values must be a list of strings",
                code="CUSTOM_FIELD_VALIDATION_INVALID",
            )
    return normalized


def _validate_options(data_type: str, options: Any) -> list[str] | None:
    if data_type != "SELECT":
        if options is not None:
            raise ConfigError(
                "options are only allowed for data_type=SELECT",
                code="CUSTOM_FIELD_OPTIONS_INVALID",
            )
        return None
    if not isinstance(options, list | tuple) or not options:
        raise ConfigError(
            "SELECT custom fields require a non-empty options list",
            code="CUSTOM_FIELD_OPTIONS_REQUIRED",
        )
    if len(options) > MAX_SELECT_OPTIONS:
        raise ConfigError(
            f"at most {MAX_SELECT_OPTIONS} options are allowed",
            code="CUSTOM_FIELD_OPTIONS_INVALID",
            details={"max_options": MAX_SELECT_OPTIONS},
        )
    normalized: list[str] = []
    for option in options:
        if not isinstance(option, str) or not option.strip():
            raise ConfigError(
                "options must be non-empty strings", code="CUSTOM_FIELD_OPTIONS_INVALID"
            )
        if option in normalized:
            raise ConfigError(
                f"duplicate option {option!r}", code="CUSTOM_FIELD_OPTIONS_INVALID"
            )
        normalized.append(option)
    return normalized


def validate_custom_field_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(payload, "custom field create")
    entity_type = str(payload.get("entity_type", "")).upper()
    if entity_type not in CUSTOM_FIELD_ENTITY_TYPES:
        raise ConfigError(
            f"entity_type must be one of {sorted(CUSTOM_FIELD_ENTITY_TYPES)}",
            code="CUSTOM_FIELD_ENTITY_TYPE_INVALID",
            details={"field": "entity_type", "allowed": sorted(CUSTOM_FIELD_ENTITY_TYPES)},
        )
    field_code = payload.get("field_code")
    if not isinstance(field_code, str) or not CUSTOM_FIELD_CODE_PATTERN.match(field_code):
        raise ConfigError(
            "field_code must be snake_case, start with a letter and be 2-64 chars",
            code="CUSTOM_FIELD_CODE_INVALID",
            details={"field": "field_code"},
        )
    label = _as_optional_str(payload.get("label"), "label", max_length=255)
    if label is None:
        raise ConfigError("label is required", code="VALIDATION_ERROR", details={"field": "label"})
    data_type = str(payload.get("data_type", "")).upper()
    if data_type not in CUSTOM_FIELD_DATA_TYPES:
        raise ConfigError(
            f"data_type must be one of {sorted(CUSTOM_FIELD_DATA_TYPES)}",
            code="CUSTOM_FIELD_TYPE_INVALID",
            details={"field": "data_type", "allowed": sorted(CUSTOM_FIELD_DATA_TYPES)},
        )
    return {
        "entity_type": entity_type,
        "field_code": field_code,
        "label": label,
        "data_type": data_type,
        "options": _validate_options(data_type, payload.get("options")),
        "is_required": (
            False if payload.get("is_required") is None else _as_bool(payload["is_required"], "is_required")
        ),
        "is_searchable": (
            False if payload.get("is_searchable") is None else _as_bool(payload["is_searchable"], "is_searchable")
        ),
        "validation": _validate_validation_payload(payload.get("validation")),
    }


def validate_custom_field_patch(
    existing: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge a patch onto an existing definition; ``data_type`` is immutable (FS-00 §6 row)."""
    payload = _require_mapping(payload, "custom field patch")
    changes: dict[str, Any] = {}
    if "data_type" in payload and str(payload["data_type"]).upper() != str(existing["data_type"]).upper():
        raise ConfigError(
            "data_type is immutable after creation",
            code="CUSTOM_FIELD_TYPE_IMMUTABLE",
            details={"data_type": existing["data_type"]},
        )
    effective_type = str(existing["data_type"]).upper()
    if "label" in payload:
        label = _as_optional_str(payload["label"], "label", max_length=255)
        if label is None:
            raise ConfigError("label must not be null", code="VALIDATION_ERROR", details={"field": "label"})
        changes["label"] = label
    if "options" in payload:
        changes["options"] = _validate_options(effective_type, payload["options"])
    if "is_required" in payload:
        changes["is_required"] = _as_bool(payload["is_required"], "is_required")
    if "is_searchable" in payload:
        changes["is_searchable"] = _as_bool(payload["is_searchable"], "is_searchable")
    if "validation" in payload:
        changes["validation"] = _validate_validation_payload(payload["validation"])
    if not changes:
        raise ConfigError(
            "at least one mutable custom-field field is required",
            code="VALIDATION_ERROR",
        )
    return changes


def custom_field_change_required(existing: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    """Idempotency helper: True when the merged values differ from the stored definition."""
    for key, value in changes.items():
        if existing.get(key) != value:
            return True
    return False


# ---- feature flags ---------------------------------------------------------------------------
def validate_flag_key(flag_key: Any) -> str:
    if not isinstance(flag_key, str) or not FLAG_KEY_PATTERN.match(flag_key) or len(flag_key) > 128:
        raise ConfigError(
            "flag_key must be a dotted lower_snake_case key (e.g. inventory.ledger.enabled)",
            code="FLAG_KEY_INVALID",
            details={"field": "flag_key"},
        )
    return flag_key


def validate_flag_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(payload, "feature flag patch")
    if "is_enabled" not in payload:
        raise ConfigError("is_enabled is required", code="VALIDATION_ERROR", details={"field": "is_enabled"})
    return {
        "is_enabled": _as_bool(payload["is_enabled"], "is_enabled"),
        "notes": _as_optional_str(payload.get("notes"), "notes", max_length=500, allow_empty=True),
    }


def flag_change_values(
    existing: Mapping[str, Any] | None,
    patch: Mapping[str, Any],
    *,
    actor_id: str | UUID | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Values to persist for a flag patch, or ``None`` when the patch is a no-op (idempotent).

    A missing row always yields values (INSERT). An existing row yields values only when
    ``is_enabled`` or ``notes`` change; the caller then writes one audit row + one outbox event.
    """
    validated = validate_flag_patch(patch)
    if existing is None:
        return {
            "is_enabled": validated["is_enabled"],
            "notes": validated["notes"],
            "source": "OVERRIDE",
            "changed_by": actor_id,
            "changed_at": now or datetime.now(UTC),
        }
    if existing.get("is_enabled") == validated["is_enabled"] and existing.get("notes") == validated["notes"]:
        return None
    return {
        "is_enabled": validated["is_enabled"],
        "notes": validated["notes"],
        "source": "OVERRIDE",
        "changed_by": actor_id,
        "changed_at": now or datetime.now(UTC),
    }


# ---- number sequences ------------------------------------------------------------------------
def validate_sequence_filter(doc_type: Any) -> str | None:
    if doc_type is None:
        return None
    if not isinstance(doc_type, str) or not DOC_TYPE_PATTERN.match(doc_type):
        raise ConfigError(
            "doc_type must be an uppercase document-type code",
            code="SEQUENCE_DOC_TYPE_INVALID",
            details={"field": "doc_type"},
        )
    return doc_type


# ---- serialisation helpers -------------------------------------------------------------------
def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def serialize_row(row: Mapping[str, Any], *, fields: Iterable[str] | None = None) -> dict[str, Any]:
    selected = fields or row.keys()
    return {key: _iso(row[key]) for key in selected if key in row}


CURRENCY_FIELDS = ("code", "numeric_code", "minor_units", "is_active", "version", "created_at", "updated_at")
FISCAL_PERIOD_FIELDS = (
    "id",
    "period_key",
    "start_date",
    "end_date",
    "status",
    "closed_at",
    "source_version",
    "version",
    "created_at",
    "updated_at",
)
CUSTOM_FIELD_FIELDS = (
    "id",
    "entity_type",
    "field_code",
    "label",
    "data_type",
    "options",
    "is_required",
    "is_searchable",
    "validation",
    "version",
    "created_at",
    "updated_at",
)
FEATURE_FLAG_FIELDS = (
    "id",
    "flag_key",
    "is_enabled",
    "source",
    "changed_by",
    "changed_at",
    "notes",
    "version",
    "created_at",
    "updated_at",
)
NUMBER_SEQUENCE_FIELDS = (
    "id",
    "doc_type",
    "scope_key",
    "prefix",
    "padding",
    "next_value",
    "reset_policy",
    "gap_policy",
    "period_key",
    "version",
    "created_at",
    "updated_at",
)


def serialize_currency(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = serialize_row(row, fields=CURRENCY_FIELDS)
    payload["tenant_scope"] = "GLOBAL"
    return payload


def serialize_fiscal_period(row: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_row(row, fields=FISCAL_PERIOD_FIELDS)


def serialize_custom_field(row: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_row(row, fields=CUSTOM_FIELD_FIELDS)


def serialize_feature_flag(row: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_row(row, fields=FEATURE_FLAG_FIELDS)


def serialize_number_sequence(row: Mapping[str, Any]) -> dict[str, Any]:
    return serialize_row(row, fields=NUMBER_SEQUENCE_FIELDS)


# ==============================================================================================
# Async repository
# ==============================================================================================
class ConfigRepository:
    """One request-scoped repository (no commits; the API dependency owns the transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- audit / outbox ----------
    async def audit(
        self,
        *,
        tenant_id: str | UUID,
        actor_id: str | UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | UUID | None,
        diff: Mapping[str, Any] | None = None,
        actor_type: str = "USER",
        source: str = "API",
    ) -> UUID:
        event_id = uuid4()
        await self.session.execute(
            insert(p.audit_event).values(
                id=event_id,
                tenant_id=tenant_id,
                event_id=event_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source=source,
                occurred_at=datetime.now(UTC),
            )
        )
        return event_id

    async def emit_event(
        self,
        *,
        tenant_id: str | UUID,
        aggregate_type: str,
        aggregate_id: str | UUID,
        aggregate_version: int,
        event_type: str,
        payload: Mapping[str, Any],
        event_version: str = "1",
    ) -> UUID:
        event_id = uuid4()
        await self.session.execute(
            insert(p.outbox_event).values(
                id=event_id,
                tenant_id=tenant_id,
                event_id=event_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_version=max(1, int(aggregate_version)),
                event_type=event_type,
                event_version=event_version,
                payload=dict(payload),
                occurred_at=datetime.now(UTC),
            )
        )
        return event_id

    # ------------------------------------------------------------------------- currencies -----
    async def list_currencies(self, *, is_active: bool | None = None) -> list[dict[str, Any]]:
        stmt = select(p.currency).where(p.currency.c.tenant_id.is_(None))
        if is_active is not None:
            stmt = stmt.where(p.currency.c.is_active.is_(is_active))
        rows = await self.session.execute(stmt.order_by(p.currency.c.code))
        return [serialize_currency(dict(row)) for row in rows.mappings().all()]

    async def get_currency(self, code: str) -> dict[str, Any] | None:
        row = await self.session.execute(
            select(p.currency).where(
                p.currency.c.code == validate_currency_code(code), p.currency.c.tenant_id.is_(None)
            )
        )
        mapping = row.mappings().first()
        return serialize_currency(dict(mapping)) if mapping else None

    async def create_currency(
        self, payload: Mapping[str, Any], *, actor_id: str | UUID | None, actor_tenant_id: str | UUID
    ) -> dict[str, Any]:
        values = validate_currency_create(payload)
        try:
            await self.session.execute(
                insert(p.currency).values(
                    code=values["code"],
                    tenant_id=None,
                    numeric_code=values["numeric_code"],
                    minor_units=values["minor_units"],
                    is_active=values["is_active"],
                )
            )
        except IntegrityError as exc:
            raise ConfigConflict(
                f"currency {values['code']!r} already exists", code="CURRENCY_EXISTS"
            ) from exc
        created = await self.get_currency(values["code"])
        if created is None:  # pragma: no cover - insert/read race only
            raise ConfigConflict("currency was not persisted", code="CURRENCY_EXISTS")
        await self.audit(
            tenant_id=actor_tenant_id,
            actor_id=actor_id,
            action="platform.currency.created",
            resource_type="currency",
            resource_id=values["code"],
            diff={"after": created},
        )
        return created

    async def update_currency(
        self,
        code: str,
        payload: Mapping[str, Any],
        *,
        actor_id: str | UUID | None,
        actor_tenant_id: str | UUID,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        normalized = validate_currency_code(code)
        current = await self.get_currency(normalized)
        if current is None:
            raise ConfigNotFound(f"currency {normalized!r} not found", code="CURRENCY_NOT_FOUND")
        _check_if_match(current.get("version"), expected_version, resource="currency")
        changes = validate_currency_patch(payload)
        await self.session.execute(
            update(p.currency)
            .where(p.currency.c.code == normalized, p.currency.c.tenant_id.is_(None))
            .values(**changes, version=p.currency.c.version + 1)
        )
        updated = await self.get_currency(normalized)
        if updated is None:  # pragma: no cover - update/read race only
            raise ConfigNotFound(f"currency {normalized!r} not found", code="CURRENCY_NOT_FOUND")
        await self.audit(
            tenant_id=actor_tenant_id,
            actor_id=actor_id,
            action="platform.currency.updated",
            resource_type="currency",
            resource_id=normalized,
            diff={"before": current, "after": updated},
        )
        return updated

    # --------------------------------------------------------------------- fiscal periods -----
    async def list_fiscal_periods(
        self,
        tenant_id: str | UUID,
        *,
        status: str | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(p.fiscal_period).where(p.fiscal_period.c.tenant_id == tenant_id)
        if status is not None:
            normalized = status.upper()
            if normalized not in FISCAL_PERIOD_STATUSES:
                raise ConfigError(
                    f"status must be one of {sorted(FISCAL_PERIOD_STATUSES)}",
                    code="PERIOD_STATUS_INVALID",
                )
            stmt = stmt.where(p.fiscal_period.c.status == normalized)
        if year is not None:
            if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200:
                raise ConfigError("year must be a four-digit integer", code="VALIDATION_ERROR")
            stmt = stmt.where(
                func.extract("year", p.fiscal_period.c.start_date) == year,
                func.extract("year", p.fiscal_period.c.end_date) == year,
            )
        rows = await self.session.execute(stmt.order_by(p.fiscal_period.c.start_date, p.fiscal_period.c.period_key))
        return [serialize_fiscal_period(dict(row)) for row in rows.mappings().all()]

    async def create_fiscal_period(
        self,
        tenant_id: str | UUID,
        payload: Mapping[str, Any],
        *,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        values = validate_fiscal_period_create(payload)
        overlapping = await self.session.execute(
            select(p.fiscal_period.c.period_key, p.fiscal_period.c.start_date, p.fiscal_period.c.end_date).where(
                p.fiscal_period.c.tenant_id == tenant_id,
                and_(
                    p.fiscal_period.c.start_date <= values["end_date"],
                    p.fiscal_period.c.end_date >= values["start_date"],
                ),
            )
        )
        overlap_rows = overlapping.mappings().all()
        if overlap_rows:
            raise ConfigConflict(
                "fiscal period overlaps an existing period (DM-PLAT-08)",
                code="PERIOD_OVERLAP",
                details={
                    "period_key": values["period_key"],
                    "overlaps": [dict(row) for row in overlap_rows],
                },
            )
        period_id = uuid4()
        try:
            await self.session.execute(
                insert(p.fiscal_period).values(
                    id=period_id,
                    tenant_id=tenant_id,
                    period_key=values["period_key"],
                    start_date=values["start_date"],
                    end_date=values["end_date"],
                    status=values["status"],
                    closed_at=values["closed_at"],
                    source_version=values["source_version"],
                )
            )
        except IntegrityError as exc:
            raise ConfigConflict(
                f"fiscal period {values['period_key']!r} already exists", code="PERIOD_EXISTS"
            ) from exc
        created = await self._get_fiscal_period(tenant_id, period_id)
        if created is None:  # pragma: no cover - insert/read race only
            raise ConfigConflict("fiscal period was not persisted", code="PERIOD_EXISTS")
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="platform.fiscal_period.created",
            resource_type="fiscal_period",
            resource_id=period_id,
            diff={"after": created},
        )
        return created

    async def _get_fiscal_period(self, tenant_id: str | UUID, period_id: str | UUID) -> dict[str, Any] | None:
        row = await self.session.execute(
            select(p.fiscal_period).where(
                p.fiscal_period.c.tenant_id == tenant_id, p.fiscal_period.c.id == period_id
            )
        )
        mapping = row.mappings().first()
        return serialize_fiscal_period(dict(mapping)) if mapping else None

    # --------------------------------------------------------------------- custom fields -----
    async def list_custom_fields(
        self, tenant_id: str | UUID, *, entity_type: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(p.custom_field_def).where(p.custom_field_def.c.tenant_id == tenant_id)
        if entity_type is not None:
            normalized = str(entity_type).upper()
            if normalized not in CUSTOM_FIELD_ENTITY_TYPES:
                raise ConfigError(
                    f"entity_type must be one of {sorted(CUSTOM_FIELD_ENTITY_TYPES)}",
                    code="CUSTOM_FIELD_ENTITY_TYPE_INVALID",
                )
            stmt = stmt.where(p.custom_field_def.c.entity_type == normalized)
        rows = await self.session.execute(
            stmt.order_by(p.custom_field_def.c.entity_type, p.custom_field_def.c.field_code)
        )
        return [serialize_custom_field(dict(row)) for row in rows.mappings().all()]

    async def get_custom_field(
        self, tenant_id: str | UUID, field_def_id: str | UUID
    ) -> dict[str, Any] | None:
        row = await self.session.execute(
            select(p.custom_field_def).where(
                p.custom_field_def.c.tenant_id == tenant_id,
                p.custom_field_def.c.id == field_def_id,
            )
        )
        mapping = row.mappings().first()
        return serialize_custom_field(dict(mapping)) if mapping else None

    async def create_custom_field(
        self,
        tenant_id: str | UUID,
        payload: Mapping[str, Any],
        *,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        values = validate_custom_field_create(payload)
        field_id = uuid4()
        try:
            await self.session.execute(
                insert(p.custom_field_def).values(id=field_id, tenant_id=tenant_id, **values)
            )
        except IntegrityError as exc:
            raise ConfigConflict(
                "custom field definition already exists for this (entity_type, field_code)",
                code="CUSTOM_FIELD_DUPLICATE",
            ) from exc
        created = await self.get_custom_field(tenant_id, field_id)
        if created is None:  # pragma: no cover - insert/read race only
            raise ConfigConflict("custom field was not persisted", code="CUSTOM_FIELD_DUPLICATE")
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="platform.custom_field.created",
            resource_type="custom_field_def",
            resource_id=field_id,
            diff={"after": created},
        )
        return created

    async def update_custom_field(
        self,
        tenant_id: str | UUID,
        field_def_id: str | UUID,
        payload: Mapping[str, Any],
        *,
        actor_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = await self.get_custom_field(tenant_id, field_def_id)
        if current is None:
            raise ConfigNotFound("custom field definition not found", code="CUSTOM_FIELD_NOT_FOUND")
        _check_if_match(current.get("version"), expected_version, resource="custom field")
        changes = validate_custom_field_patch(current, payload)
        if custom_field_change_required(current, changes):
            await self.session.execute(
                update(p.custom_field_def)
                .where(
                    p.custom_field_def.c.tenant_id == tenant_id,
                    p.custom_field_def.c.id == field_def_id,
                )
                .values(**changes, version=p.custom_field_def.c.version + 1)
            )
        updated = await self.get_custom_field(tenant_id, field_def_id)
        if updated is None:  # pragma: no cover - update/read race only
            raise ConfigNotFound("custom field definition not found", code="CUSTOM_FIELD_NOT_FOUND")
        if changes and custom_field_change_required(current, changes):
            await self.audit(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="platform.custom_field.updated",
                resource_type="custom_field_def",
                resource_id=field_def_id,
                diff={"before": current, "after": updated, "changes": {k: _iso(v) for k, v in changes.items()}},
            )
        return updated

    async def delete_custom_field(
        self,
        tenant_id: str | UUID,
        field_def_id: str | UUID,
        *,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        current = await self.get_custom_field(tenant_id, field_def_id)
        if current is None:
            raise ConfigNotFound("custom field definition not found", code="CUSTOM_FIELD_NOT_FOUND")
        in_use = await self.session.execute(
            select(func.count())
            .select_from(p.custom_field_value)
            .where(
                p.custom_field_value.c.tenant_id == tenant_id,
                p.custom_field_value.c.field_def_id == field_def_id,
            )
        )
        if int(in_use.scalar_one()) > 0:
            raise ConfigConflict(
                "custom field definition has stored values and cannot be deleted",
                code="CUSTOM_FIELD_IN_USE",
                details={"field_def_id": str(field_def_id)},
            )
        await self.session.execute(
            delete(p.custom_field_def).where(
                p.custom_field_def.c.tenant_id == tenant_id,
                p.custom_field_def.c.id == field_def_id,
            )
        )
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="platform.custom_field.deleted",
            resource_type="custom_field_def",
            resource_id=field_def_id,
            diff={"before": current},
        )
        return current

    # ---------------------------------------------------------------------- feature flags -----
    async def list_feature_flags(
        self, tenant_id: str | UUID, *, flag_key: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(p.feature_flag).where(p.feature_flag.c.tenant_id == tenant_id)
        if flag_key is not None:
            stmt = stmt.where(p.feature_flag.c.flag_key == validate_flag_key(flag_key))
        rows = await self.session.execute(stmt.order_by(p.feature_flag.c.flag_key))
        return [serialize_feature_flag(dict(row)) for row in rows.mappings().all()]

    async def get_feature_flag(self, tenant_id: str | UUID, flag_key: str) -> dict[str, Any] | None:
        row = await self.session.execute(
            select(p.feature_flag).where(
                p.feature_flag.c.tenant_id == tenant_id,
                p.feature_flag.c.flag_key == validate_flag_key(flag_key),
            )
        )
        mapping = row.mappings().first()
        return serialize_feature_flag(dict(mapping)) if mapping else None

    async def set_feature_flag(
        self,
        tenant_id: str | UUID,
        flag_key: str,
        payload: Mapping[str, Any],
        *,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        normalized = validate_flag_key(flag_key)
        current = await self.get_feature_flag(tenant_id, normalized)
        values = flag_change_values(current, payload, actor_id=actor_id, now=datetime.now(UTC))
        if values is not None:
            if current is None:
                await self.session.execute(
                    insert(p.feature_flag).values(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        flag_key=normalized,
                        **values,
                    )
                )
            else:
                await self.session.execute(
                    update(p.feature_flag)
                    .where(
                        p.feature_flag.c.tenant_id == tenant_id,
                        p.feature_flag.c.flag_key == normalized,
                    )
                    .values(**values, version=p.feature_flag.c.version + 1)
                )
            updated = await self.get_feature_flag(tenant_id, normalized)
            if updated is None:  # pragma: no cover - write/read race only
                raise ConfigConflict("feature flag was not persisted", code="FLAG_NOT_FOUND")
            await self.audit(
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=FLAG_CHANGED_EVENT,
                resource_type=FLAG_AGGREGATE_TYPE,
                resource_id=updated.get("id"),
                diff={"before": current, "after": updated, "flag_key": normalized},
            )
            await self.emit_event(
                tenant_id=tenant_id,
                aggregate_type=FLAG_AGGREGATE_TYPE,
                aggregate_id=updated["id"],
                aggregate_version=int(updated.get("version") or 1),
                event_type=FLAG_CHANGED_EVENT,
                payload={
                    "flag_key": normalized,
                    "is_enabled": updated["is_enabled"],
                    "source": updated["source"],
                    "changed_by": updated.get("changed_by"),
                    "notes": updated.get("notes"),
                    "before": None if current is None else current.get("is_enabled"),
                },
            )
            return updated
        # Idempotent no-op: return the stored row unchanged (no audit/event).
        if current is None:  # pragma: no cover - defensive; values is None only when current exists
            raise ConfigNotFound(f"feature flag {normalized!r} not found", code="FLAG_NOT_FOUND")
        return current

    # ------------------------------------------------------------------ number sequences -----
    async def list_number_sequences(
        self,
        tenant_id: str | UUID,
        *,
        doc_type: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        normalized = validate_sequence_filter(doc_type)
        stmt = select(p.number_sequence).where(p.number_sequence.c.tenant_id == tenant_id)
        if normalized is not None:
            stmt = stmt.where(p.number_sequence.c.doc_type == normalized)
        rows = await self.session.execute(
            stmt.order_by(p.number_sequence.c.doc_type, p.number_sequence.c.scope_key, p.number_sequence.c.period_key).limit(
                max(1, min(int(limit), 500))
            )
        )
        return [serialize_number_sequence(dict(row)) for row in rows.mappings().all()]


# ----------------------------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------------------------
def _check_if_match(current_version: Any, expected_version: int | None, *, resource: str) -> None:
    if expected_version is None:
        return
    try:
        current = int(current_version)
    except (TypeError, ValueError):
        current = 0
    if expected_version != current:
        raise ConfigStale(
            f"{resource} version conflict: expected {expected_version}, current {current}",
            details={"expected_version": expected_version, "current_version": current},
        )


def parse_if_match(header_value: str | None) -> int | None:
    """Parse an ``If-Match`` header (bare version or quoted ETag ``\"3\"``)."""
    if header_value is None or not str(header_value).strip():
        return None
    raw = str(header_value).strip().strip('"')
    if raw in {"*", "W/*"}:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            "If-Match must be an integer version or quoted ETag",
            code="PRECONDITION_INVALID",
        ) from exc


__all__ = [
    "ConfigConflict",
    "ConfigError",
    "ConfigNotFound",
    "ConfigRepository",
    "ConfigStale",
    "CUSTOM_FIELD_ENTITY_TYPES",
    "CUSTOM_FIELD_DATA_TYPES",
    "FISCAL_PERIOD_STATUSES",
    "GAP_POLICIES",
    "ISO_NUMERIC_MAX",
    "ISO_NUMERIC_MIN",
    "MINOR_UNITS_MAX",
    "MINOR_UNITS_MIN",
    "RESET_POLICIES",
    "assert_period_transition_guarded",
    "custom_field_change_required",
    "flag_change_values",
    "parse_if_match",
    "periods_overlap",
    "serialize_custom_field",
    "serialize_currency",
    "serialize_feature_flag",
    "serialize_fiscal_period",
    "serialize_number_sequence",
    "validate_custom_field_create",
    "validate_custom_field_patch",
    "validate_currency_code",
    "validate_currency_create",
    "validate_currency_patch",
    "validate_fiscal_period_create",
    "validate_flag_key",
    "validate_flag_patch",
    "validate_sequence_filter",
]
