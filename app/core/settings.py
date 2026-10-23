"""Typed tenant settings with documented precedence (FS-00 `settings.resolve`, DM-PLAT-07).

Resolution chain (highest first): **tenant row → global/system-tenant row → seed catalogue default →
caller-supplied default**. Values are typed via the declared :class:`SettingDefinition`
(``STRING``/``BOOL``/``NUMBER``/``JSON``) with allowed-value / range validation; namespaces and keys are
validated against the seed catalogue and the ``tenant_setting.namespace`` check constraint. Writes are
upserts (``set_setting``) with an optional audit callback receiving before/after values; no secret values
are logged by this module.

No HTTP/ORM imports; async SQLAlchemy Core only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.platform import tenant_settings as tenant_setting

SEED_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "seed" / "data" / "tenant_settings.json"

#: Namespaces allowed by the platform migration check constraint (seed `policy` rows are filtered out).
ALLOWED_NAMESPACES: frozenset[str] = frozenset(
    {
        "inventory",
        "manufacturing",
        "wms",
        "replenishment",
        "costing",
        "pricing",
        "platform",
        "accounting",
        "rbac",
        "auth",
    }
)

_UNSET = object()


class SettingsError(Exception):
    code = "SETTINGS_ERROR"


class InvalidNamespace(SettingsError):
    code = "INVALID_SETTINGS_NAMESPACE"


class UnknownSetting(SettingsError):
    code = "UNKNOWN_SETTING"


class InvalidSettingValue(SettingsError):
    code = "INVALID_SETTING_VALUE"


class SettingType(StrEnum):
    STRING = "STRING"
    BOOL = "BOOL"
    NUMBER = "NUMBER"
    JSON = "JSON"


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """Declared setting metadata + typed value validation."""

    namespace: str
    key: str
    value_type: SettingType
    default: Any = None
    allowed_values: tuple[Any, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.namespace not in ALLOWED_NAMESPACES:
            raise InvalidNamespace(f"namespace {self.namespace!r} is not allowed")
        if not self.key or not self.key.strip():
            raise InvalidSettingValue("setting key is required")
        object.__setattr__(self, "value_type", SettingType(str(self.value_type)))

    def validate(self, value: Any) -> Any:
        """Validate/coerce a stored or supplied value; raise :class:`InvalidSettingValue` on mismatch."""
        if value is None and self.value_type is SettingType.JSON:
            return None
        if self.value_type is SettingType.BOOL:
            if not isinstance(value, bool):
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be a boolean")
        elif self.value_type is SettingType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be a number")
            if self.min_value is not None and float(value) < self.min_value:
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be >= {self.min_value}")
            if self.max_value is not None and float(value) > self.max_value:
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be <= {self.max_value}")
        elif self.value_type is SettingType.STRING:
            if not isinstance(value, str):
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be a string")
        elif self.value_type is SettingType.JSON:
            if not isinstance(value, dict | list | str | int | float | bool):
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be JSON-serialisable")
            try:
                json.dumps(value)
            except (TypeError, ValueError) as exc:
                raise InvalidSettingValue(f"{self.namespace}.{self.key} must be JSON-serialisable") from exc
        if self.allowed_values and value not in self.allowed_values:
            raise InvalidSettingValue(
                f"{self.namespace}.{self.key} must be one of {sorted(map(str, self.allowed_values))}"
            )
        return value


@dataclass(frozen=True, slots=True)
class SettingResolution:
    namespace: str
    key: str
    value: Any
    value_type: SettingType
    source: str  # TENANT | GLOBAL | DEFAULT | CALLER
    tenant_id: UUID | None
    definition: SettingDefinition


@dataclass(frozen=True, slots=True)
class SettingChange:
    tenant_id: UUID
    namespace: str
    key: str
    old_value: Any
    new_value: Any
    actor_id: UUID | None
    changed_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def _definition_from_seed(record: Mapping[str, Any]) -> SettingDefinition:
    namespace = str(record["namespace"])
    key = str(record["key"])
    value_type = SettingType(str(record.get("value_type", SettingType.JSON)))
    allowed: tuple[Any, ...] = ()
    if namespace == "inventory" and key == "negative_inventory_policy":
        allowed = ("BLOCK", "WARN")
    elif namespace == "inventory" and key == "rounding_mode":
        allowed = ("PER_MOVEMENT", "DOCUMENT")
    elif namespace == "rbac" and key == "sod_enforcement":
        allowed = ("HARD", "WARN")
    return SettingDefinition(
        namespace=namespace,
        key=key,
        value_type=value_type,
        default=record.get("value"),
        allowed_values=allowed,
        min_value=0 if key == "retention_ledger_years" else None,
        description=str(record.get("description", "")),
    )


@lru_cache(maxsize=1)
def declared_settings() -> Mapping[tuple[str, str], SettingDefinition]:
    """Read-only seed setting catalogue (namespaces not allowed by the DB constraint are skipped)."""
    try:
        records: list[dict[str, Any]] = json.loads(SEED_SETTINGS_PATH.read_text(encoding="utf-8"))["records"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:  # pragma: no cover - corrupted deployment
        raise SettingsError(f"cannot load seed tenant settings: {SEED_SETTINGS_PATH}") from exc
    definitions: dict[tuple[str, str], SettingDefinition] = {}
    for record in records:
        if str(record["namespace"]) not in ALLOWED_NAMESPACES:
            continue
        definition = _definition_from_seed(record)
        definitions[(definition.namespace, definition.key)] = definition
    return definitions


class SettingsService:
    """Typed settings resolver/writer with precedence, cache and audit hook."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        definitions: Mapping[tuple[str, str], SettingDefinition] | None = None,
        global_tenant_id: UUID | None = None,
        audit: Callable[[SettingChange], None] | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self.session = session
        self.definitions: dict[tuple[str, str], SettingDefinition] = (
            dict(definitions) if definitions is not None else dict(declared_settings())
        )
        self.global_tenant_id = global_tenant_id
        self.audit = audit
        self._cache_enabled = cache_enabled
        self._cache: dict[tuple[str, str, str], SettingResolution] = {}

    # -- catalogue ----------------------------------------------------------------------------

    def declared(self) -> Mapping[tuple[str, str], SettingDefinition]:
        return dict(self.definitions)

    def definition(
        self,
        namespace: str,
        key: str,
        *,
        allow_undeclared: bool = False,
    ) -> SettingDefinition:
        normalised_namespace = str(namespace or "").strip()
        if normalised_namespace not in ALLOWED_NAMESPACES:
            raise InvalidNamespace(f"namespace {normalised_namespace!r} is not allowed")
        normalised_key = str(key or "").strip()
        if not normalised_key:
            raise InvalidSettingValue("setting key is required")
        definition = self.definitions.get((normalised_namespace, normalised_key))
        if definition is not None:
            return definition
        if allow_undeclared:
            return SettingDefinition(
                namespace=normalised_namespace,
                key=normalised_key,
                value_type=SettingType.JSON,
                default=None,
            )
        raise UnknownSetting(f"setting {normalised_namespace}.{normalised_key} is not declared")

    # -- read ---------------------------------------------------------------------------------

    async def resolve(
        self,
        tenant_id: UUID,
        namespace: str,
        key: str,
        *,
        default: Any = _UNSET,
        allow_undeclared: bool = False,
    ) -> SettingResolution:
        definition = self.definition(namespace, key, allow_undeclared=allow_undeclared or default is not _UNSET)
        cache_key = (str(tenant_id), definition.namespace, definition.key)
        if self._cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]

        row = await self._select_row(tenant_id, definition.namespace, definition.key)
        if row is not None:
            return self._remember(
                cache_key,
                SettingResolution(
                    namespace=definition.namespace,
                    key=definition.key,
                    value=definition.validate(row["value"]),
                    value_type=definition.value_type,
                    source="DEFAULT" if row["is_default"] else "TENANT",
                    tenant_id=tenant_id,
                    definition=definition,
                ),
            )
        if self.global_tenant_id is not None and self.global_tenant_id != tenant_id:
            global_row = await self._select_row(self.global_tenant_id, definition.namespace, definition.key)
            if global_row is not None:
                return self._remember(
                    cache_key,
                    SettingResolution(
                        namespace=definition.namespace,
                        key=definition.key,
                        value=definition.validate(global_row["value"]),
                        value_type=definition.value_type,
                        source="GLOBAL",
                        tenant_id=tenant_id,
                        definition=definition,
                    ),
                )
        if definition.default is not None:
            return self._remember(
                cache_key,
                SettingResolution(
                    namespace=definition.namespace,
                    key=definition.key,
                    value=definition.validate(definition.default),
                    value_type=definition.value_type,
                    source="DEFAULT",
                    tenant_id=tenant_id,
                    definition=definition,
                ),
            )
        if default is not _UNSET:
            return SettingResolution(
                namespace=definition.namespace,
                key=definition.key,
                value=default,
                value_type=definition.value_type,
                source="CALLER",
                tenant_id=tenant_id,
                definition=definition,
            )
        raise UnknownSetting(f"setting {definition.namespace}.{definition.key} has no value and no default")

    async def get_value(
        self,
        tenant_id: UUID,
        namespace: str,
        key: str,
        *,
        default: Any = _UNSET,
        allow_undeclared: bool = False,
    ) -> Any:
        resolution = await self.resolve(tenant_id, namespace, key, default=default, allow_undeclared=allow_undeclared)
        return resolution.value

    async def effective_namespace(self, tenant_id: UUID, namespace: str) -> dict[str, SettingResolution]:
        """Resolve every declared key in a namespace (tenant overrides win; useful for admin views)."""
        if namespace not in ALLOWED_NAMESPACES:
            raise InvalidNamespace(f"namespace {namespace!r} is not allowed")
        resolutions: dict[str, SettingResolution] = {}
        for (declared_namespace, key) in sorted(self.definitions):
            if declared_namespace != namespace:
                continue
            resolutions[key] = await self.resolve(tenant_id, namespace, key)
        return resolutions

    async def _select_row(self, tenant_id: UUID, namespace: str, key: str) -> Any:
        result = await self.session.execute(
            select(tenant_setting).where(
                tenant_setting.c.tenant_id == tenant_id,
                tenant_setting.c.namespace == namespace,
                tenant_setting.c.key == key,
            )
        )
        return result.mappings().first()

    def _remember(self, cache_key: tuple[str, str, str], resolution: SettingResolution) -> SettingResolution:
        if self._cache_enabled:
            self._cache[cache_key] = resolution
        return resolution

    # -- write / invalidation -----------------------------------------------------------------

    async def set_setting(
        self,
        tenant_id: UUID,
        namespace: str,
        key: str,
        value: Any,
        *,
        actor_id: UUID | None = None,
        now: datetime | None = None,
        allow_undeclared: bool = False,
    ) -> SettingResolution:
        """Validate + upsert one tenant setting; invokes the audit callback with before/after values."""
        definition = self.definition(namespace, key, allow_undeclared=allow_undeclared)
        validated = definition.validate(value)
        previous: Any = None
        try:
            previous = await self.get_value(tenant_id, definition.namespace, definition.key, allow_undeclared=allow_undeclared)
        except UnknownSetting:
            previous = None
        changed_at = now or utc_now()
        statement = pg_insert(tenant_setting).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            namespace=definition.namespace,
            key=definition.key,
            value=validated,
            value_type=definition.value_type.value,
            is_default=False,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[tenant_setting.c.tenant_id, tenant_setting.c.namespace, tenant_setting.c.key],
            set_={"value": validated, "value_type": definition.value_type.value, "is_default": False},
        )
        await self.session.execute(statement)
        self.invalidate(tenant_id=tenant_id, namespace=definition.namespace, key=definition.key)
        resolution = SettingResolution(
            namespace=definition.namespace,
            key=definition.key,
            value=validated,
            value_type=definition.value_type,
            source="TENANT",
            tenant_id=tenant_id,
            definition=definition,
        )
        if self.audit is not None:
            self.audit(
                SettingChange(
                    tenant_id=tenant_id,
                    namespace=definition.namespace,
                    key=definition.key,
                    old_value=previous,
                    new_value=validated,
                    actor_id=actor_id,
                    changed_at=changed_at,
                )
            )
        return resolution

    async def delete_setting(self, tenant_id: UUID, namespace: str, key: str) -> bool:
        """Remove a tenant override (falls back to global/seed default); returns True when a row was deleted."""
        definition = self.definition(namespace, key)
        result = await self.session.execute(
            delete(tenant_setting).where(
                tenant_setting.c.tenant_id == tenant_id,
                tenant_setting.c.namespace == definition.namespace,
                tenant_setting.c.key == definition.key,
            )
        )
        self.invalidate(tenant_id=tenant_id, namespace=definition.namespace, key=definition.key)
        return bool(getattr(result, "rowcount", 0))

    def invalidate(
        self,
        *,
        tenant_id: UUID | None = None,
        namespace: str | None = None,
        key: str | None = None,
    ) -> None:
        """Explicit cache invalidation hook (all filters optional)."""
        if tenant_id is None and namespace is None and key is None:
            self._cache.clear()
            return
        target_tenant = str(tenant_id) if tenant_id is not None else None
        for cache_key in list(self._cache):
            cached_tenant, cached_namespace, cached_key = cache_key
            if target_tenant is not None and cached_tenant != target_tenant:
                continue
            if namespace is not None and cached_namespace != namespace:
                continue
            if key is not None and cached_key != key:
                continue
            del self._cache[cache_key]


__all__ = [
    "ALLOWED_NAMESPACES",
    "SEED_SETTINGS_PATH",
    "InvalidNamespace",
    "InvalidSettingValue",
    "SettingChange",
    "SettingDefinition",
    "SettingResolution",
    "SettingType",
    "SettingsError",
    "SettingsService",
    "UnknownSetting",
    "declared_settings",
    "utc_now",
]
