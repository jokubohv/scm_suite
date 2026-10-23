"""In-process `traceability.registry@1.0.0` resolver contract (FS-03 §5.6).

Dependants (FS-04 posting engine, FS-05 cost objects, FS-10 planning, FS-11 WMS) call these methods; they
perform no writes and take no locks beyond the caller's transaction. Results are stable dicts (decimal
strings where numeric) so the contract survives transport changes (in-process now, HTTP later).

Precedence rules implemented here:
* location resolution rejects inactive rows and the UNALLOCATED sentinel unless ``allow_unallocated``;
* default status: supplied → item/location → item → tenant default (consigned postings never fall back
  to the owned default);
* effective bin requirement: item uses bins AND location bins enabled AND bin_required_on_txn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.traceability import tables as t
from app.modules.traceability.errors import (
    BinsNotEnabled,
    InactiveReference,
    NotFoundError,
    StatusesNotEnabled,
    UnallocatedNotPostable,
    ValidationError,
)
from app.modules.traceability.repository import InMemoryRegistryCache, TraceabilityRepository

__all__ = ["LocationResolver"]


def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _status_flags(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status_id": str(row["id"]),
        "code": row["code"],
        "available_for_allocation": bool(row["is_available_for_allocation"]),
        "committable": bool(row["is_committable"]),
        "sellable": bool(row["is_sellable"]),
        "consigned": bool(row["is_consigned"]),
        "default": bool(row["is_default"]),
        "active": bool(row["is_active"]),
    }


class LocationResolver:
    """Stateless resolver; one instance per request/session (optional per-request cache)."""

    def __init__(self, session: AsyncSession, *, cache: InMemoryRegistryCache | None = None) -> None:
        self.session = session
        self.repo = TraceabilityRepository(session)
        self.cache = cache or InMemoryRegistryCache()

    # ---------------------------------------------------------------- locations --------------
    async def resolve(
        self, tenant_id: str | UUID, location_id: str | UUID, *, allow_unallocated: bool = False
    ) -> dict[str, Any]:
        location = await self.repo.get_location(tenant_id, location_id)
        if location is None:
            raise NotFoundError("location not found", details={"location_id": str(location_id)})
        if location["is_unallocated"] and not allow_unallocated:
            raise UnallocatedNotPostable(
                "UNALLOCATED sentinel is only usable by bootstrap commands",
                details={"location_id": location["id"]},
            )
        if location["is_inactive"]:
            raise InactiveReference("location is inactive", details={"location_id": location["id"]})
        return location

    async def expand_location_scope(
        self,
        tenant_id: str | UUID,
        location_ids: Sequence[str | UUID],
        *,
        include_descendants: bool = False,
    ) -> list[str]:
        expanded: list[str] = []
        for location_id in location_ids:
            resolved = await self.repo.get_location(tenant_id, location_id)
            if resolved is None:
                continue
            expanded.append(str(resolved["id"]))
            if include_descendants:
                expanded.extend(str(row["id"]) for row in await self.repo.descendants(tenant_id, resolved["id"]))
        return list(dict.fromkeys(expanded))

    # ---------------------------------------------------------------- statuses ---------------
    async def get_status_flags(self, tenant_id: str | UUID, status_id: str | UUID) -> dict[str, Any]:
        status = await self.repo.get_row(t.inventory_status, tenant_id, status_id)
        if status is None:
            raise NotFoundError("inventory status not found", details={"status_id": str(status_id)})
        return _status_flags(status)

    async def resolve_default_status(
        self,
        tenant_id: str | UUID,
        *,
        item_id: str | UUID | None = None,
        location_id: str | UUID | None = None,
        ownership: str = "OWNED",
        supplied_status_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        if supplied_status_id is not None:
            status = await self.repo.get_row(t.inventory_status, tenant_id, supplied_status_id)
            if status is None:
                raise NotFoundError("inventory status not found", details={"status_id": str(supplied_status_id)})
            self._assert_status_usable(status, ownership=ownership)
            return _status_flags(status)
        # item/location → item → tenant default
        if item_id is not None and location_id is not None:
            flags = await self.repo.item_location_flags(tenant_id, item_id, location_id)
            if flags and flags.get("default_status_id"):
                status = await self.repo.get_row(t.inventory_status, tenant_id, flags["default_status_id"])
                if status is not None:
                    self._assert_status_usable(status, ownership=ownership)
                    return _status_flags(status)
        if item_id is not None:
            item = await self.repo.item_flags(tenant_id, item_id)
            if item and item.get("default_status_id"):
                status = await self.repo.get_row(t.inventory_status, tenant_id, item["default_status_id"])
                if status is not None:
                    self._assert_status_usable(status, ownership=ownership)
                    return _status_flags(status)
        default = await self.repo.default_status(tenant_id)
        if default is None:
            raise ValidationError("no default inventory status is configured", details={"code": "NO_DEFAULT_STATUS"})
        if ownership.upper() == "CONSIGNED" and not default["is_consigned"]:
            raise ValidationError(
                "consigned posting requires a consigned inventory status (no owned-default fallback)",
                details={"ownership": ownership},
            )
        self._assert_status_usable(default, ownership=ownership)
        return _status_flags(default)

    @staticmethod
    def _assert_status_usable(status: Mapping[str, Any], *, ownership: str) -> None:
        if not status["is_active"]:
            raise InactiveReference("inventory status is inactive", details={"status_id": str(status["id"])})
        if ownership.upper() == "CONSIGNED" and not status["is_consigned"]:
            raise ValidationError(
                "consigned ownership requires a consigned status",
                details={"status_id": str(status["id"])},
            )
        if ownership.upper() == "OWNED" and status["is_consigned"]:
            raise ValidationError(
                "owned ownership cannot use a consigned status",
                details={"status_id": str(status["id"])},
            )

    # ---------------------------------------------------------------- position defaults -------
    async def resolve_position_defaults(
        self,
        tenant_id: str | UUID,
        *,
        item_id: str | UUID,
        location_id: str | UUID,
        supplied_status_id: str | UUID | None = None,
        supplied_bin_id: str | UUID | None = None,
        movement_direction: str = "IN",
        ownership: str = "OWNED",
    ) -> dict[str, Any]:
        location = await self.repo.get_location(tenant_id, location_id)
        if location is None:
            raise NotFoundError("location not found", details={"location_id": str(location_id)})
        item = await self.repo.item_flags(tenant_id, item_id)
        if item is None:
            raise NotFoundError("item not found", details={"item_id": str(item_id)})
        status = await self.resolve_default_status(
            tenant_id,
            item_id=item_id,
            location_id=location_id,
            ownership=ownership,
            supplied_status_id=supplied_status_id,
        )
        use_bins = bool(item.get("use_bins"))
        bins_enabled = bool(location["is_bins_enabled"]) and use_bins
        bin_required = bins_enabled and bool(item.get("bin_required_on_txn"))
        default_bin_id = supplied_bin_id
        if default_bin_id is None and bins_enabled:
            item_location = await self.repo.item_location_flags(tenant_id, item_id, location_id)
            if item_location and item_location.get("default_bin_id"):
                default_bin_id = item_location["default_bin_id"]
            if default_bin_id is None:
                bins = await self.repo.list_bins(tenant_id, location_id=location_id, is_active=True, limit=20)
                preference = "is_default_receiving" if movement_direction.upper() == "IN" else "is_default_picking"
                preferred = [
                    row for row in bins if (await self.repo.item_bin_exists(tenant_id, item_id, location_id, row["id"]))
                ]
                del preference
                if preferred:
                    default_bin_id = preferred[0]["id"]
        statuses_enabled = bool(location["is_statuses_enabled"]) and bool(item.get("use_statuses", True))
        if not statuses_enabled and supplied_status_id is not None and not status["default"]:
            raise StatusesNotEnabled(
                "statuses are not enabled for this item/location", details={"location_id": str(location_id)}
            )
        return {
            "location_id": str(location["id"]),
            "inventory_status_id": status["status_id"],
            "status_flags": status,
            "bins_enabled": bins_enabled,
            "bin_required": bin_required,
            "default_bin_id": default_bin_id,
            "statuses_enabled": statuses_enabled,
        }

    # ---------------------------------------------------------------- validation --------------
    async def validate_posting_position(
        self,
        tenant_id: str | UUID,
        *,
        item_id: str | UUID,
        location_id: str | UUID,
        bin_id: str | UUID | None = None,
        inventory_status_id: str | UUID | None = None,
        ownership: str = "OWNED",
        qty_class: str = "ON_HAND",
        allow_unallocated: bool = False,
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        normalized: dict[str, Any] = {
            "location_id": str(location_id),
            "bin_id": str(bin_id) if bin_id else None,
            "inventory_status_id": str(inventory_status_id) if inventory_status_id else None,
        }
        try:
            location = await self.resolve(tenant_id, location_id, allow_unallocated=allow_unallocated)
            normalized["location_id"] = location["id"]
        except (NotFoundError, InactiveReference, UnallocatedNotPostable) as exc:
            errors.append({"path": "location_id", "code": exc.code, "message": exc.message})
            return {"ok": False, "normalized": normalized, "errors": errors, "warnings": warnings}
        item = await self.repo.item_flags(tenant_id, item_id)
        if item is None:
            errors.append({"path": "item_id", "code": "UNKNOWN_ITEM", "message": "item not found"})
        elif not bool(item["is_active"]):
            errors.append({"path": "item_id", "code": "INACTIVE_REFERENCE", "message": "item is inactive"})
        if ownership.upper() == "CONSIGNED" and not location["is_consignment_allowed"]:
            errors.append(
                {
                    "path": "ownership",
                    "code": "CONSIGNMENT_NOT_ALLOWED",
                    "message": "location does not allow consigned inventory",
                }
            )
        try:
            defaults = await self.resolve_position_defaults(
                tenant_id,
                item_id=item_id,
                location_id=location_id,
                supplied_status_id=inventory_status_id,
                supplied_bin_id=bin_id,
                ownership=ownership,
            )
            normalized["inventory_status_id"] = defaults["inventory_status_id"]
            if bin_id is not None:
                if not defaults["bins_enabled"]:
                    errors.append(
                        {
                            "path": "bin_id",
                            "code": "BINS_NOT_ENABLED",
                            "message": "bins are not enabled for this item/location",
                        }
                    )
                else:
                    bin_row = await self.repo.get_row(t.bin, tenant_id, bin_id)
                    if bin_row is None:
                        errors.append({"path": "bin_id", "code": "UNKNOWN_BIN", "message": "bin not found"})
                    elif str(bin_row["location_id"]) != str(location["id"]):
                        errors.append(
                            {
                                "path": "bin_id",
                                "code": "BIN_LOCATION_MISMATCH",
                                "message": "bin belongs to another location",
                            }
                        )
                    elif not bin_row["is_active"]:
                        errors.append({"path": "bin_id", "code": "INACTIVE_REFERENCE", "message": "bin is inactive"})
                    else:
                        normalized["bin_id"] = bin_row["id"]
            elif defaults["bin_required"]:
                errors.append(
                    {"path": "bin_id", "code": "BIN_REQUIRED", "message": "a bin is required for this item/location"}
                )
            if qty_class.upper() not in {"ON_HAND", "IN_TRANSIT"}:
                errors.append({"path": "qty_class", "code": "VALIDATION_FAILED", "message": "unknown qty_class"})
        except (NotFoundError, InactiveReference, ValidationError, BinsNotEnabled, StatusesNotEnabled) as exc:
            errors.append({"path": "inventory_status_id", "code": exc.code, "message": exc.message})
        return {"ok": not errors, "normalized": normalized, "errors": errors, "warnings": warnings}

    # ---------------------------------------------------------------- distribution -----------
    async def validate_distribution_target(
        self,
        tenant_id: str | UUID,
        *,
        lines: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        sentinel = await self.repo.get_sentinel(tenant_id)
        if sentinel is None:
            errors.append(
                {"path": "source", "code": "UNALLOCATED_LOCATION_REQUIRED", "message": "sentinel location missing"}
            )
        seen: dict[str, int] = {}
        for index, line in enumerate(lines):
            item_id = str(line.get("item_id") or "")
            seen[item_id] = seen.get(item_id, 0) + 1
            try:
                location = await self.resolve(tenant_id, str(line.get("to_location_id")))
            except (NotFoundError, InactiveReference, UnallocatedNotPostable) as exc:
                errors.append({"path": f"lines[{index}].to_location_id", "code": exc.code, "message": exc.message})
                continue
            try:
                await self.resolve_position_defaults(
                    tenant_id,
                    item_id=_as_uuid(line["item_id"]),
                    location_id=location["id"],
                    supplied_status_id=line.get("inventory_status_id"),
                    supplied_bin_id=line.get("bin_id"),
                )
            except (NotFoundError, InactiveReference, ValidationError, BinsNotEnabled, StatusesNotEnabled) as exc:
                errors.append({"path": f"lines[{index}]", "code": exc.code, "message": exc.message})
        return {"ok": not errors, "errors": errors, "line_count": len(lines)}

    async def bootstrap_state(self, tenant_id: str | UUID) -> dict[str, Any]:
        state = await self.repo.bootstrap_state(tenant_id)
        location = (
            await self.repo.get_location(tenant_id, state["sentinel_location_id"])
            if state["sentinel_location_id"]
            else None
        )
        del location
        return state
