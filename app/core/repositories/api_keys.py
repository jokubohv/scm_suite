"""API-key lifecycle repository (FS-00 §4/§6; task-43).

Tenant-scoped access to ``platform.api_key`` plus the pure generation/validation/rotation rules.
Only the prefix + HMAC-SHA256(secret, pepper) digest are persisted; the full secret is returned to the
caller exactly once at create/rotate time. Every mutation appends an ``audit_event`` (never containing
secret material). No commits here: the request-scoped session owns the transaction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as p
from app.core.security.errors import UnknownScope
from app.core.security.scopes import ScopeRegistry, SodContext
from app.core.security.tokens import ApiKeyMaterial

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def derive_api_key_pepper(secret: str) -> bytes:
    """Domain-separate the API-key pepper from the JWT signing secret (no new config surface)."""
    if not secret:
        raise ValueError("API-key pepper source must not be empty")
    return hashlib.sha256(f"{secret}:platform-api-key".encode()).digest()


def is_key_active(row: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """True only when the key is neither revoked nor expired."""
    if row.get("revoked_at") is not None:
        return False
    expires_at = row.get("expires_at")
    if expires_at is None:
        return True
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return expires_at > moment


def validate_api_key_scopes(registry: ScopeRegistry, scopes: Iterable[str]) -> tuple[str, ...]:
    """Normalise/validate machine-credential scopes against the authoritative catalogue.

    Rules (FS-00 §4 / Δ-RBAC): every scope must exist and be assignable (never platform-only or
    service-only), and the set must not combine an SoD pair (assignment context)."""
    requested = list(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
    if not requested:
        raise UnknownScope("at least one capability scope is required", details={"field": "scopes"})
    canonical = registry.require_assignable(requested)
    registry.require_no_sod_conflict(canonical, context=SodContext.ASSIGNMENT)
    return tuple(sorted(canonical))


def validate_expiry(expires_at: datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Reject a past expiry; normalise to UTC."""
    if expires_at is None:
        return None
    moment = now or _now()
    normalised = expires_at.astimezone(UTC) if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    if normalised <= moment:
        raise ValueError("expires_at must be in the future")
    return normalised


def public_key_row(row: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Serialise an api_key row for API output — no secret/hash material is ever included."""
    moment = now or _now()
    expires_at = row.get("expires_at")
    revoked_at = row.get("revoked_at")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "scopes": list(row.get("scopes") or []),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "expires_at": expires_at,
        "last_used_at": row.get("last_used_at"),
        "revoked_at": revoked_at,
        "revoked": revoked_at is not None,
        "expired": bool(expires_at is not None and expires_at <= moment),
        "version": row.get("version", 1),
    }


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """Create/rotate result: the one-time secret plus the persisted (safe) row."""

    row: Mapping[str, Any]
    material: ApiKeyMaterial


class ApiKeyRepository:
    """Request-scoped repository. Tenant scoping is enforced on every query."""

    def __init__(self, session: AsyncSession, *, registry: ScopeRegistry, hasher: Any,
                 clock: Clock = _now) -> None:
        self.session = session
        self.registry = registry
        self.hasher = hasher
        self._clock = clock

    # ------------------------------------------------------------------ audit ---------------
    async def _audit(self, *, tenant_id: UUID | str, actor_id: UUID | str | None, action: str,
                     key_id: UUID | str | None, diff: Mapping[str, Any] | None = None) -> None:
        event_id = uuid4()
        await self.session.execute(
            insert(p.audit_event).values(
                id=event_id,
                tenant_id=_as_uuid(tenant_id),
                event_id=event_id,
                actor_type="USER",
                actor_id=_as_uuid(actor_id),
                action=action,
                resource_type="api_key",
                resource_id=str(key_id) if key_id is not None else None,
                diff=dict(diff or {}),
                source="API",
                occurred_at=self._clock(),
            )
        )

    # ------------------------------------------------------------------ reads ---------------
    async def get(self, tenant_id: UUID | str, key_id: UUID | str) -> Mapping[str, Any] | None:
        row = await self.session.execute(
            select(p.api_key).where(p.api_key.c.tenant_id == _as_uuid(tenant_id),
                                    p.api_key.c.id == _as_uuid(key_id))
        )
        mapping = row.mappings().first()
        return dict(mapping) if mapping else None

    async def list(self, tenant_id: UUID | str, *, include_revoked: bool = False,
                   include_expired: bool = True) -> list[dict[str, Any]]:
        stmt = select(p.api_key).where(p.api_key.c.tenant_id == _as_uuid(tenant_id))
        if not include_revoked:
            stmt = stmt.where(p.api_key.c.revoked_at.is_(None))
        rows = await self.session.execute(stmt.order_by(p.api_key.c.created_at.desc(), p.api_key.c.id))
        moment = self._clock()
        items = [dict(row) for row in rows.mappings().all()]
        if include_expired:
            return items
        return [item for item in items if not item.get("expires_at") or item["expires_at"] > moment]

    # ------------------------------------------------------------------ writes --------------
    async def create(self, *, tenant_id: UUID | str, actor_id: UUID | str | None, name: str,
                     scopes: Sequence[str], expires_at: datetime | None = None) -> IssuedApiKey:
        canonical = validate_api_key_scopes(self.registry, scopes)
        expiry = validate_expiry(expires_at, now=self._clock())
        material = self.hasher.generate()
        key_id = uuid4()
        await self.session.execute(
            insert(p.api_key).values(
                id=key_id, tenant_id=_as_uuid(tenant_id), name=name.strip(),
                key_prefix=material.key_prefix, secret_hash=material.secret_hash,
                scopes=list(canonical), expires_at=expiry,
                created_by=_as_uuid(actor_id), updated_by=_as_uuid(actor_id),
            )
        )
        await self._audit(tenant_id=tenant_id, actor_id=actor_id, action="api_key.created",
                          key_id=key_id, diff={"name": name.strip(), "scopes": list(canonical),
                                               "expires_at": expiry.isoformat() if expiry else None})
        row = await self.get(tenant_id, key_id)
        assert row is not None
        return IssuedApiKey(row=row, material=material)

    async def revoke(self, *, tenant_id: UUID | str, actor_id: UUID | str | None,
                     key_id: UUID | str, reason: str | None = None) -> Mapping[str, Any] | None:
        row = await self.get(tenant_id, key_id)
        if row is None:
            return None
        if row.get("revoked_at") is None:
            await self.session.execute(
                update(p.api_key)
                .where(p.api_key.c.tenant_id == _as_uuid(tenant_id),
                       p.api_key.c.id == _as_uuid(key_id),
                       p.api_key.c.revoked_at.is_(None))
                .values(revoked_at=self._clock(), updated_by=_as_uuid(actor_id),
                        version=p.api_key.c.version + 1)
            )
            await self._audit(tenant_id=tenant_id, actor_id=actor_id, action="api_key.revoked",
                              key_id=key_id, diff={"reason": reason, "key_prefix": row["key_prefix"]})
        updated = await self.get(tenant_id, key_id)
        return updated

    async def rotate(self, *, tenant_id: UUID | str, actor_id: UUID | str | None,
                     key_id: UUID | str) -> IssuedApiKey | None:
        row = await self.get(tenant_id, key_id)
        if row is None or row.get("revoked_at") is not None:
            return None
        material = self.hasher.generate()
        await self.session.execute(
            update(p.api_key)
            .where(p.api_key.c.tenant_id == _as_uuid(tenant_id), p.api_key.c.id == _as_uuid(key_id),
                   p.api_key.c.revoked_at.is_(None))
            .values(key_prefix=material.key_prefix, secret_hash=material.secret_hash,
                    updated_by=_as_uuid(actor_id), version=p.api_key.c.version + 1)
        )
        await self._audit(tenant_id=tenant_id, actor_id=actor_id, action="api_key.rotated",
                          key_id=key_id, diff={"old_prefix": row["key_prefix"],
                                               "new_prefix": material.key_prefix})
        updated = await self.get(tenant_id, key_id)
        assert updated is not None
        return IssuedApiKey(row=updated, material=material)
