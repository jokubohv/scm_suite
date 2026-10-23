"""FS-00 platform "extras" repository (task-54): tenant lifecycle extras, custom-field values,
user data scopes, approval-definition and number-sequence patches, canonical audit reads.

Async SQLAlchemy 2.0 Core over the ``platform`` tables (migrations 0001–0003). Every method is
tenant-scoped by construction (the caller supplies the principal's tenant), never commits (the request
session dependency owns the transaction) and returns JSON-safe dicts. Authorization/validation policy
(scope checks, error mapping, If-Match parsing) stays in the API layer; this module owns SQL shape,
state-transition guards and audit writes.

Tenant offboarding reuses the canonical :class:`PlatformAdminRepository` lifecycle/audit machinery
(``ACTIVE|SUSPENDED → OFFBOARDING``, session revocation, SYSTEM-tenant audit) and adds the FS-00 export
marker (recorded inside ``tenant.settings.offboarding_export``) and response shape.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, desc, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import custom_fields
from app.core.custom_fields import AuditHook, CustomFieldAuditEvent, CustomFieldValue
from app.core.idempotency_store import build_fingerprint, complete, guard
from app.core.models import platform as p
from app.core.repositories.platform_admin import (
    ALLOWED_TRANSITIONS,
    PlatformAdminRepository,
    validate_status_transition,
)
from app.core.security.tenant_context import SYSTEM_TENANT_ID

SYSTEM_ID = UUID(str(SYSTEM_TENANT_ID))
DATA_SCOPE_TYPES = frozenset({"LOCATION", "CLASS", "DEPARTMENT", "SUBSIDIARY"})
RESET_POLICIES = frozenset({"NEVER", "ANNUAL", "MONTHLY"})
GAP_POLICIES = frozenset({"GAPLESS", "NON_GAPLESS"})
INVITATION_TTL = timedelta(days=7)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPROVAL_DEFINITION_FIELDS = ("condition", "steps", "priority", "is_enabled")
SEQUENCE_PATCH_FIELDS = ("prefix", "padding", "reset_policy", "gap_policy", "next_value")
AUDIT_MAX_LIMIT = 500


# --------------------------------------------------------------------------- errors ----------
class PlatformExtrasError(Exception):
    """Typed error; routers map ``code``/``http_status`` to problem+json."""

    code: str = "PLATFORM_EXTRAS_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class NotFoundError(PlatformExtrasError):
    code = "NOT_FOUND"
    http_status = 404


class TenantNotFoundError(NotFoundError):
    code = "TENANT_NOT_FOUND"


class UserNotFoundError(NotFoundError):
    code = "USER_NOT_FOUND"


class DefinitionNotFoundError(NotFoundError):
    code = "APPROVAL_DEFINITION_NOT_FOUND"


class SequenceNotFoundError(NotFoundError):
    code = "NUMBER_SEQUENCE_NOT_FOUND"


class ValidationError(PlatformExtrasError):
    code = "VALIDATION_ERROR"
    http_status = 422


class ConflictError(PlatformExtrasError):
    code = "CONFLICT"
    http_status = 409


class TenantTransitionError(ConflictError):
    code = "TENANT_INVALID_STATE"


class ReservedTenantError(ConflictError):
    code = "RESERVED_TENANT"


class VersionConflictError(ConflictError):
    code = "VERSION_CONFLICT"


class InvitationConflictError(ConflictError):
    code = "BOOTSTRAP_ADMIN_INVITER_REQUIRED"


# --------------------------------------------------------------------------- helpers ---------
def _as_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return _iso(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def normalise_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise ValidationError("a valid email address is required", details={"field": "email"})
    return email


def parse_version(if_match: str | None) -> int | None:
    """Parse a simple numeric or weak/strong ETag ``If-Match`` header ('3', '\"3\"', W/\"3\")."""
    if if_match is None:
        return None
    raw = if_match.strip()
    if raw in {"", "*"}:
        return None
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    raw = raw.strip('"')
    try:
        version = int(raw)
    except ValueError as exc:
        raise ValidationError("If-Match must contain a numeric version", details={"field": "If-Match"}) from exc
    if version < 1:
        raise ValidationError("If-Match version must be >= 1", details={"field": "If-Match"})
    return version


def validate_data_scopes(scopes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(scopes, Sequence) or isinstance(scopes, str | bytes) or len(scopes) > 500:
        raise ValidationError("scopes must be an array (max 500)")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in scopes:
        scope_type = str(item.get("scope_type", "")).upper()
        scope_value = str(item.get("scope_value", "")).strip()
        include_descendants = bool(item.get("include_descendants", False))
        if scope_type not in DATA_SCOPE_TYPES:
            raise ValidationError(
                "unknown data scope_type", details={"field": "scope_type", "allowed": sorted(DATA_SCOPE_TYPES)}
            )
        if not scope_value or len(scope_value) > 200:
            raise ValidationError("scope_value must be 1-200 characters", details={"field": "scope_value"})
        key = (scope_type, scope_value)
        if key in seen:
            raise ValidationError(
                "duplicate data scope", details={"scope_type": scope_type, "scope_value": scope_value}
            )
        seen.add(key)
        rows.append({"scope_type": scope_type, "scope_value": scope_value, "include_descendants": include_descendants})
    return rows


def validate_approval_definition_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - set(APPROVAL_DEFINITION_FIELDS))
    if unknown:
        raise ValidationError("unknown approval-definition fields", details={"fields": unknown})
    result: dict[str, Any] = {}
    if "condition" in payload:
        condition = payload["condition"]
        if not isinstance(condition, Mapping):
            raise ValidationError("condition must be a JSON object", details={"field": "condition"})
        result["condition"] = dict(condition)
    if "steps" in payload:
        steps = payload["steps"]
        if not isinstance(steps, Sequence) or isinstance(steps, str | bytes):
            raise ValidationError("steps must be a JSON array", details={"field": "steps"})
        if len(steps) > 50:
            raise ValidationError("steps must contain at most 50 entries", details={"field": "steps"})
        result["steps"] = [dict(step) if isinstance(step, Mapping) else step for step in steps]
    if "priority" in payload:
        priority = payload["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100_000:
            raise ValidationError("priority must be an integer >= 0", details={"field": "priority"})
        result["priority"] = priority
    if "is_enabled" in payload:
        if not isinstance(payload["is_enabled"], bool):
            raise ValidationError("is_enabled must be a boolean", details={"field": "is_enabled"})
        result["is_enabled"] = payload["is_enabled"]
    if not result:
        raise ValidationError("at least one editable field is required")
    return result


def validate_number_sequence_patch(payload: Mapping[str, Any], *, current_next_value: int) -> dict[str, Any]:
    unknown = sorted(set(payload) - set(SEQUENCE_PATCH_FIELDS))
    if unknown:
        raise ValidationError("unknown number-sequence fields", details={"fields": unknown})
    result: dict[str, Any] = {}
    if "prefix" in payload:
        prefix = payload["prefix"]
        if not isinstance(prefix, str) or len(prefix) > 32:
            raise ValidationError("prefix must be a string of at most 32 characters", details={"field": "prefix"})
        result["prefix"] = prefix
    if "padding" in payload:
        padding = payload["padding"]
        if not isinstance(padding, int) or isinstance(padding, bool) or not 1 <= padding <= 12:
            raise ValidationError("padding must be an integer 1-12", details={"field": "padding"})
        result["padding"] = padding
    if "reset_policy" in payload:
        policy = str(payload["reset_policy"]).upper()
        if policy not in RESET_POLICIES:
            raise ValidationError(
                "unknown reset_policy", details={"field": "reset_policy", "allowed": sorted(RESET_POLICIES)}
            )
        result["reset_policy"] = policy
    if "gap_policy" in payload:
        policy = str(payload["gap_policy"]).upper()
        if policy not in GAP_POLICIES:
            raise ValidationError(
                "unknown gap_policy", details={"field": "gap_policy", "allowed": sorted(GAP_POLICIES)}
            )
        result["gap_policy"] = policy
    if "next_value" in payload:
        next_value = payload["next_value"]
        if not isinstance(next_value, int) or isinstance(next_value, bool) or next_value < 1:
            raise ValidationError("next_value must be an integer >= 1", details={"field": "next_value"})
        if next_value < current_next_value:
            raise ValidationError(
                "next_value can never be decreased",
                details={"field": "next_value", "current": current_next_value, "requested": next_value},
            )
        result["next_value"] = next_value
    if not result:
        raise ValidationError("at least one editable field is required")
    return result


def serialize_custom_value(value: CustomFieldValue) -> dict[str, Any]:
    return {
        "field_code": value.field_code,
        "data_type": value.data_type,
        "value": _json_value(value.value),
        "entity_type": value.entity_type,
        "entity_id": str(value.entity_id),
    }


def serialize_data_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope_type": str(row["scope_type"]),
        "scope_value": str(row["scope_value"]),
        "include_descendants": bool(row["include_descendants"]),
    }


def serialize_approval_definition(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "document_type": row["document_type"],
        "name": row["name"],
        "condition": dict(row["condition"] or {}),
        "steps": list(row["steps"] or []),
        "priority": int(row["priority"]),
        "is_enabled": bool(row["is_enabled"]),
        "version": int(row["version"]),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def serialize_number_sequence(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "doc_type": row["doc_type"],
        "scope_key": row["scope_key"],
        "period_key": row.get("period_key"),
        "prefix": row["prefix"],
        "padding": int(row["padding"]),
        "next_value": int(row["next_value"]),
        "reset_policy": row["reset_policy"],
        "gap_policy": row["gap_policy"],
        "version": int(row["version"]),
    }


def serialize_tenant(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "code": row["code"],
        "name": row["name"],
        "status": row["status"],
        "base_currency_code": row.get("base_currency_code"),
        "timezone": row["timezone"],
        "version": int(row["version"]),
    }


def serialize_audit_event(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "event_id": str(row["event_id"]),
        "tenant_id": str(row["tenant_id"]),
        "actor_type": row["actor_type"],
        "actor_id": str(row["actor_id"]) if row["actor_id"] is not None else None,
        "action": row["action"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "diff": dict(row["diff"] or {}),
        "request_id": row.get("request_id"),
        "trace_id": row.get("trace_id"),
        "source": row["source"],
        "occurred_at": _iso(row["occurred_at"]),
    }


def encode_cursor(occurred_at: datetime, event_id: UUID) -> str:
    raw = f"{occurred_at.astimezone(UTC).isoformat()}|{event_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        occurred_raw, event_raw = raw.split("|", 1)
        return datetime.fromisoformat(occurred_raw), UUID(event_raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("invalid cursor", details={"field": "cursor"}) from exc


class PlatformExtrasRepository:
    """Request-scoped repository; no commits (the session dependency owns the transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._admin = PlatformAdminRepository(session)

    # ------------------------------------------------------------------ audit / idempotency
    async def audit(
        self,
        *,
        tenant_id: UUID | str,
        actor_id: UUID | str | None,
        action: str,
        resource_type: str,
        resource_id: UUID | str | None,
        diff: Mapping[str, Any] | None = None,
        system_scope: bool = False,
        actor_type: str = "USER",
        source: str = "API",
    ) -> UUID:
        event_id = uuid4()
        await self.session.execute(
            insert(p.audit_event).values(
                id=event_id,
                tenant_id=SYSTEM_ID if system_scope else _as_uuid(tenant_id),
                event_id=event_id,
                actor_type=actor_type,
                actor_id=_as_uuid(actor_id),
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source=source,
                occurred_at=_now(),
            )
        )
        return event_id

    async def begin_idempotent(
        self,
        *,
        tenant_id: UUID | str,
        endpoint_id: str,
        key: str,
        fingerprint: str,
        actor_id: UUID | str | None,
    ) -> dict[str, Any] | None:
        """Reserve the key. Returns a replay payload for a completed duplicate, else ``None``."""
        decision = await guard(
            self.session,
            tenant_id=tenant_id,
            endpoint_id=endpoint_id,
            key=key,
            request_fingerprint_hash=fingerprint,
            actor_id=actor_id,
        )
        decision.raise_for_conflict()
        if decision.replay and decision.record is not None:
            record = decision.record
            return {
                "response_status": int(record.response_status or 200),
                "response_body": dict(record.response_body or {}),
                "resource_id": record.resource_id,
            }
        return None

    async def complete_idempotent(
        self,
        *,
        tenant_id: UUID | str,
        endpoint_id: str,
        key: str,
        response_status: int,
        response_body: Mapping[str, Any],
        resource_id: UUID | str | None,
    ) -> None:
        await complete(
            self.session,
            tenant_id=tenant_id,
            endpoint_id=endpoint_id,
            key=key,
            response_status=response_status,
            response_body=response_body,
            resource_id=resource_id,
        )

    @staticmethod
    def fingerprint(*, method: str, path: str, tenant_id: UUID | str, endpoint_id: str, body: Any = None) -> str:
        return build_fingerprint(method=method, path=path, tenant_id=tenant_id, endpoint_id=endpoint_id, body=body)

    # ------------------------------------------------------------------ tenant lifecycle
    async def get_tenant(self, tenant_id: str | UUID) -> dict[str, Any] | None:
        return await self._admin.get_tenant(tenant_id)

    async def offboard_tenant(
        self,
        *,
        tenant_id: str | UUID,
        reason: str,
        export: bool,
        actor_identity_id: str | UUID | None,
    ) -> dict[str, Any]:
        current = await self._admin.get_tenant(tenant_id)
        if current is None:
            raise TenantNotFoundError("tenant not found", details={"tenant_id": str(tenant_id)})
        if str(current["id"]) == str(SYSTEM_ID):
            raise ReservedTenantError("the SYSTEM tenant cannot be offboarded")
        validate_status_transition(str(current["status"]), "OFFBOARDING")
        row, already, sessions_revoked = await self._admin.transition_status(
            tenant_id=tenant_id,
            target_status="OFFBOARDING",
            reason=reason,
            actor_identity_id=actor_identity_id,
        )
        settings = dict(row.get("settings") or {})
        marker = settings.get("offboarding_export")
        if not isinstance(marker, Mapping) or not marker.get("export_job_id"):
            marker = {
                "export_job_id": str(uuid4()),
                "format": "JSON_NDJSON",
                "requested_at": _now().isoformat(),
                "export": bool(export),
                "reason": reason,
                "status": "REQUESTED",
            }
            settings["offboarding_export"] = dict(marker)
            await self.session.execute(
                update(p.tenant)
                .where(p.tenant.c.id == _as_uuid(tenant_id))
                .values(settings=settings, updated_at=_now(), version=p.tenant.c.version + 1)
            )
            await self.audit(
                tenant_id=SYSTEM_ID,
                actor_id=actor_identity_id,
                action="tenant.offboard.export_requested",
                resource_type="tenant",
                resource_id=tenant_id,
                diff={"export_job_id": marker["export_job_id"], "format": marker["format"], "export": bool(export)},
                system_scope=True,
            )
        updated = await self._admin.get_tenant(tenant_id)
        assert updated is not None
        return {
            "tenant_id": str(updated["id"]),
            "state": "OFFBOARDING",
            "status": updated["status"],
            "already": already,
            "sessions_revoked": sessions_revoked,
            "export_job_id": marker["export_job_id"],
            "export": {
                "export_job_id": marker["export_job_id"],
                "format": marker["format"],
                "status": marker["status"],
            },
            "tenant": serialize_tenant(updated),
        }

    async def bootstrap_admin(
        self,
        *,
        tenant_id: str | UUID,
        email: str,
        actor_identity_id: str | UUID | None,
    ) -> dict[str, Any]:
        normalised = normalise_email(email)
        tenant_row = await self._admin.get_tenant(tenant_id)
        if tenant_row is None:
            raise TenantNotFoundError("tenant not found", details={"tenant_id": str(tenant_id)})
        if str(tenant_row["id"]) == str(SYSTEM_ID):
            raise ReservedTenantError("the SYSTEM tenant cannot be bootstrapped")
        if str(tenant_row["status"]) != "ACTIVE":
            raise TenantTransitionError(
                "bootstrap admin invitation requires an ACTIVE tenant", details={"status": tenant_row["status"]}
            )
        admin_role = (
            (
                await self.session.execute(
                    select(p.role).where(
                        p.role.c.tenant_id == _as_uuid(tenant_id),
                        p.role.c.code == "admin",
                        p.role.c.inactive_at.is_(None),
                    )
                )
            )
            .mappings()
            .first()
        )
        if admin_role is None:
            raise ValidationError("tenant has no active 'admin' role template", details={"role": "admin"})
        inviter = (
            await self.session.execute(
                select(p.app_user.c.id)
                .where(
                    p.app_user.c.tenant_id == _as_uuid(tenant_id),
                    p.app_user.c.status == "ACTIVE",
                    p.app_user.c.membership_status.in_(("ACTIVE", "INVITED")),
                )
                .order_by(p.app_user.c.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if inviter is None:
            raise InvitationConflictError(
                "tenant has no membership that can issue the bootstrap invitation",
                details={"tenant_id": str(tenant_id)},
            )
        now = _now()
        expires_at = now + INVITATION_TTL
        token = secrets.token_urlsafe(32)
        invitation_id = uuid4()
        await self.session.execute(
            update(p.tenant_invitation)
            .where(
                p.tenant_invitation.c.tenant_id == _as_uuid(tenant_id),
                p.tenant_invitation.c.email == normalised,
                p.tenant_invitation.c.status == "PENDING",
            )
            .values(status="REVOKED")
        )
        await self.session.execute(
            insert(p.tenant_invitation).values(
                id=invitation_id,
                tenant_id=_as_uuid(tenant_id),
                email=normalised,
                intended_role_ids=[admin_role["id"]],
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                status="PENDING",
                invited_by=inviter,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
                version=1,
                created_by=_as_uuid(actor_identity_id),
                updated_by=_as_uuid(actor_identity_id),
            )
        )
        await self.audit(
            tenant_id=SYSTEM_ID,
            actor_id=actor_identity_id,
            action="tenant.bootstrap_admin.invited",
            resource_type="tenant_invitation",
            resource_id=invitation_id,
            diff={
                "tenant_id": str(tenant_id),
                "email": normalised,
                "role": "admin",
                "expires_at": expires_at.isoformat(),
            },
            system_scope=True,
        )
        return {
            "invitation_id": str(invitation_id),
            "tenant_id": str(tenant_id),
            "email": normalised,
            "expires_at": expires_at.isoformat(),
            "status": "PENDING",
        }

    # ------------------------------------------------------------------ custom fields
    async def read_custom_values(
        self, *, tenant_id: str | UUID, entity_type: str, entity_id: str | UUID
    ) -> list[dict[str, Any]]:
        entity_uuid = _as_uuid(entity_id)
        if entity_uuid is None:
            raise ValidationError("entity_id must be a UUID", details={"field": "entity_id"})
        values = await custom_fields.read_values(
            self.session, tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_uuid
        )
        return [serialize_custom_value(value) for value in values]

    async def write_custom_values(
        self,
        *,
        tenant_id: str | UUID,
        entity_type: str,
        entity_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
    ) -> list[dict[str, Any]]:
        entity_uuid = _as_uuid(entity_id)
        if entity_uuid is None:
            raise ValidationError("entity_id must be a UUID", details={"field": "entity_id"})
        if not isinstance(values, Mapping) or not values:
            raise ValidationError(
                "values must be a non-empty object of field_code -> value", details={"field": "values"}
            )

        async def _audit(event: CustomFieldAuditEvent) -> None:
            await self.audit(
                tenant_id=event.tenant_id,
                actor_id=event.actor_id,
                action="custom_field_values.updated",
                resource_type=event.entity_type,
                resource_id=event.entity_id,
                diff={
                    "changes": [
                        {
                            "field_code": change.field_code,
                            "action": change.action,
                            "before": _json_value(change.before),
                            "after": _json_value(change.after),
                        }
                        for change in event.changes
                    ]
                },
            )

        hook: AuditHook = _audit
        stored = await custom_fields.validate_and_store(
            self.session,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_uuid,
            values=values,
            audit=hook,
            actor_id=actor_id,
        )
        return [serialize_custom_value(value) for value in stored]

    # ------------------------------------------------------------------ data scopes
    async def get_user(self, *, tenant_id: str | UUID, user_id: str | UUID) -> dict[str, Any] | None:
        user_uuid = _as_uuid(user_id)
        if user_uuid is None:
            return None
        row = (
            (
                await self.session.execute(
                    select(p.app_user).where(
                        p.app_user.c.tenant_id == _as_uuid(tenant_id), p.app_user.c.id == user_uuid
                    )
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def replace_user_data_scopes(
        self,
        *,
        tenant_id: str | UUID,
        user_id: str | UUID,
        scopes: Sequence[Mapping[str, Any]],
        actor_id: str | UUID | None,
    ) -> list[dict[str, Any]]:
        user = await self.get_user(tenant_id=tenant_id, user_id=user_id)
        if user is None:
            raise UserNotFoundError("user not found", details={"user_id": str(user_id)})
        rows = validate_data_scopes(scopes)
        await self.session.execute(
            delete(p.user_data_scope).where(
                and_(
                    p.user_data_scope.c.tenant_id == _as_uuid(tenant_id),
                    p.user_data_scope.c.user_id == user["id"],
                    p.user_data_scope.c.role_id.is_(None),
                )
            )
        )
        for row in rows:
            await self.session.execute(
                insert(p.user_data_scope).values(
                    id=uuid4(),
                    tenant_id=_as_uuid(tenant_id),
                    user_id=user["id"],
                    role_id=None,
                    scope_type=row["scope_type"],
                    scope_value=row["scope_value"],
                    include_descendants=row["include_descendants"],
                    created_at=_now(),
                    updated_at=_now(),
                    version=1,
                    created_by=_as_uuid(actor_id),
                    updated_by=_as_uuid(actor_id),
                )
            )
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="user.data_scopes.replaced",
            resource_type="app_user",
            resource_id=user["id"],
            diff={"scopes": rows},
        )
        return rows

    # ------------------------------------------------------------------ approval definitions
    async def patch_approval_definition(
        self,
        *,
        tenant_id: str | UUID,
        definition_id: str | UUID,
        payload: Mapping[str, Any],
        expected_version: int | None,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        definition_uuid = _as_uuid(definition_id)
        if definition_uuid is None:
            raise DefinitionNotFoundError(
                "approval definition not found", details={"definition_id": str(definition_id)}
            )
        current = (
            (
                await self.session.execute(
                    select(p.approval_definition).where(
                        p.approval_definition.c.tenant_id == _as_uuid(tenant_id),
                        p.approval_definition.c.id == definition_uuid,
                    )
                )
            )
            .mappings()
            .first()
        )
        if current is None:
            raise DefinitionNotFoundError(
                "approval definition not found", details={"definition_id": str(definition_id)}
            )
        if expected_version is not None and int(current["version"]) != expected_version:
            raise VersionConflictError(
                "approval definition version does not match If-Match",
                details={"current_version": int(current["version"]), "if_match": expected_version},
            )
        changes = validate_approval_definition_patch(payload)
        await self.session.execute(
            update(p.approval_definition)
            .where(
                p.approval_definition.c.tenant_id == _as_uuid(tenant_id),
                p.approval_definition.c.id == definition_uuid,
                p.approval_definition.c.version == int(current["version"]),
            )
            .values(
                **changes,
                updated_at=_now(),
                updated_by=_as_uuid(actor_id),
                version=p.approval_definition.c.version + 1,
            )
        )
        updated = (
            (
                await self.session.execute(
                    select(p.approval_definition).where(
                        p.approval_definition.c.tenant_id == _as_uuid(tenant_id),
                        p.approval_definition.c.id == definition_uuid,
                    )
                )
            )
            .mappings()
            .first()
        )
        assert updated is not None
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="approval_definition.updated",
            resource_type="approval_definition",
            resource_id=definition_uuid,
            diff={"fields": sorted(changes), "version": int(updated["version"])},
        )
        return serialize_approval_definition(dict(updated))

    # ------------------------------------------------------------------ number sequences
    async def patch_number_sequence(
        self,
        *,
        tenant_id: str | UUID,
        sequence_id: str | UUID,
        payload: Mapping[str, Any],
        expected_version: int | None,
        actor_id: str | UUID | None,
    ) -> dict[str, Any]:
        sequence_uuid = _as_uuid(sequence_id)
        if sequence_uuid is None:
            raise SequenceNotFoundError("number sequence not found", details={"sequence_id": str(sequence_id)})
        current = (
            (
                await self.session.execute(
                    select(p.number_sequence).where(
                        p.number_sequence.c.tenant_id == _as_uuid(tenant_id),
                        p.number_sequence.c.id == sequence_uuid,
                    )
                )
            )
            .mappings()
            .first()
        )
        if current is None:
            raise SequenceNotFoundError("number sequence not found", details={"sequence_id": str(sequence_id)})
        if expected_version is not None and int(current["version"]) != expected_version:
            raise VersionConflictError(
                "number sequence version does not match If-Match",
                details={"current_version": int(current["version"]), "if_match": expected_version},
            )
        changes = validate_number_sequence_patch(payload, current_next_value=int(current["next_value"]))
        await self.session.execute(
            update(p.number_sequence)
            .where(
                p.number_sequence.c.tenant_id == _as_uuid(tenant_id),
                p.number_sequence.c.id == sequence_uuid,
                p.number_sequence.c.version == int(current["version"]),
            )
            .values(
                **changes,
                updated_at=_now(),
                updated_by=_as_uuid(actor_id),
                version=p.number_sequence.c.version + 1,
            )
        )
        updated = (
            (
                await self.session.execute(
                    select(p.number_sequence).where(
                        p.number_sequence.c.tenant_id == _as_uuid(tenant_id),
                        p.number_sequence.c.id == sequence_uuid,
                    )
                )
            )
            .mappings()
            .first()
        )
        assert updated is not None
        await self.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="number_sequence.updated",
            resource_type="number_sequence",
            resource_id=sequence_uuid,
            diff={"fields": sorted(changes), "version": int(updated["version"])},
        )
        return serialize_number_sequence(dict(updated))

    # ------------------------------------------------------------------ audit reads
    async def query_audit_events(
        self,
        *,
        tenant_id: str | UUID,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actor_id: str | UUID | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None]:
        bounded = max(1, min(int(limit), AUDIT_MAX_LIMIT))
        stmt = select(p.audit_event).where(p.audit_event.c.tenant_id == _as_uuid(tenant_id))
        if resource_type:
            stmt = stmt.where(p.audit_event.c.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(p.audit_event.c.resource_id == resource_id)
        actor_uuid = _as_uuid(actor_id) if actor_id is not None else None
        if actor_id is not None:
            if actor_uuid is None:
                raise ValidationError("actor_id must be a UUID", details={"field": "actor_id"})
            stmt = stmt.where(p.audit_event.c.actor_id == actor_uuid)
        if occurred_from is not None:
            stmt = stmt.where(p.audit_event.c.occurred_at >= occurred_from.astimezone(UTC))
        if occurred_to is not None:
            stmt = stmt.where(p.audit_event.c.occurred_at <= occurred_to.astimezone(UTC))
        if cursor:
            cursor_at, cursor_id = decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    p.audit_event.c.occurred_at < cursor_at,
                    and_(p.audit_event.c.occurred_at == cursor_at, p.audit_event.c.id < cursor_id),
                )
            )
        stmt = stmt.order_by(desc(p.audit_event.c.occurred_at), desc(p.audit_event.c.id)).limit(bounded + 1)
        rows = (await self.session.execute(stmt)).mappings().all()
        items = [serialize_audit_event(dict(row)) for row in rows[:bounded]]
        next_cursor = None
        if len(rows) > bounded:
            last = rows[bounded - 1]
            next_cursor = encode_cursor(last["occurred_at"], last["id"])
        return items, next_cursor


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ConflictError",
    "DefinitionNotFoundError",
    "InvitationConflictError",
    "NotFoundError",
    "PlatformExtrasError",
    "PlatformExtrasRepository",
    "ReservedTenantError",
    "SequenceNotFoundError",
    "TenantNotFoundError",
    "TenantTransitionError",
    "UserNotFoundError",
    "ValidationError",
    "VersionConflictError",
    "decode_cursor",
    "encode_cursor",
    "normalise_email",
    "parse_version",
    "serialize_approval_definition",
    "serialize_audit_event",
    "serialize_custom_value",
    "serialize_data_scope",
    "serialize_number_sequence",
    "serialize_tenant",
    "validate_approval_definition_patch",
    "validate_data_scopes",
    "validate_number_sequence_patch",
]
