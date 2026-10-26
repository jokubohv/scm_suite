"""FS-03 traceability registry use-case service: topology lifecycle, status semantics, bootstrap.

Validation is deliberately explicit (stable codes from FS-03 §5/§6.1); writes audit + publish
`traceability.*` events through the transactional outbox. Stock-coupled guards are cross-FS: FS-04 installs
the balance guard triggers/tables, and this service degrades to warnings unless
`inventory.storage.strict_inactivation` is enabled.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.modules.traceability import tables as t
from app.modules.traceability.errors import (
    BinsInUse,
    BinsNotEnabled,
    ConflictError,
    DefaultStatusConflict,
    DomainDisabled,
    HierarchyCycle,
    HierarchyDepthExceeded,
    InactiveReference,
    InvalidHierarchy,
    InvalidStateTransition,
    LocationHasStock,
    LocationRequired,
    NotFoundError,
    StatusesNotEnabled,
    StatusInUse,
    StockGuardUnavailable,
    SystemStatusImmutable,
    TraceabilityError,
    UnallocatedImmutable,
    ValidationError,
    ZoneLocationMismatch,
)
from app.modules.traceability.repository import TraceabilityRepository
from app.modules.traceability.resolver import LocationResolver

__all__ = [
    "BIN_TYPES",
    "LOCATION_TYPES",
    "TraceabilityService",
    "ZONE_TYPES",
    "normalise_code",
    "run_bootstrap_sentinel",
    "run_deferred_fk_close",
    "validate_bin_payload",
    "validate_location_payload",
    "validate_status_flags",
]

LOCATION_TYPES = frozenset({"WAREHOUSE", "STORE", "RECEIVING", "TRANSIT", "CONSUMPTION", "UNALLOCATED"})
ZONE_TYPES = frozenset({"STORAGE", "RECEIVING", "STAGING", "PICKING", "SHIPPING"})
BIN_TYPES = frozenset({"STORAGE", "PICKING", "STAGING", "RECEIVING", "DOCK"})
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.\-]{0,63}$")
MAX_LOCATION_DEPTH = 10
DOMAIN_FLAG = "traceability.registry.enabled"
LEGACY_DOMAIN_FLAGS = ("traceability.registry.enabled", "inventory.storage_registry.enabled")

SETTING_BINS = ("inventory", "bins.enabled")
SETTING_STATUSES = ("inventory", "statuses.enabled")
SETTING_STRICT_INACTIVATION = ("inventory", "strict_inactivation")
SETTING_COMMIT_NON_AVAILABLE = ("inventory", "commit_non_available")


def _now() -> datetime:
    return datetime.now(UTC)


def normalise_code(value: Any, *, field: str = "code") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required", details={"field": field})
    code = value.strip().upper()
    if not CODE_RE.match(code):
        raise ValidationError(
            f"{field} must match [A-Z0-9][A-Z0-9_.-]{{0,63}}",
            details={"field": field, "value": code},
        )
    return code


def _text(value: Any, *, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required", details={"field": field})
    text = value.strip()
    if len(text) > max_length:
        raise ValidationError(f"{field} is too long", details={"field": field, "max_length": max_length})
    return text


def _optional_text(value: Any, *, field: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, max_length=max_length)


def _bool(value: Any, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be a boolean", details={"field": field})
    return value


def _timezone(value: Any) -> str:
    if value is None:
        return "UTC"
    if not isinstance(value, str):
        raise ValidationError("timezone must be an IANA timezone", details={"field": "timezone"})
    try:
        ZoneInfo(value)
    except (ValueError, KeyError) as exc:
        raise ValidationError("invalid IANA timezone", details={"field": "timezone", "value": value}) from exc
    return value


def _address(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("address must be an object", details={"field": "address"})
    return dict(value)


def _uuid(value: Any, *, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid UUID", details={"field": field, "value": str(value)}) from exc


def _map_unique_violation(exc: Exception, mapping: Mapping[str, str]) -> Exception:
    text = str(exc)
    for constraint, message in mapping.items():
        if constraint in text:
            from app.modules.traceability.errors import ConflictError

            return ConflictError(message, details={"constraint": constraint})
    return exc


def validate_location_payload(values: Mapping[str, Any], *, update: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not update:
        payload["code"] = normalise_code(values.get("code"))
        payload["name"] = _text(values.get("name"), field="name")
        location_type = str(values.get("location_type") or "").strip().upper()
        if location_type not in LOCATION_TYPES:
            raise ValidationError(
                "invalid location_type", details={"field": "location_type", "allowed": sorted(LOCATION_TYPES)}
            )
        payload["location_type"] = location_type
        if bool(values.get("is_unallocated")) or location_type == "UNALLOCATED":
            raise ValidationError("UNALLOCATED sentinel locations are provisioned by bootstrap only")
        payload["parent_id"] = _uuid(values["parent_id"], field="parent_id") if values.get("parent_id") else None
        payload["address"] = _address(values.get("address"))
        payload["subsidiary_ref"] = _optional_text(values.get("subsidiary_ref"), field="subsidiary_ref")
        payload["timezone"] = _timezone(values.get("timezone"))
        payload["is_bins_enabled"] = _bool(values.get("is_bins_enabled"), field="is_bins_enabled")
        payload["is_statuses_enabled"] = _bool(values.get("is_statuses_enabled"), field="is_statuses_enabled")
        payload["is_consignment_allowed"] = _bool(values.get("is_consignment_allowed"), field="is_consignment_allowed")
        payload["allow_commit_from_non_available"] = _bool(
            values.get("allow_commit_from_non_available"), field="allow_commit_from_non_available"
        )
        if payload["is_consignment_allowed"] and not payload["is_statuses_enabled"]:
            raise ValidationError(
                "consignment requires is_statuses_enabled=true (consigned status separation is mandatory)",
                details={"field": "is_consignment_allowed"},
            )
    else:
        for key in ("name",):
            if key in values:
                payload[key] = _text(values[key], field=key)
        if "address" in values:
            payload["address"] = _address(values.get("address"))
        if "subsidiary_ref" in values:
            payload["subsidiary_ref"] = _optional_text(values.get("subsidiary_ref"), field="subsidiary_ref")
        if "timezone" in values:
            payload["timezone"] = _timezone(values.get("timezone"))
        for key in (
            "is_bins_enabled",
            "is_statuses_enabled",
            "is_consignment_allowed",
            "allow_commit_from_non_available",
        ):
            if key in values:
                payload[key] = _bool(values[key], field=key)
        if "parent_id" in values:
            payload["parent_id"] = _uuid(values["parent_id"], field="parent_id") if values.get("parent_id") else None
        for immutable in ("code", "location_type", "is_unallocated"):
            if immutable in values:
                raise ValidationError(f"{immutable} is immutable", details={"field": immutable})
        if payload.get("is_consignment_allowed") and not payload.get("is_statuses_enabled"):
            raise ValidationError(
                "consignment requires is_statuses_enabled=true",
                details={"field": "is_consignment_allowed"},
            )
    return payload


def validate_bin_payload(values: Mapping[str, Any], *, update: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not update:
        payload["location_id"] = _uuid(values.get("location_id"), field="location_id")
        payload["code"] = normalise_code(values.get("code"))
        payload["name"] = _text(values.get("name"), field="name")
        bin_type = str(values.get("bin_type") or "STORAGE").strip().upper()
        if bin_type not in BIN_TYPES:
            raise ValidationError("invalid bin_type", details={"field": "bin_type", "allowed": sorted(BIN_TYPES)})
        payload["bin_type"] = bin_type
        payload["zone_id"] = _uuid(values["zone_id"], field="zone_id") if values.get("zone_id") else None
        payload["is_pickable"] = _bool(values.get("is_pickable"), field="is_pickable")
        payload["capacity_qty"] = None
        if values.get("capacity_qty") is not None:
            try:
                from decimal import Decimal

                capacity = Decimal(str(values["capacity_qty"]))
            except Exception as exc:  # noqa: BLE001
                raise ValidationError("capacity_qty must be a decimal", details={"field": "capacity_qty"}) from exc
            if capacity <= 0:
                raise ValidationError("capacity_qty must be > 0", details={"field": "capacity_qty"})
            payload["capacity_qty"] = capacity
        payload["capacity_uom_id"] = (
            _uuid(values["capacity_uom_id"], field="capacity_uom_id") if values.get("capacity_uom_id") else None
        )
        payload["coordinates"] = _address(values.get("coordinates"))
    else:
        for key in ("name",):
            if key in values:
                payload[key] = _text(values[key], field=key)
        if "bin_type" in values:
            bin_type = str(values["bin_type"]).strip().upper()
            if bin_type not in BIN_TYPES:
                raise ValidationError("invalid bin_type", details={"field": "bin_type", "allowed": sorted(BIN_TYPES)})
            payload["bin_type"] = bin_type
        if "zone_id" in values:
            payload["zone_id"] = _uuid(values["zone_id"], field="zone_id") if values.get("zone_id") else None
        if "is_pickable" in values:
            payload["is_pickable"] = _bool(values["is_pickable"], field="is_pickable")
        if "coordinates" in values:
            payload["coordinates"] = _address(values.get("coordinates"))
        for immutable in ("location_id", "code"):
            if immutable in values:
                raise ValidationError(f"{immutable} is immutable", details={"field": immutable})
    return payload


def validate_status_flags(
    *,
    is_available_for_allocation: bool,
    is_committable: bool,
    is_sellable: bool,
    is_consigned: bool,
    is_default: bool,
    commit_non_available: bool,
) -> None:
    if is_consigned and is_default:
        raise DefaultStatusConflict("a consigned status cannot be the default status")
    if is_committable and not is_available_for_allocation and not commit_non_available:
        raise ValidationError(
            "committable statuses must be available for allocation unless commit_non_available is enabled",
            details={"code": "STATUS_FLAGS_INVALID", "field": "is_committable"},
        )
    if is_committable and is_consigned and not is_available_for_allocation:
        raise ValidationError("consigned committable status must be available for allocation")


class TraceabilityService:
    def __init__(self, session: AsyncSession, *, resolver: LocationResolver | None = None) -> None:
        self.session = session
        self.repo = TraceabilityRepository(session)
        self.resolver = resolver or LocationResolver(session)

    # ---------------------------------------------------------------- settings / gate -------
    async def tenant_setting(self, tenant_id: str | UUID, namespace: str, key: str, default: Any = None) -> Any:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        result = await self.session.execute(
            select(table.c.value).where(
                table.c.tenant_id == _uuid(tenant_id, field="tenant_id"),
                table.c.namespace == namespace,
                table.c.key == key,
            )
        )
        row = result.scalar_one_or_none()
        return default if row is None else row

    async def domain_enabled(self, tenant_id: str | UUID) -> bool:
        table = platform_models.metadata.tables["platform.feature_flag"]
        result = await self.session.execute(
            select(table.c.is_enabled).where(
                table.c.tenant_id == _uuid(tenant_id, field="tenant_id"),
                table.c.flag_key.in_(LEGACY_DOMAIN_FLAGS),
            )
        )
        return any(bool(value) for value in result.scalars().all())

    async def bins_enabled_for_tenant(self, tenant_id: str | UUID) -> bool:
        return bool(await self.tenant_setting(tenant_id, *SETTING_BINS, default=True))

    async def statuses_enabled_for_tenant(self, tenant_id: str | UUID) -> bool:
        return bool(await self.tenant_setting(tenant_id, *SETTING_STATUSES, default=True))

    async def strict_inactivation(self, tenant_id: str | UUID) -> bool:
        return bool(await self.tenant_setting(tenant_id, *SETTING_STRICT_INACTIVATION, default=False))

    async def commit_non_available(self, tenant_id: str | UUID) -> bool:
        return bool(await self.tenant_setting(tenant_id, *SETTING_COMMIT_NON_AVAILABLE, default=False))

    # ---------------------------------------------------------------- bootstrap --------------
    async def ensure_bootstrap(self, tenant_id: str | UUID) -> dict[str, Any]:
        if not await self.domain_enabled(tenant_id):
            raise DomainDisabled("traceability registry is disabled for this tenant")
        sentinel = await self.repo.ensure_bootstrap(tenant_id)
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=None,
            action="traceability.bootstrap.sentinel",
            resource_type="traceability.location",
            resource_id=sentinel["id"],
            diff={"seeded": True},
        )
        return sentinel

    async def bootstrap_state(self, tenant_id: str | UUID) -> dict[str, Any]:
        if not await self.domain_enabled(tenant_id):
            raise DomainDisabled("traceability registry is disabled for this tenant")
        state = await self.resolver.bootstrap_state(tenant_id)
        state["domain_enabled"] = True
        state["bins_enabled"] = await self.bins_enabled_for_tenant(tenant_id)
        state["statuses_enabled"] = await self.statuses_enabled_for_tenant(tenant_id)
        state["strict_inactivation"] = await self.strict_inactivation(tenant_id)
        sentinel = await self.repo.get_sentinel(tenant_id)
        if sentinel is not None:
            state["sentinel"] = sentinel
        return state

    # ---------------------------------------------------------------- locations --------------
    async def list_locations(self, tenant_id: str | UUID, **filters: Any) -> list[dict[str, Any]]:
        return await self.repo.list_locations(tenant_id, **filters)

    async def get_location(self, tenant_id: str | UUID, location_id: str | UUID) -> dict[str, Any]:
        location = await self.repo.get_location(tenant_id, location_id)
        if location is None:
            raise NotFoundError("location not found", details={"location_id": str(location_id)})
        return location

    async def _validate_parent(
        self, tenant_id: str | UUID, *, parent_id: UUID | None, location_id: UUID | None = None
    ) -> None:
        if parent_id is None:
            return
        parent = await self.repo.get_location(tenant_id, parent_id)
        if parent is None:
            raise ValidationError("parent location not found", details={"field": "parent_id"})
        if parent["is_unallocated"]:
            raise InvalidHierarchy("UNALLOCATED sentinel cannot have children")
        if location_id is not None and str(parent_id) == str(location_id):
            raise HierarchyCycle("location cannot be its own parent", details={"location_id": str(location_id)})
        # depth from parent's ancestry must stay below MAX_LOCATION_DEPTH
        depth = 1
        cursor: dict[str, Any] = parent
        while cursor.get("parent_id") is not None and depth < MAX_LOCATION_DEPTH + 2:
            next_cursor = await self.repo.get_location(tenant_id, cursor["parent_id"])
            if next_cursor is None:
                break
            cursor = next_cursor
            depth += 1
        if depth >= MAX_LOCATION_DEPTH:
            raise HierarchyDepthExceeded(
                "location hierarchy depth limit exceeded", details={"max_depth": MAX_LOCATION_DEPTH}
            )
        if location_id is not None:
            descendants = await self.repo.descendants(tenant_id, location_id, max_depth=MAX_LOCATION_DEPTH)
            if any(str(row["id"]) == str(parent_id) for row in descendants):
                raise HierarchyCycle("location hierarchy cycle detected", details={"parent_id": str(parent_id)})

    async def create_location(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.ensure_bootstrap(tenant_id)
        payload = validate_location_payload(values)
        await self._validate_parent(tenant_id, parent_id=payload.get("parent_id"))
        if payload["is_bins_enabled"] and not await self.bins_enabled_for_tenant(tenant_id):
            raise BinsNotEnabled("bins are globally disabled for this tenant")
        if payload["is_statuses_enabled"] and not await self.statuses_enabled_for_tenant(tenant_id):
            raise StatusesNotEnabled("inventory statuses are globally disabled for this tenant")
        if payload["location_type"] == "UNALLOCATED":
            raise ValidationError("UNALLOCATED locations are provisioned by bootstrap only")
        if await self.repo.get_location_by_code(tenant_id, payload["code"]) is not None:
            raise ConflictError("location code already exists", details={"code": payload["code"]})
        try:
            row = await self.repo.insert_row(t.location, tenant_id, payload, actor_id)
        except Exception as exc:  # noqa: BLE001
            if "uq_location_code" in str(exc):
                raise ConflictError("location code already exists", details={"code": payload["code"]}) from exc
            raise
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.location.created",
            resource_type="traceability.location",
            resource_id=row["id"],
            diff={"code": row["code"]},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.location.created",
            aggregate_type="traceability.location",
            aggregate_id=row["id"],
            payload={"location_id": row["id"], "code": row["code"], "location_type": row["location_type"]},
        )
        return row

    async def update_location(
        self,
        *,
        tenant_id: str,
        location_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = await self.get_location(tenant_id, location_id)
        payload = validate_location_payload(values, update=True)
        if current["is_unallocated"] and payload:
            raise UnallocatedImmutable("UNALLOCATED sentinel identity flags are immutable")
        if "parent_id" in payload:
            await self._validate_parent(
                tenant_id, parent_id=payload["parent_id"], location_id=_uuid(location_id, field="location_id")
            )
        if payload.get("is_bins_enabled") and not await self.bins_enabled_for_tenant(tenant_id):
            raise BinsNotEnabled("bins are globally disabled for this tenant")
        if payload.get("is_statuses_enabled") and not await self.statuses_enabled_for_tenant(tenant_id):
            raise StatusesNotEnabled("inventory statuses are globally disabled for this tenant")
        if (
            "is_consignment_allowed" in payload
            and payload["is_consignment_allowed"]
            and not (payload.get("is_statuses_enabled", current["is_statuses_enabled"]))
        ):
            raise ValidationError("consignment requires statuses enabled", details={"field": "is_consignment_allowed"})
        try:
            row = await self.repo.update_row(
                t.location, tenant_id, location_id, payload, actor_id, expected_version=expected_version
            )
        except TraceabilityError:
            raise
        except Exception as exc:  # noqa: BLE001
            if "trg_location_hierarchy_cycle" in str(exc) or "cycle" in str(exc).lower():
                raise HierarchyCycle("location hierarchy cycle detected") from exc
            raise
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.location.updated",
            resource_type="traceability.location",
            resource_id=row["id"],
            diff=payload,
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.location.updated",
            aggregate_type="traceability.location",
            aggregate_id=row["id"],
            payload={"location_id": row["id"], "changed": sorted(payload)},
        )
        return row

    async def activate_location(
        self, *, tenant_id: str, location_id: str | UUID, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_location(tenant_id, location_id)
        if not current["is_inactive"]:
            raise InvalidStateTransition("location is already active")
        if current["parent_id"] is not None:
            parent = await self.repo.get_location(tenant_id, current["parent_id"])
            if parent is not None and parent["is_inactive"]:
                raise InactiveReference("parent location is inactive", details={"parent_id": parent["id"]})
        row = await self.repo.update_row(
            t.location,
            tenant_id,
            location_id,
            {"is_inactive": False, "inactive_at": None, "inactive_reason": None},
            actor_id,
        )
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.location.activated",
            resource_type="traceability.location",
            resource_id=row["id"],
            diff={},
        )
        return row

    async def inactivate_location(
        self, *, tenant_id: str, location_id: str | UUID, reason: str | None, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_location(tenant_id, location_id)
        if current["is_unallocated"]:
            raise UnallocatedImmutable("UNALLOCATED sentinel cannot be inactivated")
        if current["is_inactive"]:
            raise InvalidStateTransition("location is already inactive")
        if await self.repo.active_children_count(tenant_id, location_id) > 0:
            raise InvalidHierarchy("location has active child locations", details={"code": "LOCATION_HAS_CHILDREN"})
        active_bins = await self.repo.active_bins_count(tenant_id, location_id)
        if current["is_bins_enabled"] and active_bins > 0:
            raise BinsInUse(
                "location has active bins; inactivate them first",
                details={"code": "BINS_IN_USE", "active_bins": active_bins},
            )
        warnings: list[str] = []
        stock_rows = await self.repo.location_balance_count(tenant_id, location_id)
        if stock_rows > 0:
            raise LocationHasStock("location has non-zero inventory balances", details={"rows": stock_rows})
        if not await self.repo.stock_guard_installed():
            warnings.append("BALANCE_GUARD_DEFERRED")
            if await self.strict_inactivation(tenant_id):
                raise StockGuardUnavailable(
                    "strict inactivation requested but FS-04 stock guard is not installed",
                    details={"warnings": warnings},
                )
        row = await self.repo.update_row(
            t.location,
            tenant_id,
            location_id,
            {"is_inactive": True, "inactive_at": _now(), "inactive_reason": (reason or "").strip() or None},
            actor_id,
        )
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.location.inactivated",
            resource_type="traceability.location",
            resource_id=row["id"],
            diff={"reason": reason, "warnings": warnings},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.location.inactivated",
            aggregate_type="traceability.location",
            aggregate_id=row["id"],
            payload={"location_id": row["id"], "warnings": warnings},
        )
        row["warnings"] = warnings
        return row

    async def children(
        self, tenant_id: str | UUID, location_id: str | UUID, *, active_only: bool = False
    ) -> list[dict[str, Any]]:
        await self.get_location(tenant_id, location_id)
        return await self.repo.children(tenant_id, location_id, active_only=active_only)

    async def descendants(self, tenant_id: str | UUID, location_id: str | UUID) -> list[dict[str, Any]]:
        await self.get_location(tenant_id, location_id)
        return await self.repo.descendants(tenant_id, location_id, max_depth=MAX_LOCATION_DEPTH)

    async def location_bins(
        self, tenant_id: str | UUID, location_id: str | UUID, **filters: Any
    ) -> list[dict[str, Any]]:
        await self.get_location(tenant_id, location_id)
        return await self.repo.list_bins(tenant_id, location_id=location_id, **filters)

    # ---------------------------------------------------------------- zones ------------------
    async def list_zones(self, tenant_id: str | UUID, **filters: Any) -> list[dict[str, Any]]:
        return await self.repo.list_zones(tenant_id, **filters)

    async def get_zone(self, tenant_id: str | UUID, zone_id: str | UUID) -> dict[str, Any]:
        zone = await self.repo.get_row(t.zone, tenant_id, zone_id)
        if zone is None:
            raise NotFoundError("zone not found", details={"zone_id": str(zone_id)})
        return zone

    async def create_zone(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        location = await self.get_location(tenant_id, _uuid(values.get("location_id"), field="location_id"))
        if location["is_inactive"]:
            raise InactiveReference("cannot create a zone in an inactive location")
        code = normalise_code(values.get("code"))
        zone_type = str(values.get("zone_type") or "").strip().upper()
        if zone_type not in ZONE_TYPES:
            raise ValidationError("invalid zone_type", details={"field": "zone_type", "allowed": sorted(ZONE_TYPES)})
        priority = int(values.get("priority", 0))
        if priority < 0:
            raise ValidationError("priority must be >= 0", details={"field": "priority"})
        payload = {
            "location_id": location["id"],
            "code": code,
            "name": _text(values.get("name"), field="name"),
            "zone_type": zone_type,
            "priority": priority,
            "is_active": _bool(values.get("is_active"), field="is_active", default=True),
        }
        try:
            row = await self.repo.insert_row(t.zone, tenant_id, payload, actor_id)
        except Exception as exc:  # noqa: BLE001
            raise _map_unique_violation(exc, {"uq_zone_code": "zone code already exists at this location"}) from exc
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.zone.created",
            resource_type="traceability.zone",
            resource_id=row["id"],
            diff={"code": code},
        )
        return row

    async def update_zone(
        self,
        *,
        tenant_id: str,
        zone_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        await self.get_zone(tenant_id, zone_id)
        payload: dict[str, Any] = {}
        for key in ("name",):
            if key in values:
                payload[key] = _text(values[key], field=key)
        if "zone_type" in values:
            zone_type = str(values["zone_type"]).strip().upper()
            if zone_type not in ZONE_TYPES:
                raise ValidationError("invalid zone_type", details={"field": "zone_type"})
            payload["zone_type"] = zone_type
        if "priority" in values:
            priority = int(values["priority"])
            if priority < 0:
                raise ValidationError("priority must be >= 0", details={"field": "priority"})
            payload["priority"] = priority
        if "is_active" in values:
            payload["is_active"] = _bool(values["is_active"], field="is_active")
        for immutable in ("location_id", "code"):
            if immutable in values:
                raise ValidationError(f"{immutable} is immutable", details={"field": immutable})
        row = await self.repo.update_row(
            t.zone, tenant_id, zone_id, payload, actor_id, expected_version=expected_version
        )
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.zone.updated",
            resource_type="traceability.zone",
            resource_id=row["id"],
            diff=payload,
        )
        return row

    async def inactivate_zone(
        self, *, tenant_id: str, zone_id: str | UUID, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_zone(tenant_id, zone_id)
        if not current["is_active"]:
            raise InvalidStateTransition("zone is already inactive")
        bins = await self.repo.list_bins(tenant_id, zone_id=_uuid(zone_id, field="zone_id"), is_active=True, limit=1)
        if bins:
            raise ConflictError("zone has active bins", details={"code": "ZONE_IN_USE"})
        row = await self.repo.update_row(t.zone, tenant_id, zone_id, {"is_active": False}, actor_id)
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.zone.inactivated",
            resource_type="traceability.zone",
            resource_id=row["id"],
            diff={},
        )
        return row

    # ---------------------------------------------------------------- bins -------------------
    async def list_bins(self, tenant_id: str | UUID, **filters: Any) -> list[dict[str, Any]]:
        return await self.repo.list_bins(tenant_id, **filters)

    async def get_bin(self, tenant_id: str | UUID, bin_id: str | UUID) -> dict[str, Any]:
        bin_row = await self.repo.get_row(t.bin, tenant_id, bin_id)
        if bin_row is None:
            raise NotFoundError("bin not found", details={"bin_id": str(bin_id)})
        return bin_row

    async def _assert_bins_allowed(self, tenant_id: str, location: Mapping[str, Any]) -> None:
        if not await self.bins_enabled_for_tenant(tenant_id):
            raise BinsNotEnabled("bins are globally disabled for this tenant")
        if not location["is_bins_enabled"]:
            raise BinsNotEnabled("bins are not enabled on this location", details={"location_id": location["id"]})
        if location["is_inactive"]:
            raise InactiveReference("location is inactive")

    async def create_bin(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        payload = validate_bin_payload(values)
        location = await self.get_location(tenant_id, payload["location_id"])
        await self._assert_bins_allowed(tenant_id, location)
        zone_id = payload.get("zone_id")
        if zone_id is not None:
            zone = await self.get_zone(tenant_id, zone_id)
            if str(zone["location_id"]) != str(location["id"]):
                raise ZoneLocationMismatch("bin zone belongs to a different location")
            if not zone["is_active"]:
                raise InactiveReference("zone is inactive")
        if payload.get("capacity_uom_id") is not None and not await self.repo.uom_exists(
            tenant_id, payload["capacity_uom_id"]
        ):
            raise ValidationError("capacity_uom_id not found", details={"field": "capacity_uom_id"})
        try:
            row = await self.repo.insert_row(t.bin, tenant_id, payload, actor_id)
        except Exception as exc:  # noqa: BLE001
            raise _map_unique_violation(exc, {"uq_bin_code": "bin code already exists at this location"}) from exc
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.bin.created",
            resource_type="traceability.bin",
            resource_id=row["id"],
            diff={"code": row["code"], "location_id": location["id"]},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.bin.changed",
            aggregate_type="traceability.bin",
            aggregate_id=row["id"],
            payload={"bin_id": row["id"], "change": "CREATED", "location_id": location["id"]},
        )
        return row

    async def update_bin(
        self,
        *,
        tenant_id: str,
        bin_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = await self.get_bin(tenant_id, bin_id)
        location = await self.get_location(tenant_id, current["location_id"])
        payload = validate_bin_payload(values, update=True)
        if "zone_id" in payload and payload["zone_id"] is not None:
            zone = await self.get_zone(tenant_id, payload["zone_id"])
            if str(zone["location_id"]) != str(current["location_id"]):
                raise ZoneLocationMismatch("bin zone belongs to a different location")
        row = await self.repo.update_row(t.bin, tenant_id, bin_id, payload, actor_id, expected_version=expected_version)
        del location
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.bin.updated",
            resource_type="traceability.bin",
            resource_id=row["id"],
            diff=payload,
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.bin.changed",
            aggregate_type="traceability.bin",
            aggregate_id=row["id"],
            payload={"bin_id": row["id"], "change": "UPDATED", "changed": sorted(payload)},
        )
        return row

    async def activate_bin(self, *, tenant_id: str, bin_id: str | UUID, actor_id: str | UUID | None) -> dict[str, Any]:
        current = await self.get_bin(tenant_id, bin_id)
        if current["is_active"]:
            raise InvalidStateTransition("bin is already active")
        location = await self.get_location(tenant_id, current["location_id"])
        await self._assert_bins_allowed(tenant_id, location)
        row = await self.repo.update_row(t.bin, tenant_id, bin_id, {"is_active": True}, actor_id)
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.bin.changed",
            aggregate_type="traceability.bin",
            aggregate_id=row["id"],
            payload={"bin_id": row["id"], "change": "ACTIVATED"},
        )
        return row

    async def inactivate_bin(
        self, *, tenant_id: str, bin_id: str | UUID, reason: str | None, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_bin(tenant_id, bin_id)
        if not current["is_active"]:
            raise InvalidStateTransition("bin is already inactive")
        balance_rows = await self.repo.bin_balance_count(tenant_id, bin_id)
        if balance_rows > 0:
            raise BinsInUse("bin has non-zero balances", details={"rows": balance_rows})
        if not await self.repo.stock_guard_installed():
            pass  # warning-only: bin balances live in FS-04
        row = await self.repo.update_row(t.bin, tenant_id, bin_id, {"is_active": False}, actor_id)
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.bin.inactivated",
            resource_type="traceability.bin",
            resource_id=row["id"],
            diff={"reason": reason},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.bin.changed",
            aggregate_type="traceability.bin",
            aggregate_id=row["id"],
            payload={"bin_id": row["id"], "change": "INACTIVATED"},
        )
        return row

    # ---------------------------------------------------------------- statuses ---------------
    async def list_statuses(self, tenant_id: str | UUID, **filters: Any) -> list[dict[str, Any]]:
        return await self.repo.list_statuses(tenant_id, **filters)

    async def get_status(self, tenant_id: str | UUID, status_id: str | UUID) -> dict[str, Any]:
        status = await self.repo.get_row(t.inventory_status, tenant_id, status_id)
        if status is None:
            raise NotFoundError("inventory status not found", details={"status_id": str(status_id)})
        return status

    async def create_status(
        self, *, tenant_id: str, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        await self.ensure_bootstrap(tenant_id)
        code = normalise_code(values.get("code"))
        flags = {
            "is_available_for_allocation": _bool(
                values.get("is_available_for_allocation"), field="is_available_for_allocation", default=True
            ),
            "is_committable": _bool(values.get("is_committable"), field="is_committable", default=True),
            "is_sellable": _bool(values.get("is_sellable"), field="is_sellable", default=True),
            "is_consigned": _bool(values.get("is_consigned"), field="is_consigned"),
            "is_default": _bool(values.get("is_default"), field="is_default"),
            "is_active": _bool(values.get("is_active"), field="is_active", default=True),
        }
        validate_status_flags(
            is_available_for_allocation=flags["is_available_for_allocation"],
            is_committable=flags["is_committable"],
            is_sellable=flags["is_sellable"],
            is_consigned=flags["is_consigned"],
            is_default=flags["is_default"],
            commit_non_available=await self.commit_non_available(tenant_id),
        )
        if await self.repo.get_status_by_code(tenant_id, code) is not None:
            raise ConflictError("status code already exists", details={"code": code})
        if flags["is_default"]:
            await self.repo.clear_default_status(tenant_id)
        try:
            row = await self.repo.insert_row(
                t.inventory_status,
                tenant_id,
                {"code": code, "name": _text(values.get("name"), field="name"), **flags, "is_system": False},
                actor_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_unique_violation(
                exc,
                {
                    "uq_status_code": "status code already exists",
                    "uq_status_default": "a default status already exists",
                },
            ) from exc
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.status.created",
            resource_type="traceability.inventory_status",
            resource_id=row["id"],
            diff=flags,
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.status.changed",
            aggregate_type="traceability.inventory_status",
            aggregate_id=row["id"],
            payload={"status_id": row["id"], "code": code, "change": "CREATED"},
        )
        return row

    async def update_status(
        self,
        *,
        tenant_id: str,
        status_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = await self.get_status(tenant_id, status_id)
        payload: dict[str, Any] = {}
        if "name" in values:
            payload["name"] = _text(values["name"], field="name")
        flags = {
            key: _bool(values[key], field=key)
            for key in ("is_available_for_allocation", "is_committable", "is_sellable", "is_consigned", "is_default")
            if key in values
        }
        if current["is_system"] and set(flags) - {"is_sellable"}:
            raise SystemStatusImmutable(
                "system status flags are immutable except is_sellable", details={"status_id": str(status_id)}
            )
        for immutable in ("code", "is_system", "is_active"):
            if immutable in values:
                raise ValidationError(
                    f"{immutable} is immutable; use lifecycle endpoints", details={"field": immutable}
                )
        merged = {
            "is_available_for_allocation": flags.get(
                "is_available_for_allocation", current["is_available_for_allocation"]
            ),
            "is_committable": flags.get("is_committable", current["is_committable"]),
            "is_sellable": flags.get("is_sellable", current["is_sellable"]),
            "is_consigned": flags.get("is_consigned", current["is_consigned"]),
            "is_default": flags.get("is_default", current["is_default"]),
        }
        validate_status_flags(
            is_available_for_allocation=merged["is_available_for_allocation"],
            is_committable=merged["is_committable"],
            is_sellable=merged["is_sellable"],
            is_consigned=merged["is_consigned"],
            is_default=merged["is_default"],
            commit_non_available=await self.commit_non_available(tenant_id),
        )
        if current["is_default"] and merged["is_committable"] is False and current["is_default"]:
            pass  # default may be non-committable (e.g. HOLD); no rule
        balance_rows = await self.repo.status_balance_count(tenant_id, status_id)
        if balance_rows > 0 and (
            merged["is_available_for_allocation"] != current["is_available_for_allocation"]
            or merged["is_committable"] != current["is_committable"]
            or merged["is_consigned"] != current["is_consigned"]
        ):
            raise StatusInUse("status flag change blocked by non-zero balances", details={"rows": balance_rows})
        payload.update(flags)
        if flags.get("is_default") is True and not current["is_default"]:
            await self.repo.clear_default_status(tenant_id, except_id=_uuid(status_id, field="status_id"))
        row = await self.repo.update_row(
            t.inventory_status, tenant_id, status_id, payload, actor_id, expected_version=expected_version
        )
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.status.updated",
            resource_type="traceability.inventory_status",
            resource_id=row["id"],
            diff=payload,
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.status.changed",
            aggregate_type="traceability.inventory_status",
            aggregate_id=row["id"],
            payload={"status_id": row["id"], "code": row["code"], "change": "UPDATED"},
        )
        return row

    async def activate_status(
        self, *, tenant_id: str, status_id: str | UUID, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_status(tenant_id, status_id)
        if current["is_active"]:
            raise InvalidStateTransition("status is already active")
        row = await self.repo.update_row(t.inventory_status, tenant_id, status_id, {"is_active": True}, actor_id)
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.status.changed",
            aggregate_type="traceability.inventory_status",
            aggregate_id=row["id"],
            payload={"status_id": row["id"], "change": "ACTIVATED"},
        )
        return row

    async def inactivate_status(
        self, *, tenant_id: str, status_id: str | UUID, reason: str | None, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_status(tenant_id, status_id)
        if current["is_system"]:
            raise SystemStatusImmutable("system statuses cannot be inactivated", details={"code": current["code"]})
        if current["is_default"]:
            raise InvalidStateTransition(
                "the default status cannot be inactivated; set another default first",
                details={"code": "DEFAULT_STATUS_IMMUTABLE"},
            )
        if not current["is_active"]:
            raise InvalidStateTransition("status is already inactive")
        balance_rows = await self.repo.status_balance_count(tenant_id, status_id)
        if balance_rows > 0:
            raise StatusInUse("status has non-zero balances", details={"rows": balance_rows})
        row = await self.repo.update_row(t.inventory_status, tenant_id, status_id, {"is_active": False}, actor_id)
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.status.inactivated",
            resource_type="traceability.inventory_status",
            resource_id=row["id"],
            diff={"reason": reason},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.status.changed",
            aggregate_type="traceability.inventory_status",
            aggregate_id=row["id"],
            payload={"status_id": row["id"], "change": "INACTIVATED"},
        )
        return row

    async def set_default_status(
        self, *, tenant_id: str, status_id: str | UUID, actor_id: str | UUID | None
    ) -> dict[str, Any]:
        current = await self.get_status(tenant_id, status_id)
        if not current["is_active"]:
            raise InactiveReference("cannot set an inactive status as default")
        if current["is_consigned"]:
            raise DefaultStatusConflict("consigned statuses cannot be the default status")
        await self.repo.clear_default_status(tenant_id, except_id=_uuid(status_id, field="status_id"))
        row = await self.repo.update_row(t.inventory_status, tenant_id, status_id, {"is_default": True}, actor_id)
        await self.repo.audit(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="traceability.status.default_set",
            resource_type="traceability.inventory_status",
            resource_id=row["id"],
            diff={"code": row["code"]},
        )
        await self.repo.emit(
            tenant_id=tenant_id,
            event_type="traceability.status.changed",
            aggregate_type="traceability.inventory_status",
            aggregate_id=row["id"],
            payload={"status_id": row["id"], "code": row["code"], "change": "DEFAULT_SET"},
        )
        return row

    # ---------------------------------------------------------------- eligibility -----------
    async def posting_eligibility(
        self,
        tenant_id: str | UUID,
        *,
        item_id: str,
        bin_id: str | None = None,
        inventory_status_id: str | None = None,
        ownership: str = "OWNED",
        qty_class: str = "ON_HAND",
    ) -> dict[str, Any]:
        if not await self.domain_enabled(tenant_id):
            raise DomainDisabled("traceability registry is disabled for this tenant")
        await self.repo.ensure_bootstrap(tenant_id)
        location_id = bin_id  # resolved below when bin provided
        del location_id
        if bin_id is not None:
            bin_row = await self.repo.get_row(t.bin, tenant_id, bin_id)
            if bin_row is None:
                raise NotFoundError("bin not found", details={"bin_id": bin_id})
            location_id = bin_row["location_id"]
        else:
            item_location = await self.repo.item_location_default(tenant_id, item_id)
            if item_location is None:
                raise LocationRequired(
                    "bin_id is required when the item has no single default location",
                    details={"field": "bin_id"},
                )
            location_id = item_location["location_id"]
        validation = await self.resolver.validate_posting_position(
            tenant_id,
            item_id=_uuid(item_id, field="item_id"),
            location_id=_uuid(location_id, field="location_id"),
            bin_id=_uuid(bin_id, field="bin_id") if bin_id else None,
            inventory_status_id=_uuid(inventory_status_id, field="inventory_status_id")
            if inventory_status_id
            else None,
            ownership=ownership,
            qty_class=qty_class,
        )
        return {
            "item_id": item_id,
            "bin_id": bin_id,
            "location_id": str(location_id),
            "eligible": bool(validation["ok"]),
            "normalized": validation["normalized"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
            "bins_enabled": await self.bins_enabled_for_tenant(tenant_id),
            "statuses_enabled": await self.statuses_enabled_for_tenant(tenant_id),
        }

    # ---------------------------------------------------------------- jobs ------------------
    async def deferred_fk_state(self, tenant_id: str | UUID) -> dict[str, Any]:
        orphans = await self.repo.deferred_fk_orphans(tenant_id)
        return {"tenant_id": str(tenant_id), "orphans": orphans, "ok": all(count == 0 for count in orphans.values())}


# --------------------------------------------------------------------------- jobs ------------
async def run_bootstrap_sentinel(session: AsyncSession, tenant_id: str | UUID) -> dict[str, Any]:
    """Job handler: re-runnable per-tenant sentinel/status bootstrap (idempotent, flag-independent)."""
    service = TraceabilityService(session)
    sentinel = await service.repo.ensure_bootstrap(tenant_id)
    state = await service.bootstrap_state(tenant_id)
    await service.repo.emit(
        tenant_id=tenant_id,
        event_type="traceability.bootstrap.completed",
        aggregate_type="traceability.location",
        aggregate_id=sentinel["id"],
        payload={"seeded": state["seeded"], "status_count": state["status_count"]},
    )
    return {
        "sentinel_location_id": sentinel["id"],
        **{k: state[k] for k in ("sentinel_present", "status_count", "default_status_code", "seeded")},
    }


async def run_deferred_fk_close(session: AsyncSession, tenant_id: str | UUID) -> dict[str, Any]:
    """Job handler: verify FS-01 deferred references (DMX-19); migration owns constraint creation."""
    service = TraceabilityService(session)
    state = await service.deferred_fk_state(tenant_id)
    await service.repo.emit(
        tenant_id=tenant_id,
        event_type="traceability.deferred_fk.checked",
        aggregate_type="traceability.registry",
        aggregate_id=uuid.uuid5(uuid.NAMESPACE_URL, f"traceability.deferred_fk/{tenant_id}"),
        payload={"orphans": state["orphans"], "ok": state["ok"]},
    )
    return state


def status_flags_view(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status_id": status["id"],
        "code": status["code"],
        "available_for_allocation": status["is_available_for_allocation"],
        "committable": status["is_committable"],
        "sellable": status["is_sellable"],
        "consigned": status["is_consigned"],
        "default": status["is_default"],
        "active": status["is_active"],
    }
