"""Platform-admin tenant repository (B1 / FS-00 platform-admin window).

Read/write access to ``platform.tenant`` and the supporting projections needed by the platform-admin
tenant API (tenant detail settings summary, membership view, audit rows, idempotency replay and session
revocation on suspend/offboard).

Boundaries (stay in scope):

* **Platform context only.** Every write is audited with ``tenant_id = SYSTEM_TENANT_ID`` (FS-00
  RULE-PLAT-16); the reserved SYSTEM tenant row itself is never mutable and never deleted.
* **No hard delete.** Tenants move ``ACTIVE -> SUSPENDED -> ACTIVE`` or ``ACTIVE/SUSPENDED ->
  OFFBOARDING`` (terminal); there is no DELETE path anywhere in this module.
* **No cross-context provisioning.** Tenant *row* provisioning and its defaults are owned here; the
  permission catalogue, role templates, number sequences, flags and bootstrap invitations are provisioned
  by their owning FS-00 services (RBAC/auth/config) and are intentionally not written from this module.
* Policy-free DB access, like the other platform repositories: the API layer owns authorization and maps
  the typed errors below to problem+json.

Pure validators/transition helpers are module-level so the unit suite can exercise the rules without a
database.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.platform_events import audit_recorded, emit_platform_event
from app.core.security.tenant_context import SYSTEM_TENANT_ID
from app.core.tenant_provisioning import provision_tenant_defaults

# --------------------------------------------------------------------------- constants ----------
TENANT_STATUSES: tuple[str, ...] = ("ACTIVE", "SUSPENDED", "OFFBOARDING")
PROVISIONABLE_STATUSES: frozenset[str] = frozenset({"ACTIVE", "SUSPENDED"})
NEGATIVE_INVENTORY_POLICIES: frozenset[str] = frozenset({"BLOCK", "WARN", "ALLOW"})
ROUNDING_MODES: frozenset[str] = frozenset({"PER_MOVEMENT", "PER_TRANSACTION", "PER_DOCUMENT"})
PATCHABLE_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "timezone",
        "base_currency_code",
        "negative_inventory_policy",
        "rounding_mode",
        "retention_ledger_years",
        "settings",
        "status",
    }
)
ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "ACTIVE": frozenset({"SUSPENDED", "OFFBOARDING"}),
    "SUSPENDED": frozenset({"ACTIVE", "OFFBOARDING"}),
    "OFFBOARDING": frozenset(),
}
STATUS_AUDIT_ACTIONS: Mapping[str, str] = {
    "SUSPENDED": "tenant.suspended",
    "ACTIVE": "tenant.reactivated",
    "OFFBOARDING": "tenant.offboarded",
}
TENANT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,39}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
IDEMPOTENCY_TTL = timedelta(days=7)
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
_SYSTEM_ID = UUID(str(SYSTEM_TENANT_ID))


class PlatformAdminError(Exception):
    """Base typed error mapped to problem+json by the router."""

    code: str = "PLATFORM_ADMIN_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class TenantValidationError(PlatformAdminError):
    code = "VALIDATION_FAILED"
    http_status = 422


class TenantNotFoundError(PlatformAdminError):
    code = "TENANT_NOT_FOUND"
    http_status = 404


class TenantConflictError(PlatformAdminError):
    code = "TENANT_CODE_CONFLICT"
    http_status = 409


class TenantTransitionError(PlatformAdminError):
    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class ReservedTenantError(PlatformAdminError):
    code = "SYSTEM_TENANT_RESERVED"
    http_status = 409


class TenantVersionConflictError(PlatformAdminError):
    code = "STALE_VERSION"
    http_status = 412


class IdempotencyError(PlatformAdminError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class IdempotencyInProgressError(PlatformAdminError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409


# --------------------------------------------------------------------------- helpers ------------
def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def is_system_tenant(tenant_id: str | UUID) -> bool:
    return _as_uuid(tenant_id) == _SYSTEM_ID


def normalise_code(code: str) -> str:
    """Canonical tenant code (upper-case, stripped) used for uniqueness and lookups."""
    return str(code).strip().upper()


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (KeyError, ValueError):
        raise TenantValidationError("unknown IANA timezone", details={"field": "timezone", "value": value}) from None
    return value


def validate_currency_code(value: str) -> str:
    code = str(value).strip().upper()
    if not CURRENCY_RE.match(code):
        raise TenantValidationError(
            "base_currency_code must be a 3-letter ISO-4217 code", details={"field": "base_currency_code"}
        )
    return code


def validate_code(code: str) -> str:
    candidate = normalise_code(code)
    if not TENANT_CODE_RE.match(candidate):
        raise TenantValidationError(
            "tenant code must match ^[A-Z][A-Z0-9_-]{2,40}$ (normalised upper-case)",
            details={"field": "code"},
        )
    return candidate


def validate_name(name: str) -> str:
    candidate = " ".join(str(name).split())
    if not 1 <= len(candidate) <= 200:
        raise TenantValidationError("name must be 1..200 characters", details={"field": "name"})
    return candidate


def validate_reason(reason: str, *, field: str = "reason") -> str:
    candidate = " ".join(str(reason).split())
    if not 3 <= len(candidate) <= 500:
        raise TenantValidationError(f"{field} must be 3..500 characters", details={"field": field})
    return candidate


def validate_email(value: str) -> str:
    candidate = str(value).strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", candidate) or len(candidate) > 254:
        raise TenantValidationError("invalid email address", details={"field": "bootstrap_admin_email"})
    return candidate


def validate_create(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate/normalise a tenant-create payload. Returns values ready for the insert."""
    if "code" not in payload or payload["code"] in (None, ""):
        raise TenantValidationError("code is required", details={"field": "code"})
    status = str(payload.get("status") or "ACTIVE").upper()
    if status not in PROVISIONABLE_STATUSES:
        raise TenantValidationError(
            "new tenants may only be provisioned as ACTIVE or SUSPENDED",
            details={"field": "status", "value": status},
        )
    policy = str(payload.get("negative_inventory_policy") or "BLOCK").upper()
    if policy not in NEGATIVE_INVENTORY_POLICIES:
        raise TenantValidationError(
            "negative_inventory_policy must be one of BLOCK/WARN/ALLOW",
            details={"field": "negative_inventory_policy", "value": policy},
        )
    rounding = str(payload.get("rounding_mode") or "PER_MOVEMENT").upper()
    if rounding not in ROUNDING_MODES:
        raise TenantValidationError(
            "rounding_mode must be one of PER_MOVEMENT/PER_TRANSACTION/PER_DOCUMENT",
            details={"field": "rounding_mode", "value": rounding},
        )
    retention = int(payload.get("retention_ledger_years", 7))
    if not 1 <= retention <= 50:
        raise TenantValidationError(
            "retention_ledger_years must be between 1 and 50", details={"field": "retention_ledger_years"}
        )
    settings = payload.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise TenantValidationError("settings must be a JSON object", details={"field": "settings"})
    defaults = payload.get("settings_defaults") or {}
    if not isinstance(defaults, Mapping):
        raise TenantValidationError("settings_defaults must be a JSON object", details={"field": "settings_defaults"})
    merged_settings = {**dict(defaults), **dict(settings)}
    result: dict[str, Any] = {
        "code": validate_code(payload["code"]),
        "name": validate_name(payload.get("name") or ""),
        "status": status,
        "timezone": validate_timezone(str(payload.get("timezone") or "UTC")),
        "base_currency_code": (
            validate_currency_code(payload["base_currency_code"]) if payload.get("base_currency_code") else None
        ),
        "negative_inventory_policy": policy,
        "rounding_mode": rounding,
        "retention_ledger_years": retention,
        "settings": dict(merged_settings),
    }
    if payload.get("bootstrap_admin_email"):
        result["bootstrap_admin_email"] = validate_email(payload["bootstrap_admin_email"])
    return result


def validate_patch(payload: Mapping[str, Any], *, current_status: str) -> dict[str, Any]:
    """Validate a PATCH payload; raises for unknown/immutable fields, empty patches and bad transitions."""
    keys = set(payload)
    unknown = keys - PATCHABLE_FIELDS
    if unknown:
        raise TenantValidationError(
            "unknown or immutable tenant fields",
            details={"fields": sorted(unknown)},
        )
    if not keys:
        raise TenantValidationError("patch body must change at least one field")
    result: dict[str, Any] = {}
    for key in keys:
        value = payload[key]
        if key == "name":
            result[key] = validate_name(value)
        elif key == "timezone":
            result[key] = validate_timezone(str(value))
        elif key == "base_currency_code":
            result[key] = validate_currency_code(str(value))
        elif key == "negative_inventory_policy":
            policy = str(value).upper()
            if policy not in NEGATIVE_INVENTORY_POLICIES:
                raise TenantValidationError(
                    "negative_inventory_policy must be one of BLOCK/WARN/ALLOW",
                    details={"field": "negative_inventory_policy"},
                )
            result[key] = policy
        elif key == "rounding_mode":
            rounding = str(value).upper()
            if rounding not in ROUNDING_MODES:
                raise TenantValidationError(
                    "rounding_mode must be one of PER_MOVEMENT/PER_TRANSACTION/PER_DOCUMENT",
                    details={"field": "rounding_mode"},
                )
            result[key] = rounding
        elif key == "retention_ledger_years":
            retention = int(value)
            if not 1 <= retention <= 50:
                raise TenantValidationError(
                    "retention_ledger_years must be between 1 and 50",
                    details={"field": "retention_ledger_years"},
                )
            result[key] = retention
        elif key == "settings":
            if not isinstance(value, Mapping):
                raise TenantValidationError("settings must be a JSON object", details={"field": "settings"})
            result[key] = dict(value)
        elif key == "status":
            target = str(value).upper()
            validate_status_transition(current_status, target)
            result[key] = target
    return result


def validate_status_transition(current: str, target: str) -> None:
    """Enforce the FS-00 tenant lifecycle (same-state is a no-op, validated by the caller)."""
    if current not in TENANT_STATUSES:
        raise TenantTransitionError("tenant is in an unknown status", details={"current": current})
    if target not in TENANT_STATUSES:
        raise TenantTransitionError("unknown target tenant status", details={"target": target})
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise TenantTransitionError(
            f"tenant transition {current} -> {target} is not permitted",
            details={"current": current, "target": target, "allowed": sorted(ALLOWED_TRANSITIONS[current])},
        )


def transition_audit_action(current: str, target: str) -> str | None:
    if current == target:
        return None
    validate_status_transition(current, target)
    return STATUS_AUDIT_ACTIONS[target]


def tenant_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Serialise a tenant row for API responses (ISO timestamps, JSON-safe scalars, no internals)."""
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "code": row["code"],
        "name": row["name"],
        "base_currency_code": row["base_currency_code"],
        "timezone": row["timezone"],
        "status": row["status"],
        "negative_inventory_policy": row["negative_inventory_policy"],
        "rounding_mode": row["rounding_mode"],
        "retention_ledger_years": row["retention_ledger_years"],
        "settings": dict(row.get("settings") or {}),
        "version": int(row["version"]),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "created_by": _iso(row.get("created_by")),
        "updated_by": _iso(row.get("updated_by")),
        "is_system": is_system_tenant(row["id"]),
    }


def settings_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(row.get("settings") or {})
    return {
        "negative_inventory_policy": row["negative_inventory_policy"],
        "rounding_mode": row["rounding_mode"],
        "retention_ledger_years": row["retention_ledger_years"],
        "settings_keys": sorted(settings)[:50],
        "settings": settings,
    }


def encode_cursor(created_at: Any, row_id: Any) -> str:
    payload = json.dumps({"t": _iso(created_at), "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        created_at = datetime.fromisoformat(str(data["t"]))
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return created_at.astimezone(UTC), _as_uuid(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise TenantValidationError("invalid pagination cursor", details={"field": "cursor"}) from None


def payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_idempotency_key(key: str) -> str:
    if not key or not IDEMPOTENCY_KEY_RE.match(key):
        raise TenantValidationError("invalid Idempotency-Key", details={"field": "idempotency_key"})
    return key


# --------------------------------------------------------------------------- repository --------
class PlatformAdminRepository:
    """Request-scoped repository; the request dependency owns the transaction (no commits here)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- audit ---------------------------------------------------------------------------------
    async def audit(
        self,
        *,
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
            pg_insert(p.audit_event).values(
                id=event_id,
                tenant_id=_SYSTEM_ID,
                event_id=event_id,
                actor_type=actor_type,
                actor_id=_as_uuid(actor_id) if actor_id is not None else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source=source,
                occurred_at=_now(),
            )
        )
        await audit_recorded(
            self.session, tenant_id=str(_SYSTEM_ID), event_id=str(event_id), actor_type=actor_type,
            action=action, resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            occurred_at=_now().isoformat(),
        )
        return event_id

    # -- reads ---------------------------------------------------------------------------------
    async def is_platform_admin(self, identity_id: str | UUID) -> bool:
        row = await self.session.execute(
            select(p.user_identity.c.is_platform_admin).where(p.user_identity.c.id == _as_uuid(identity_id))
        )
        return bool(row.scalar_one_or_none())

    async def get_tenant(self, tenant_id: str | UUID) -> dict[str, Any] | None:
        row = await self.session.execute(select(p.tenant).where(p.tenant.c.id == _as_uuid(tenant_id)))
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def get_tenant_by_code(self, code: str) -> dict[str, Any] | None:
        row = await self.session.execute(select(p.tenant).where(p.tenant.c.code == normalise_code(code)))
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def list_tenants(
        self,
        *,
        status: str | None = None,
        code: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        stmt = select(p.tenant).where(p.tenant.c.id != _SYSTEM_ID)
        if status:
            normalised = str(status).upper()
            if normalised not in TENANT_STATUSES:
                raise TenantValidationError("unknown tenant status filter", details={"field": "status"})
            stmt = stmt.where(p.tenant.c.status == normalised)
        if code:
            stmt = stmt.where(p.tenant.c.code == normalise_code(code))
        if cursor:
            created_at, last_id = decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    p.tenant.c.created_at < created_at,
                    and_(p.tenant.c.created_at == created_at, p.tenant.c.id < last_id),
                )
            )
        stmt = stmt.order_by(p.tenant.c.created_at.desc(), p.tenant.c.id.desc()).limit(limit + 1)
        rows = [dict(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])
        return [tenant_view(row) for row in rows], next_cursor

    async def membership_count(self, tenant_id: str | UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(p.app_user).where(p.app_user.c.tenant_id == _as_uuid(tenant_id))
        )
        return int(result.scalar_one() or 0)

    async def list_memberships(
        self,
        tenant_id: str | UUID,
        *,
        membership_status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> tuple[list[dict[str, Any]], str | None]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        tid = _as_uuid(tenant_id)
        stmt = (
            select(
                p.app_user.c.id.label("app_user_id"),
                p.app_user.c.identity_id,
                p.app_user.c.email,
                p.app_user.c.display_name,
                p.app_user.c.status,
                p.app_user.c.membership_status,
                p.app_user.c.joined_at,
                p.app_user.c.is_service_account,
                p.app_user.c.created_at,
                p.user_identity.c.email.label("identity_email"),
                p.user_identity.c.is_platform_admin,
            )
            .select_from(p.app_user.join(p.user_identity, p.user_identity.c.id == p.app_user.c.identity_id))
            .where(p.app_user.c.tenant_id == tid)
        )
        if membership_status:
            normalised = str(membership_status).upper()
            if normalised not in {"INVITED", "ACTIVE", "SUSPENDED"}:
                raise TenantValidationError("unknown membership_status filter", details={"field": "membership_status"})
            stmt = stmt.where(p.app_user.c.membership_status == normalised)
        if cursor:
            created_at, last_id = decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    p.app_user.c.created_at < created_at,
                    and_(p.app_user.c.created_at == created_at, p.app_user.c.id < last_id),
                )
            )
        stmt = stmt.order_by(p.app_user.c.created_at.desc(), p.app_user.c.id.desc()).limit(limit + 1)
        rows = [dict(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["app_user_id"])
        await self._attach_roles(tid, rows)
        items = [self._membership_view(row) for row in rows]
        return items, next_cursor

    async def _attach_roles(self, tenant_id: UUID, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ids = [row["app_user_id"] for row in rows]
        stmt = (
            select(p.user_role.c.user_id, p.role.c.code)
            .select_from(
                p.user_role.join(
                    p.role,
                    and_(p.role.c.tenant_id == p.user_role.c.tenant_id, p.role.c.id == p.user_role.c.role_id),
                )
            )
            .where(
                p.user_role.c.tenant_id == tenant_id,
                p.user_role.c.user_id.in_(ids),
                p.user_role.c.effective_to.is_(None),
            )
            .order_by(p.user_role.c.user_id, p.role.c.code)
        )
        by_user: dict[UUID, list[str]] = {}
        for user_id, role_code in (await self.session.execute(stmt)).all():
            by_user.setdefault(user_id, []).append(str(role_code))
        for row in rows:
            row["roles"] = by_user.get(row["app_user_id"], [])

    @staticmethod
    def _membership_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "app_user_id": str(row["app_user_id"]),
            "identity_id": str(row["identity_id"]),
            "email": str(row.get("identity_email") or row.get("email") or ""),
            "display_name": row.get("display_name"),
            "status": row["status"],
            "membership_status": row["membership_status"],
            "roles": list(row.get("roles") or []),
            "is_platform_admin": bool(row.get("is_platform_admin")),
            "is_service_account": bool(row.get("is_service_account")),
            "joined_at": _iso(row.get("joined_at")),
            "created_at": _iso(row.get("created_at")),
        }

    # -- writes --------------------------------------------------------------------------------
    async def create_tenant(
        self,
        *,
        data: Mapping[str, Any],
        actor_identity_id: str | UUID | None,
    ) -> dict[str, Any]:
        code = str(data["code"])
        existing = await self.get_tenant_by_code(code)
        if existing is not None:
            raise TenantConflictError("tenant code already exists", details={"field": "code", "code": code})
        currency = data.get("base_currency_code")
        if currency and not await self._currency_exists(str(currency)):
            raise TenantValidationError(
                "base currency is not an active platform currency",
                details={"field": "base_currency_code", "value": currency},
            )
        tenant_id = uuid4()
        now = _now()
        values = {
            "id": tenant_id,
            "tenant_id": tenant_id,
            "code": code,
            "name": str(data["name"]),
            "base_currency_code": currency,
            "timezone": str(data["timezone"]),
            "status": str(data["status"]),
            "negative_inventory_policy": str(data["negative_inventory_policy"]),
            "rounding_mode": str(data["rounding_mode"]),
            "retention_ledger_years": int(data["retention_ledger_years"]),
            "settings": dict(data.get("settings") or {}),
            "created_at": now,
            "updated_at": now,
            "created_by": _as_uuid(actor_identity_id) if actor_identity_id else None,
            "updated_by": _as_uuid(actor_identity_id) if actor_identity_id else None,
            "version": 1,
        }
        try:
            await self.session.execute(p.tenant.insert().values(**values))
            # FS-00 tenant provisioning: clone sequences/settings/flags + the default enabled
            # INVENTORY_COUNT approval definition (idempotent; same transaction as the tenant row).
            await provision_tenant_defaults(self.session, tenant_id)
        except Exception as exc:  # pragma: no cover - unique race maps to the same conflict
            if "uq_tenant_code" in str(exc):
                raise TenantConflictError("tenant code already exists", details={"code": code}) from exc
            raise
        row = await self.get_tenant(tenant_id)
        assert row is not None
        await emit_platform_event(
            self.session, "tenant.provisioned", tenant_id=str(tenant_id), aggregate_type="tenant",
            aggregate_id=str(tenant_id),
            payload={"tenant_id": str(tenant_id), "code": str(row["code"]),
                     "base_currency_code": row.get("base_currency_code"),
                     "timezone": str(row["timezone"]), "seeded": {"roles": 7, "sequences": 27}},
        )
        return row

    async def update_tenant(
        self,
        *,
        tenant_id: str | UUID,
        patch: Mapping[str, Any],
        actor_identity_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        current = await self.get_tenant(tenant_id)
        if current is None:
            raise TenantNotFoundError("tenant not found")
        if is_system_tenant(tenant_id):
            raise ReservedTenantError("the SYSTEM tenant is reserved and cannot be modified")
        current_status = str(current["status"])
        current_version = int(current["version"])
        if expected_version is not None and expected_version != current_version:
            raise TenantVersionConflictError(
                "tenant version does not match If-Match",
                details={"current_version": current_version, "expected_version": expected_version},
            )
        changed = sorted(patch)
        target_status = str(patch.get("status", current_status))
        action = transition_audit_action(current_status, target_status)
        values: dict[str, Any] = {key: value for key, value in patch.items() if key != "status"}
        values["status"] = target_status
        values["updated_by"] = _as_uuid(actor_identity_id) if actor_identity_id else None
        values["updated_at"] = _now()
        values["version"] = current_version + 1
        stmt = (
            update(p.tenant)
            .where(p.tenant.c.id == _as_uuid(tenant_id), p.tenant.c.version == current_version)
            .values(**values)
        )
        result = await self.session.execute(stmt)
        if getattr(result, "rowcount", 1) == 0:
            raise TenantVersionConflictError("tenant was modified by another request", details={"field": "If-Match"})
        sessions_revoked = 0
        if target_status in {"SUSPENDED", "OFFBOARDING"} and target_status != current_status:
            sessions_revoked = await self.revoke_sessions(tenant_id, reason=f"tenant.{target_status.lower()}")
        updated = await self.get_tenant(tenant_id)
        assert updated is not None
        before = {key: _iso(current.get(key)) for key in changed if key in current}
        after = {key: _iso(updated.get(key)) for key in changed}
        await self.audit(
            actor_id=actor_identity_id,
            action=action or "tenant.updated",
            resource_type="tenant",
            resource_id=tenant_id,
            diff={
                "changed_fields": changed,
                "before": before,
                "after": after,
                "reason": patch.get("reason"),
                "sessions_revoked": sessions_revoked,
            },
        )
        return updated, changed

    async def transition_status(
        self,
        *,
        tenant_id: str | UUID,
        target_status: str,
        reason: str,
        actor_identity_id: str | UUID | None,
    ) -> tuple[dict[str, Any], bool, int]:
        current = await self.get_tenant(tenant_id)
        if current is None:
            raise TenantNotFoundError("tenant not found")
        if is_system_tenant(tenant_id):
            raise ReservedTenantError("the SYSTEM tenant is reserved and cannot be modified")
        current_status = str(current["status"])
        target = str(target_status).upper()
        already = current_status == target
        validate_status_transition(current_status, target)
        if already:
            return current, True, 0
        sessions_revoked = 0
        if target in {"SUSPENDED", "OFFBOARDING"}:
            sessions_revoked = await self.revoke_sessions(tenant_id, reason=f"tenant.{target.lower()}")
        stmt = (
            update(p.tenant)
            .where(
                p.tenant.c.id == _as_uuid(tenant_id),
                p.tenant.c.version == int(current["version"]),
                p.tenant.c.status == current_status,
            )
            .values(
                status=target,
                updated_by=_as_uuid(actor_identity_id) if actor_identity_id else None,
                updated_at=_now(),
                version=int(current["version"]) + 1,
            )
        )
        result = await self.session.execute(stmt)
        if getattr(result, "rowcount", 1) == 0:
            raise TenantTransitionError("tenant changed concurrently; retry the transition")
        updated = await self.get_tenant(tenant_id)
        assert updated is not None
        await self.audit(
            actor_id=actor_identity_id,
            action=STATUS_AUDIT_ACTIONS[target],
            resource_type="tenant",
            resource_id=tenant_id,
            diff={
                "from": current_status,
                "to": target,
                "reason": reason,
                "sessions_revoked": sessions_revoked,
            },
        )
        if target == "SUSPENDED":
            await emit_platform_event(
                self.session, "tenant.suspended", tenant_id=str(tenant_id), aggregate_type="tenant",
                aggregate_id=str(tenant_id),
                payload={"tenant_id": str(tenant_id), "reason": reason,
                         "sessions_revoked": sessions_revoked,
                         "actor_identity_id": str(actor_identity_id) if actor_identity_id else None},
            )
        return updated, False, sessions_revoked

    async def revoke_sessions(self, tenant_id: str | UUID, *, reason: str) -> int:
        stmt = (
            update(p.user_session)
            .where(p.user_session.c.tenant_id == _as_uuid(tenant_id), p.user_session.c.status == "ACTIVE")
            .values(
                status="REVOKED",
                revoked_at=_now(),
                revoked_reason=reason[:200],
                updated_at=_now(),
                version=p.user_session.c.version + 1,
            )
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def _currency_exists(self, code: str) -> bool:
        row = await self.session.execute(
            select(p.currency.c.code).where(p.currency.c.code == code, p.currency.c.is_active.is_(True))
        )
        return row.scalar_one_or_none() is not None

    # -- idempotency ---------------------------------------------------------------------------
    async def begin_idempotent(
        self,
        *,
        endpoint_id: str,
        key: str,
        request_hash: str,
        actor_identity_id: str | UUID | None,
    ) -> dict[str, Any] | None:
        """Reserve an idempotency key. Returns a stored replay payload for completed duplicates."""
        validate_idempotency_key(key)
        insert_result = await self.session.execute(
            pg_insert(p.idempotency_key)
            .values(
                id=uuid4(),
                tenant_id=_SYSTEM_ID,
                endpoint_id=endpoint_id,
                key_value=key,
                request_hash=request_hash,
                status="IN_PROGRESS",
                expires_at=_now() + IDEMPOTENCY_TTL,
                created_at=_now(),
                updated_at=_now(),
                created_by=_as_uuid(actor_identity_id) if actor_identity_id else None,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "endpoint_id", "key_value"])
        )
        if int(getattr(insert_result, "rowcount", 0) or 0) == 1:
            return None  # this request reserved the key and should proceed
        row = await self._idempotency_row(endpoint_id, key)
        if row is None:  # pragma: no cover - conflict without a readable row
            return None
        if str(row["request_hash"]) != request_hash:
            raise IdempotencyError(
                "Idempotency-Key was reused with a different request payload",
                details={"field": "Idempotency-Key"},
            )
        if row["status"] == "COMPLETED":
            return {
                "response_status": int(row["response_status"] or 200),
                "response_body": dict(row["response_body"] or {}),
                "resource_id": str(row["resource_id"]) if row["resource_id"] else None,
            }
        raise IdempotencyInProgressError("a request with this Idempotency-Key is already in progress")

    async def complete_idempotent(
        self,
        *,
        endpoint_id: str,
        key: str,
        response_status: int,
        response_body: Mapping[str, Any],
        resource_id: str | UUID | None,
    ) -> None:
        await self.session.execute(
            update(p.idempotency_key)
            .where(
                p.idempotency_key.c.tenant_id == _SYSTEM_ID,
                p.idempotency_key.c.endpoint_id == endpoint_id,
                p.idempotency_key.c.key_value == key,
            )
            .values(
                status="COMPLETED",
                response_status=int(response_status),
                response_body=dict(response_body),
                resource_id=_as_uuid(resource_id) if resource_id is not None else None,
                updated_at=_now(),
                version=p.idempotency_key.c.version + 1,
            )
        )

    async def _idempotency_row(self, endpoint_id: str, key: str) -> dict[str, Any] | None:
        row = await self.session.execute(
            select(p.idempotency_key).where(
                p.idempotency_key.c.tenant_id == _SYSTEM_ID,
                p.idempotency_key.c.endpoint_id == endpoint_id,
                p.idempotency_key.c.key_value == key,
            )
        )
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "NEGATIVE_INVENTORY_POLICIES",
    "PATCHABLE_FIELDS",
    "PROVISIONABLE_STATUSES",
    "PlatformAdminError",
    "PlatformAdminRepository",
    "ROUNDING_MODES",
    "ReservedTenantError",
    "TENANT_STATUSES",
    "TenantConflictError",
    "TenantNotFoundError",
    "TenantTransitionError",
    "TenantValidationError",
    "TenantVersionConflictError",
    "decode_cursor",
    "encode_cursor",
    "is_system_tenant",
    "normalise_code",
    "payload_hash",
    "settings_summary",
    "tenant_view",
    "transition_audit_action",
    "validate_create",
    "validate_email",
    "validate_idempotency_key",
    "validate_name",
    "validate_patch",
    "validate_reason",
    "validate_status_transition",
    "validate_timezone",
]
