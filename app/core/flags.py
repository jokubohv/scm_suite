"""Per-tenant feature flags (FS-00 `flags.is_enabled`, DM-PLAT-05).

Resolution: tenant ``feature_flag`` row → declared seed default → **OFF**. Every declared flag defaults
off (seed loader enforces ``default_enabled is False``); enabling a flag is a tenant-scoped override.
Reads are cached in-process for hot paths and the cache has an explicit ``invalidate()`` hook (role/flag
writers call it; multi-instance deployments should use the platform cache/event bus at integration time).

No HTTP/ORM imports; async SQLAlchemy Core only. ``require_flag`` raises a ``SecurityError`` subclass
(``DOMAIN_DISABLED``, HTTP 409) so API handlers can use the standard problem+json envelope.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import feature_flag
from app.core.security.errors import SecurityError

SEED_FLAGS_PATH = Path(__file__).resolve().parents[1] / "seed" / "data" / "feature_flags.json"
FLAG_KEY_SUFFIX = ".enabled"


class FeatureFlagError(Exception):
    code = "FEATURE_FLAG_ERROR"


class UnknownFlag(FeatureFlagError):
    code = "UNKNOWN_FEATURE_FLAG"


class FeatureDisabled(SecurityError):
    """Raised by ``require_flag`` for a disabled (default-off) feature (HTTP 409 DOMAIN_DISABLED)."""

    code = "DOMAIN_DISABLED"
    http_status = 409


class InvalidFlagChange(FeatureFlagError):
    code = "INVALID_FEATURE_FLAG_CHANGE"


@dataclass(frozen=True, slots=True)
class FlagDefinition:
    flag_key: str
    default_enabled: bool = False
    owner_feature_set: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class FlagResolution:
    flag_key: str
    tenant_id: UUID | None
    enabled: bool
    source: str  # DEFAULT | OVERRIDE | ENV
    default_enabled: bool = False


@dataclass(frozen=True, slots=True)
class FlagChange:
    tenant_id: UUID
    flag_key: str
    enabled: bool
    previous_enabled: bool | None
    actor_id: UUID | None
    changed_at: datetime
    notes: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def declared_flags() -> Mapping[str, FlagDefinition]:
    """Read-only seed flag catalogue (17 flags, all default OFF)."""
    try:
        records: list[dict[str, Any]] = json.loads(SEED_FLAGS_PATH.read_text(encoding="utf-8"))["records"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:  # pragma: no cover - corrupted deployment
        raise FeatureFlagError(f"cannot load seed feature flags: {SEED_FLAGS_PATH}") from exc
    definitions: dict[str, FlagDefinition] = {}
    for record in records:
        definition = FlagDefinition(
            flag_key=str(record["flag_key"]),
            default_enabled=bool(record.get("default_enabled", False)),
            owner_feature_set=str(record.get("owner_feature_set", "")),
            description=str(record.get("description", "")),
        )
        if not definition.flag_key.endswith(FLAG_KEY_SUFFIX):
            raise FeatureFlagError(f"feature flag {definition.flag_key!r} must end with {FLAG_KEY_SUFFIX!r}")
        if definition.default_enabled:
            raise FeatureFlagError(f"declared feature flag {definition.flag_key!r} must default OFF")
        if definition.flag_key in definitions:
            raise FeatureFlagError(f"duplicate declared feature flag {definition.flag_key!r}")
        definitions[definition.flag_key] = definition
    return definitions


class FlagService:
    """Tenant flag resolver with default-off semantics, cache and write/invalidation hooks."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        declarations: Mapping[str, FlagDefinition] | None = None,
        audit: Callable[[FlagChange], None] | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self.session = session
        self.declarations: dict[str, FlagDefinition] = (
            dict(declarations) if declarations is not None else dict(declared_flags())
        )
        self.audit = audit
        self._cache_enabled = cache_enabled
        self._cache: dict[tuple[str, str], FlagResolution] = {}

    # -- catalogue ----------------------------------------------------------------------------

    def declared(self) -> Mapping[str, FlagDefinition]:
        return dict(self.declarations)

    def definition(self, flag_key: str, *, allow_undeclared: bool = False) -> FlagDefinition:
        key = str(flag_key or "").strip()
        if not key:
            raise InvalidFlagChange("flag_key is required")
        if key in self.declarations:
            return self.declarations[key]
        if allow_undeclared and key.endswith(FLAG_KEY_SUFFIX):
            return FlagDefinition(flag_key=key, default_enabled=False)
        raise UnknownFlag(f"feature flag {key!r} is not declared in the seed catalogue")

    # -- read ---------------------------------------------------------------------------------

    async def get(self, tenant_id: UUID | None, flag_key: str, *, allow_undeclared: bool = False) -> FlagResolution:
        definition = self.definition(flag_key, allow_undeclared=allow_undeclared)
        key = definition.flag_key
        cache_key = ("", key) if tenant_id is None else (str(tenant_id), key)
        if self._cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]
        if tenant_id is not None:
            result = await self.session.execute(
                select(feature_flag.c.is_enabled, feature_flag.c.source).where(
                    feature_flag.c.tenant_id == tenant_id,
                    feature_flag.c.flag_key == key,
                )
            )
            row = result.mappings().first()
            if row is not None:
                resolution = FlagResolution(
                    flag_key=key,
                    tenant_id=tenant_id,
                    enabled=bool(row["is_enabled"]),
                    source=str(row["source"] or "OVERRIDE"),
                    default_enabled=definition.default_enabled,
                )
                if self._cache_enabled:
                    self._cache[cache_key] = resolution
                return resolution
        resolution = FlagResolution(
            flag_key=key,
            tenant_id=tenant_id,
            enabled=definition.default_enabled,
            source="DEFAULT",
            default_enabled=definition.default_enabled,
        )
        if self._cache_enabled:
            self._cache[cache_key] = resolution
        return resolution

    async def is_enabled(self, tenant_id: UUID | None, flag_key: str, *, allow_undeclared: bool = False) -> bool:
        return (await self.get(tenant_id, flag_key, allow_undeclared=allow_undeclared)).enabled

    async def require_flag(self, tenant_id: UUID | None, flag_key: str, *, allow_undeclared: bool = False) -> None:
        resolution = await self.get(tenant_id, flag_key, allow_undeclared=allow_undeclared)
        if not resolution.enabled:
            raise FeatureDisabled(
                f"feature {flag_key!r} is disabled for this tenant",
                details={"flag_key": flag_key, "tenant_id": str(tenant_id) if tenant_id else None},
            )

    async def list_flags(self, tenant_id: UUID | None) -> dict[str, FlagResolution]:
        resolutions: dict[str, FlagResolution] = {}
        for flag_key in sorted(self.declarations):
            resolutions[flag_key] = await self.get(tenant_id, flag_key)
        return resolutions

    # -- write / invalidation -----------------------------------------------------------------

    async def set_flag(
        self,
        tenant_id: UUID,
        flag_key: str,
        enabled: bool,
        *,
        actor_id: UUID | None = None,
        notes: str | None = None,
        source: str = "OVERRIDE",
        now: datetime | None = None,
        allow_undeclared: bool = False,
    ) -> FlagResolution:
        """Set the tenant override (upsert), record change metadata, audit and invalidate the cache."""
        definition = self.definition(flag_key, allow_undeclared=allow_undeclared)
        if source not in {"DEFAULT", "OVERRIDE", "ENV"}:
            raise InvalidFlagChange(f"invalid flag source {source!r}")
        changed_at = now or utc_now()
        previous = await self.get(tenant_id, definition.flag_key, allow_undeclared=allow_undeclared)
        statement = pg_insert(feature_flag).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            flag_key=definition.flag_key,
            is_enabled=bool(enabled),
            source=source,
            changed_by=actor_id,
            changed_at=changed_at,
            notes=notes,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[feature_flag.c.tenant_id, feature_flag.c.flag_key],
            set_={
                "is_enabled": bool(enabled),
                "source": source,
                "changed_by": actor_id,
                "changed_at": changed_at,
                "notes": notes,
            },
        )
        await self.session.execute(statement)
        self.invalidate(tenant_id=tenant_id, flag_key=definition.flag_key)
        resolution = FlagResolution(
            flag_key=definition.flag_key,
            tenant_id=tenant_id,
            enabled=bool(enabled),
            source=source,
            default_enabled=definition.default_enabled,
        )
        if self.audit is not None:
            self.audit(
                FlagChange(
                    tenant_id=tenant_id,
                    flag_key=definition.flag_key,
                    enabled=bool(enabled),
                    previous_enabled=previous.enabled,
                    actor_id=actor_id,
                    changed_at=changed_at,
                    notes=notes,
                )
            )
        return resolution

    def invalidate(self, *, tenant_id: UUID | None = None, flag_key: str | None = None) -> None:
        """Explicit cache invalidation hook (``tenant_id``/``flag_key`` filters optional)."""
        if tenant_id is None and flag_key is None:
            self._cache.clear()
            return
        target_tenant = str(tenant_id) if tenant_id is not None else None
        for cache_key in list(self._cache):
            cached_tenant, cached_flag = cache_key
            if target_tenant is not None and cached_tenant != target_tenant:
                continue
            if flag_key is not None and cached_flag != flag_key:
                continue
            del self._cache[cache_key]


__all__ = [
    "FLAG_KEY_SUFFIX",
    "SEED_FLAGS_PATH",
    "FeatureDisabled",
    "FeatureFlagError",
    "FlagChange",
    "FlagDefinition",
    "FlagResolution",
    "FlagService",
    "InvalidFlagChange",
    "UnknownFlag",
    "declared_flags",
    "utc_now",
]
