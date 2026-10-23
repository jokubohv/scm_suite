"""Typed custom-field value service over ``platform.custom_field_def`` / ``custom_field_value``.

FS-00 §6 contract: ``custom_fields.validate_and_store(entity_type, entity_id, values)``. This module is
the single implementation of custom-field **value** semantics (the definition CRUD is owned by the
config service):

* entity-type allow-list from FS-00 §6 (``ITEM, LOT, SERIAL, WORK_ORDER, VENDOR, PURCHASE_ORDER,
  ITEM_RECEIPT, VENDOR_BILL, CUSTOMER_PART, UPC``); unknown types/fields fail closed;
* typed validation/coercion for ``TEXT``/``NUMBER``/``DATE``/``BOOL``/``SELECT`` against the definition's
  ``options`` and ``validation`` JSONB (``min``/``max``/``min_length``/``max_length``/``regex``/
  ``allowed_values`` — the same rule vocabulary the config service accepts);
* required-field enforcement (``CUSTOM_FIELD_REQUIRED``) for activation/complete-payload paths;
* tenant-scoped upsert per ``(entity_type, entity_id, field_code)`` into the typed EAV columns, clear via
  ``None``, and typed read helpers + usage count;
* definitions are read-only here (immutability is enforced by the owning config service); a malformed
  definition fails closed with ``CUSTOM_FIELD_DEFINITION_INVALID`` instead of silently skipping values;
* audit is a **callback hook** (``audit=async fn(event)``) so this core service does not assume ownership
  of the audit table; the hook must be called in the caller's transaction (fail-closed).

Async SQLAlchemy Core only. No HTTP, no commits (caller owns the transaction).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p

__all__ = [
    "ALLOWED_ENTITY_TYPES",
    "CUSTOM_FIELD_DATA_TYPES",
    "CUSTOM_FIELD_VALIDATION_KEYS",
    "AuditHook",
    "CustomFieldAuditEvent",
    "CustomFieldChange",
    "CustomFieldDefinition",
    "CustomFieldDefinitionError",
    "CustomFieldError",
    "CustomFieldRequiredError",
    "CustomFieldService",
    "CustomFieldTypeMismatchError",
    "CustomFieldUnknownError",
    "CustomFieldValidationError",
    "CustomFieldValue",
    "UnknownEntityTypeError",
    "assert_required_present",
    "coerce_value",
    "definition_from_row",
    "list_definitions",
    "normalise_entity_type",
    "read_value",
    "read_values",
    "usage",
    "validate_and_store",
    "validate_value",
]

# FS-00 §6 / §5 custom-field entity_type allowlist for Phase 1.
ALLOWED_ENTITY_TYPES: frozenset[str] = frozenset(
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
CUSTOM_FIELD_DATA_TYPES: frozenset[str] = frozenset({"TEXT", "NUMBER", "DATE", "BOOL", "SELECT"})
CUSTOM_FIELD_VALIDATION_KEYS: frozenset[str] = frozenset(
    {"min", "max", "min_length", "max_length", "regex", "allowed_values"}
)
_MAX_TEXT_LENGTH = 10_000
_NUMBER_MAX_DIGITS = 26  # NUMERIC(38,12) leaves 26 integer digits


# --------------------------------------------------------------------------- errors -----------
class CustomFieldError(Exception):
    """Base typed error; routers map ``code``/``http_status`` to problem+json."""

    code: str = "CUSTOM_FIELD_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class UnknownEntityTypeError(CustomFieldError):
    code = "CUSTOM_FIELD_ENTITY_TYPE_INVALID"
    http_status = 422


class CustomFieldDefinitionError(CustomFieldError):
    code = "CUSTOM_FIELD_DEFINITION_INVALID"
    http_status = 422


class CustomFieldUnknownError(CustomFieldError):
    code = "CUSTOM_FIELD_UNKNOWN"
    http_status = 422


class CustomFieldTypeMismatchError(CustomFieldError):
    code = "CUSTOM_FIELD_TYPE_MISMATCH"
    http_status = 422


class CustomFieldValidationError(CustomFieldError):
    code = "CUSTOM_FIELD_INVALID"
    http_status = 422


class CustomFieldRequiredError(CustomFieldError):
    code = "CUSTOM_FIELD_REQUIRED"
    http_status = 422


# --------------------------------------------------------------------------- dataclasses -------
@dataclass(frozen=True, slots=True)
class CustomFieldDefinition:
    id: UUID
    entity_type: str
    field_code: str
    label: str
    data_type: str
    options: tuple[str, ...]
    is_required: bool
    is_searchable: bool
    validation: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CustomFieldValue:
    definition_id: UUID
    entity_id: UUID
    entity_type: str
    field_code: str
    data_type: str
    value: Any

    @property
    def typed_value(self) -> Any:
        return self.value


@dataclass(frozen=True, slots=True)
class CustomFieldChange:
    field_code: str
    action: Literal["SET", "CLEAR"]
    before: Any = None
    after: Any = None


@dataclass(frozen=True, slots=True)
class CustomFieldAuditEvent:
    tenant_id: UUID
    entity_type: str
    entity_id: UUID
    actor_id: str | None
    changes: tuple[CustomFieldChange, ...]


AuditHook = Callable[[CustomFieldAuditEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Validated:
    definition: CustomFieldDefinition
    value: Any
    column: str
    db_value: Any


# --------------------------------------------------------------------------- pure helpers ------
def normalise_entity_type(entity_type: str) -> str:
    candidate = str(entity_type).strip().upper()
    if candidate not in ALLOWED_ENTITY_TYPES:
        raise UnknownEntityTypeError(
            "unsupported custom-field entity_type",
            details={"entity_type": candidate, "allowed": sorted(ALLOWED_ENTITY_TYPES)},
        )
    return candidate


def _normalise_validation(validation: Any, *, field_code: str) -> dict[str, Any]:
    if validation is None:
        return {}
    if not isinstance(validation, Mapping):
        raise CustomFieldDefinitionError(
            "definition validation must be a JSON object", details={"field_code": field_code}
        )
    normalised = dict(validation)
    unknown = sorted(set(normalised) - CUSTOM_FIELD_VALIDATION_KEYS)
    if unknown:
        raise CustomFieldDefinitionError(
            "definition has unsupported validation rules",
            details={"field_code": field_code, "unsupported_keys": unknown},
        )
    for key in ("min", "max"):
        if key in normalised and not isinstance(normalised[key], int | float):
            raise CustomFieldDefinitionError(
                "definition validation min/max must be numeric",
                details={"field_code": field_code, "field": f"validation.{key}"},
            )
    if "min" in normalised and "max" in normalised and normalised["min"] > normalised["max"]:
        raise CustomFieldDefinitionError("definition validation min must be <= max", details={"field_code": field_code})
    for key in ("min_length", "max_length"):
        if key in normalised:
            value = normalised[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CustomFieldDefinitionError(
                    "definition validation min_length/max_length must be non-negative integers",
                    details={"field_code": field_code, "field": f"validation.{key}"},
                )
    if "min_length" in normalised and "max_length" in normalised:
        if normalised["min_length"] > normalised["max_length"]:
            raise CustomFieldDefinitionError(
                "definition validation min_length must be <= max_length", details={"field_code": field_code}
            )
    if "regex" in normalised:
        pattern = normalised["regex"]
        if not isinstance(pattern, str):
            raise CustomFieldDefinitionError(
                "definition validation regex must be a string", details={"field_code": field_code}
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CustomFieldDefinitionError(
                "definition validation regex is invalid",
                details={"field_code": field_code, "error": str(exc)},
            ) from exc
    if "allowed_values" in normalised:
        allowed = normalised["allowed_values"]
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise CustomFieldDefinitionError(
                "definition validation allowed_values must be a list of strings",
                details={"field_code": field_code},
            )
    return normalised


def _normalise_options(data_type: str, options: Any, *, field_code: str) -> tuple[str, ...]:
    if data_type != "SELECT":
        if options not in (None, []):
            raise CustomFieldDefinitionError(
                "only SELECT custom fields may define options",
                details={"field_code": field_code, "data_type": data_type},
            )
        return ()
    if not isinstance(options, list | tuple) or not options:
        raise CustomFieldDefinitionError(
            "SELECT custom fields require a non-empty options list", details={"field_code": field_code}
        )
    normalised = tuple(str(item) for item in options)
    if len(set(normalised)) != len(normalised):
        raise CustomFieldDefinitionError("SELECT options must be unique", details={"field_code": field_code})
    return normalised


def definition_from_row(row: Mapping[str, Any] | RowMapping) -> CustomFieldDefinition:
    """Validate and materialise one ``custom_field_def`` row (fail closed on malformed defs)."""
    data_type = str(row["data_type"]).upper()
    field_code = str(row["field_code"])
    if data_type not in CUSTOM_FIELD_DATA_TYPES:
        raise CustomFieldDefinitionError(
            "definition has an unsupported data_type",
            details={"field_code": field_code, "data_type": data_type},
        )
    entity_type = str(row["entity_type"]).upper()
    if entity_type not in ALLOWED_ENTITY_TYPES:
        raise CustomFieldDefinitionError(
            "definition has an entity_type outside the Phase-1 allowlist",
            details={"field_code": field_code, "entity_type": entity_type},
        )
    return CustomFieldDefinition(
        id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
        entity_type=entity_type,
        field_code=field_code,
        label=str(row.get("label") or field_code),
        data_type=data_type,
        options=_normalise_options(data_type, row.get("options"), field_code=field_code),
        is_required=bool(row.get("is_required")),
        is_searchable=bool(row.get("is_searchable")),
        validation=_normalise_validation(row.get("validation"), field_code=field_code),
    )


def _as_decimal(value: Any, *, field_code: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CustomFieldTypeMismatchError(
            "NUMBER custom field requires a numeric value", details={"field_code": field_code}
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CustomFieldTypeMismatchError(
            "NUMBER custom field requires a decimal value", details={"field_code": field_code}
        ) from None
    if not number.is_finite():
        raise CustomFieldTypeMismatchError("NUMBER custom field must be finite", details={"field_code": field_code})
    if abs(number.adjusted()) > _NUMBER_MAX_DIGITS:
        raise CustomFieldTypeMismatchError(
            "NUMBER custom field exceeds NUMERIC(38,12) precision", details={"field_code": field_code}
        )
    return number


def _as_date(value: Any, *, field_code: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CustomFieldTypeMismatchError(
        "DATE custom field requires an ISO-8601 date (YYYY-MM-DD)", details={"field_code": field_code}
    )


def _as_bool(value: Any, *, field_code: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    raise CustomFieldTypeMismatchError("BOOL custom field requires a boolean", details={"field_code": field_code})


def coerce_value(definition: CustomFieldDefinition, value: Any) -> tuple[Any, str, Any]:
    """Coerce one raw value to the definition's typed column. Returns ``(typed, column, db_value)``."""
    field_code = definition.field_code
    data_type = definition.data_type
    if data_type == "TEXT":
        if not isinstance(value, str):
            raise CustomFieldTypeMismatchError(
                "TEXT custom field requires a string", details={"field_code": field_code}
            )
        if len(value) > _MAX_TEXT_LENGTH:
            raise CustomFieldValidationError("TEXT custom field value is too long", details={"field_code": field_code})
        return value, "value_text", value
    if data_type == "NUMBER":
        number = _as_decimal(value, field_code=field_code)
        return number, "value_number", number
    if data_type == "DATE":
        parsed = _as_date(value, field_code=field_code)
        return parsed, "value_date", parsed
    if data_type == "BOOL":
        parsed_bool = _as_bool(value, field_code=field_code)
        return parsed_bool, "value_bool", parsed_bool
    if data_type == "SELECT":
        selected = str(value) if not isinstance(value, str) else value
        if selected not in definition.options:
            raise CustomFieldValidationError(
                "SELECT custom field value is not one of the allowed options",
                details={"field_code": field_code, "allowed": list(definition.options)},
            )
        return selected, "value_text", selected
    raise CustomFieldDefinitionError(  # pragma: no cover - definition_from_row guards this
        "unsupported custom-field data_type", details={"field_code": field_code, "data_type": data_type}
    )


def _decimal_or_none(item: Any) -> Decimal | None:
    try:
        return Decimal(str(item))
    except (InvalidOperation, ValueError):
        return None


def validate_value(definition: CustomFieldDefinition, value: Any) -> tuple[Any, str, Any]:
    """Coerce + enforce the definition's validation JSONB. Returns ``(typed, column, db_value)``."""
    typed, column, db_value = coerce_value(definition, value)
    rules = definition.validation
    field_code = definition.field_code
    if definition.data_type == "NUMBER":
        number = typed if isinstance(typed, Decimal) else Decimal(str(typed))
        minimum = _decimal_or_none(rules.get("min"))
        maximum = _decimal_or_none(rules.get("max"))
        if minimum is not None and number < minimum:
            raise CustomFieldValidationError(
                "NUMBER custom field value is below min", details={"field_code": field_code, "min": str(minimum)}
            )
        if maximum is not None and number > maximum:
            raise CustomFieldValidationError(
                "NUMBER custom field value is above max", details={"field_code": field_code, "max": str(maximum)}
            )
    elif definition.data_type == "TEXT":
        text = str(typed)
        if "min_length" in rules and len(text) < int(rules["min_length"]):
            raise CustomFieldValidationError(
                "TEXT custom field value is shorter than min_length", details={"field_code": field_code}
            )
        if "max_length" in rules and len(text) > int(rules["max_length"]):
            raise CustomFieldValidationError(
                "TEXT custom field value is longer than max_length", details={"field_code": field_code}
            )
        if "regex" in rules and re.fullmatch(str(rules["regex"]), text) is None:
            raise CustomFieldValidationError(
                "TEXT custom field value does not match regex", details={"field_code": field_code}
            )
    allowed_values = rules.get("allowed_values")
    if allowed_values:
        allowed = {str(item) for item in allowed_values}
        candidate = str(typed)
        if candidate not in allowed:
            raise CustomFieldValidationError(
                "custom field value is not one of the allowed_values",
                details={"field_code": field_code, "allowed": sorted(allowed)},
            )
    return typed, column, db_value


def assert_required_present(definitions: Mapping[str, CustomFieldDefinition], values: Mapping[str, Any]) -> None:
    """Raise ``CUSTOM_FIELD_REQUIRED`` listing every required field missing/None in ``values``."""
    supplied = {code for code, value in values.items() if value is not None}
    missing = sorted(
        code for code, definition in definitions.items() if definition.is_required and code not in supplied
    )
    if missing:
        raise CustomFieldRequiredError("required custom fields are missing", details={"missing": missing})


# --------------------------------------------------------------------------- DB access ---------
def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _decode_stored(definition: CustomFieldDefinition, row: Mapping[str, Any]) -> Any:
    data_type = definition.data_type
    if data_type == "TEXT":
        raw = row.get("value_text")
    elif data_type == "NUMBER":
        raw = row.get("value_number")
    elif data_type == "DATE":
        raw = row.get("value_date")
    elif data_type == "BOOL":
        raw = row.get("value_bool")
    elif data_type == "SELECT":
        raw = row.get("value_text")
    else:  # pragma: no cover - guarded by definition_from_row
        raw = None
    if raw is None:
        raise CustomFieldDefinitionError(
            "stored custom-field value does not match its definition data_type",
            details={"field_code": definition.field_code, "data_type": data_type},
        )
    if data_type == "NUMBER":
        return Decimal(str(raw))
    if data_type == "DATE" and isinstance(raw, datetime):
        return raw.date()
    return raw


async def _definitions_for(
    session: AsyncSession, tenant_id: UUID, entity_type: str
) -> dict[str, CustomFieldDefinition]:
    result = await session.execute(
        select(p.custom_field_def).where(
            p.custom_field_def.c.tenant_id == tenant_id,
            p.custom_field_def.c.entity_type == entity_type,
        )
    )
    definitions: dict[str, CustomFieldDefinition] = {}
    for row in result.mappings().all():
        definition = definition_from_row(row)
        definitions[definition.field_code] = definition
    return definitions


async def list_definitions(
    session: AsyncSession, *, tenant_id: str | UUID, entity_type: str | None = None
) -> list[CustomFieldDefinition]:
    stmt = select(p.custom_field_def).where(p.custom_field_def.c.tenant_id == _as_uuid(tenant_id))
    if entity_type is not None:
        stmt = stmt.where(p.custom_field_def.c.entity_type == normalise_entity_type(entity_type))
    stmt = stmt.order_by(p.custom_field_def.c.entity_type, p.custom_field_def.c.field_code)
    rows = await session.execute(stmt)
    return [definition_from_row(row) for row in rows.mappings().all()]


async def get_definition(
    session: AsyncSession, *, tenant_id: str | UUID, entity_type: str, field_code: str
) -> CustomFieldDefinition | None:
    definitions = await _definitions_for(session, _as_uuid(tenant_id), normalise_entity_type(entity_type))
    return definitions.get(str(field_code).strip())


def _validate_values(
    definitions: Mapping[str, CustomFieldDefinition],
    values: Mapping[str, Any],
    *,
    require_complete: bool,
) -> dict[str, _Validated]:
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise CustomFieldUnknownError("unknown custom field(s) for this entity_type", details={"unknown": unknown})
    validated: dict[str, _Validated] = {}
    for code, value in values.items():
        if value is None:
            continue
        definition = definitions[code]
        typed, column, db_value = validate_value(definition, value)
        validated[code] = _Validated(definition=definition, value=typed, column=column, db_value=db_value)
    if require_complete:
        assert_required_present(definitions, values)
    return validated


async def _existing_rows(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    entity_id: UUID,
    definition_ids: Sequence[UUID],
) -> dict[UUID, Mapping[str, Any]]:
    if not definition_ids:
        return {}
    result = await session.execute(
        select(p.custom_field_value).where(
            p.custom_field_value.c.tenant_id == tenant_id,
            p.custom_field_value.c.entity_id == entity_id,
            p.custom_field_value.c.field_def_id.in_(list(definition_ids)),
        )
    )
    return {row["field_def_id"]: dict(row) for row in result.mappings().all()}


async def validate_and_store(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    entity_type: str,
    entity_id: str | UUID,
    values: Mapping[str, Any],
    require_complete: bool = False,
    audit: AuditHook | None = None,
    actor_id: str | UUID | None = None,
    now: datetime | None = None,
) -> list[CustomFieldValue]:
    """Validate + upsert custom-field values for one entity (typed EAV columns), tenant scoped.

    ``values`` maps ``field_code -> raw value``. A ``None`` value clears that field. With
    ``require_complete=True`` every ``is_required`` definition for the entity type must be present
    (used by activation/document-completion paths; raises ``CUSTOM_FIELD_REQUIRED``).
    """
    tid = _as_uuid(tenant_id)
    eid = _as_uuid(entity_id)
    et = normalise_entity_type(entity_type)
    if not isinstance(values, Mapping):
        raise CustomFieldValidationError("values must be an object of field_code -> value")
    defs = await _definitions_for(session, tid, et)
    validated = _validate_values(defs, values, require_complete=require_complete)
    existing = await _existing_rows(session, tenant_id=tid, entity_id=eid, definition_ids=[d.id for d in defs.values()])
    moment = now or datetime.now(UTC)
    changes: list[CustomFieldChange] = []
    stored: list[CustomFieldValue] = []
    for code, raw_value in values.items():
        definition = defs[code]
        current_row = existing.get(definition.id)
        before = _decode_stored(definition, current_row) if current_row is not None else None
        if raw_value is None:
            if current_row is not None:
                await session.execute(
                    delete(p.custom_field_value).where(
                        p.custom_field_value.c.tenant_id == tid,
                        p.custom_field_value.c.field_def_id == definition.id,
                        p.custom_field_value.c.entity_id == eid,
                    )
                )
                changes.append(CustomFieldChange(field_code=code, action="CLEAR", before=before, after=None))
            continue
        validated_value = validated[code]
        columns: dict[str, Any] = {
            "value_text": None,
            "value_number": None,
            "value_date": None,
            "value_bool": None,
        }
        columns[validated_value.column] = validated_value.db_value
        insert_stmt = pg_insert(p.custom_field_value).values(
            id=uuid4(),
            tenant_id=tid,
            field_def_id=definition.id,
            entity_id=eid,
            created_at=moment,
            updated_at=moment,
            created_by=_as_uuid(actor_id) if actor_id is not None else None,
            updated_by=_as_uuid(actor_id) if actor_id is not None else None,
            version=1,
            **columns,
        )
        await session.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=["tenant_id", "field_def_id", "entity_id"],
                set_={
                    **columns,
                    "updated_at": moment,
                    "updated_by": _as_uuid(actor_id) if actor_id is not None else None,
                    "version": p.custom_field_value.c.version + 1,
                },
            )
        )
        changes.append(CustomFieldChange(field_code=code, action="SET", before=before, after=validated_value.value))
        stored.append(
            CustomFieldValue(
                definition_id=definition.id,
                entity_id=eid,
                entity_type=et,
                field_code=code,
                data_type=definition.data_type,
                value=validated_value.value,
            )
        )
    if audit is not None and changes:
        await audit(
            CustomFieldAuditEvent(
                tenant_id=tid,
                entity_type=et,
                entity_id=eid,
                actor_id=str(actor_id) if actor_id is not None else None,
                changes=tuple(changes),
            )
        )
    return stored


async def read_values(
    session: AsyncSession, *, tenant_id: str | UUID, entity_type: str, entity_id: str | UUID
) -> list[CustomFieldValue]:
    """Typed values for one entity, ordered by field_code, tenant scoped (no cross-tenant reads)."""
    tid = _as_uuid(tenant_id)
    et = normalise_entity_type(entity_type)
    eid = _as_uuid(entity_id)
    stmt = (
        select(
            p.custom_field_def.c.id.label("field_def_id"),
            p.custom_field_def.c.field_code,
            p.custom_field_def.c.entity_type,
            p.custom_field_def.c.data_type,
            p.custom_field_value.c.entity_id,
            p.custom_field_value.c.value_text,
            p.custom_field_value.c.value_number,
            p.custom_field_value.c.value_date,
            p.custom_field_value.c.value_bool,
        )
        .select_from(
            p.custom_field_value.join(
                p.custom_field_def, p.custom_field_def.c.id == p.custom_field_value.c.field_def_id
            )
        )
        .where(
            p.custom_field_value.c.tenant_id == tid,
            p.custom_field_def.c.tenant_id == tid,
            p.custom_field_value.c.entity_id == eid,
            p.custom_field_def.c.entity_type == et,
        )
        .order_by(p.custom_field_def.c.field_code)
    )
    rows = await session.execute(stmt)
    definitions = await _definitions_for(session, tid, et)
    results: list[CustomFieldValue] = []
    for row in rows.mappings().all():
        definition = definitions.get(str(row["field_code"]))
        if definition is None:  # pragma: no cover - join guarantees the def exists
            continue
        results.append(
            CustomFieldValue(
                definition_id=definition.id,
                entity_id=row["entity_id"],
                entity_type=definition.entity_type,
                field_code=definition.field_code,
                data_type=definition.data_type,
                value=_decode_stored(definition, dict(row)),
            )
        )
    return results


async def read_value(
    session: AsyncSession,
    *,
    tenant_id: str | UUID,
    entity_type: str,
    entity_id: str | UUID,
    field_code: str,
) -> CustomFieldValue | None:
    for item in await read_values(session, tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id):
        if item.field_code == str(field_code).strip():
            return item
    return None


async def usage(session: AsyncSession, *, tenant_id: str | UUID, field_def_id: str | UUID) -> dict[str, Any]:
    """Usage summary for one definition: value count + definition identity (tenant scoped)."""
    tid = _as_uuid(tenant_id)
    definition = await session.execute(
        select(p.custom_field_def).where(
            p.custom_field_def.c.tenant_id == tid, p.custom_field_def.c.id == _as_uuid(field_def_id)
        )
    )
    row = definition.mappings().first()
    if row is None:
        raise CustomFieldUnknownError("custom-field definition not found", details={"field_def_id": str(field_def_id)})
    count = await session.execute(
        select(func.count())
        .select_from(p.custom_field_value)
        .where(
            p.custom_field_value.c.tenant_id == tid,
            p.custom_field_value.c.field_def_id == _as_uuid(field_def_id),
        )
    )
    materialised = definition_from_row(row)
    return {
        "field_def_id": str(materialised.id),
        "entity_type": materialised.entity_type,
        "field_code": materialised.field_code,
        "data_type": materialised.data_type,
        "value_count": int(count.scalar_one() or 0),
    }


class CustomFieldService:
    """Object facade over the module functions (one instance per request/session scope)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_definitions(
        self, *, tenant_id: str | UUID, entity_type: str | None = None
    ) -> list[CustomFieldDefinition]:
        return await list_definitions(self.session, tenant_id=tenant_id, entity_type=entity_type)

    async def validate_and_store(
        self,
        *,
        tenant_id: str | UUID,
        entity_type: str,
        entity_id: str | UUID,
        values: Mapping[str, Any],
        require_complete: bool = False,
        audit: AuditHook | None = None,
        actor_id: str | UUID | None = None,
    ) -> list[CustomFieldValue]:
        return await validate_and_store(
            self.session,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            values=values,
            require_complete=require_complete,
            audit=audit,
            actor_id=actor_id,
        )

    async def read_values(
        self, *, tenant_id: str | UUID, entity_type: str, entity_id: str | UUID
    ) -> list[CustomFieldValue]:
        return await read_values(self.session, tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id)

    async def read_value(
        self,
        *,
        tenant_id: str | UUID,
        entity_type: str,
        entity_id: str | UUID,
        field_code: str,
    ) -> CustomFieldValue | None:
        return await read_value(
            self.session,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            field_code=field_code,
        )

    async def usage(self, *, tenant_id: str | UUID, field_def_id: str | UUID) -> dict[str, Any]:
        return await usage(self.session, tenant_id=tenant_id, field_def_id=field_def_id)
