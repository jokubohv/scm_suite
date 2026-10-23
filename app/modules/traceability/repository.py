"""Async SQLAlchemy Core repository for the FS-03 traceability registry.

Owns all DB access for the four ``traceability`` tables plus the read-only FS-01 item-master lookups
needed by posting-position validation (item/inventory flags and bin/status pointers). No commits
(caller owns the transaction), no ORM, no writes to FS-01 tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.traceability import tables as t
from app.modules.traceability.errors import NotFoundError

__all__ = ["InMemoryRegistryCache", "TraceabilityRepository"]

SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")

STATUS_SEEDS: tuple[tuple[str, str, bool, bool, bool, bool, bool, bool], ...] = (
    ("AVAILABLE", "Available", True, True, True, False, True, True),
    ("HOLD", "Hold", False, False, False, False, False, False),
    ("DAMAGED", "Damaged", False, False, False, False, False, False),
    ("RETURNED", "Returned", False, False, False, False, False, False),
    ("CONSIGNED_GOOD", "Consigned Good", True, True, True, True, False, True),
    ("USED", "Used", True, False, False, False, False, False),
    ("REFURB", "Refurbished", True, False, False, False, False, False),
)
DEFAULT_STATUS_CODE = "AVAILABLE"
SENTINEL_CODE = "UNALLOCATED"


class InMemoryRegistryCache:
    """Tiny per-request registry cache for resolver calls within one posting transaction."""

    def __init__(self) -> None:
        self.locations: dict[UUID, dict[str, Any]] = {}
        self.statuses: dict[UUID, dict[str, Any]] = {}

    def clear(self) -> None:
        self.locations.clear()
        self.statuses.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _row(mapping: Any) -> dict[str, Any]:
    return {key: _iso(value) for key, value in dict(mapping).items()}


def _as_uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


class TraceabilityRepository:
    """Request-scoped repository."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- platform helpers -------
    async def audit(
        self,
        *,
        tenant_id: str | UUID,
        actor_id: str | UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | UUID | None,
        diff: Mapping[str, Any] | None = None,
    ) -> None:
        event_id = uuid.uuid4()
        table = platform_models.metadata.tables["platform.audit_event"]
        await self.session.execute(
            table.insert().values(
                id=event_id,
                tenant_id=_as_uuid(tenant_id),
                event_id=event_id,
                actor_type="USER" if actor_id else "SYSTEM",
                actor_id=_as_uuid(actor_id) if actor_id else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source="API",
                occurred_at=_now(),
            )
        )

    async def emit(
        self,
        *,
        tenant_id: str | UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | UUID,
        payload: Mapping[str, Any],
    ) -> None:
        await emit_outbox(
            self.session,
            event_type=event_type,
            tenant_id=str(tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=dict(payload),
            event_version="1.0.0",
            aggregate_version=1,
        )

    async def tenant_row(self, tenant_id: str | UUID) -> dict[str, Any] | None:
        table = platform_models.metadata.tables["platform.tenant"]
        result = await self.session.execute(select(table).where(table.c.id == _as_uuid(tenant_id)))
        mapping = result.mappings().first()
        return dict(mapping) if mapping else None

    # ---------------------------------------------------------------- bootstrap --------------
    async def ensure_bootstrap(self, tenant_id: str | UUID) -> dict[str, Any]:
        """Idempotently ensure sentinel + status catalogue for a tenant (lazy provisioning)."""
        tid = _as_uuid(tenant_id)
        tenant = await self.tenant_row(tid)
        if tenant is None:
            raise NotFoundError("tenant not found", details={"tenant_id": str(tid)})
        sentinel = await self.get_sentinel(tid)
        if sentinel is None:
            timezone = str(tenant.get("timezone") or "UTC")
            statuses_enabled = True
            await self.session.execute(
                t.location.insert().values(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    code=SENTINEL_CODE,
                    name="Unallocated",
                    parent_id=None,
                    location_type="UNALLOCATED",
                    is_bins_enabled=False,
                    is_statuses_enabled=statuses_enabled,
                    is_consignment_allowed=False,
                    allow_commit_from_non_available=False,
                    subsidiary_ref=None,
                    timezone=timezone,
                    address=None,
                    is_unallocated=True,
                    is_inactive=False,
                    version=1,
                )
            )
        existing = {
            str(row["code"])
            for row in (
                await self.session.execute(
                    select(t.inventory_status.c.code).where(t.inventory_status.c.tenant_id == tid)
                )
            )
            .mappings()
            .all()
        }
        for code, name, available, committable, sellable, consigned, is_default, is_system in STATUS_SEEDS:
            if code in existing:
                continue
            await self.session.execute(
                t.inventory_status.insert().values(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    code=code,
                    name=name,
                    is_available_for_allocation=available,
                    is_committable=committable,
                    is_sellable=sellable,
                    is_consigned=consigned,
                    is_default=is_default,
                    is_system=is_system,
                    is_active=True,
                    version=1,
                )
            )
        sentinel = await self.get_sentinel(tid)
        assert sentinel is not None
        return sentinel

    async def get_sentinel(self, tenant_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.location).where(
                t.location.c.tenant_id == _as_uuid(tenant_id), t.location.c.is_unallocated.is_(True)
            )
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def bootstrap_state(self, tenant_id: str | UUID) -> dict[str, Any]:
        tid = _as_uuid(tenant_id)
        sentinel = await self.get_sentinel(tid)
        status_count = (
            await self.session.execute(
                select(func.count()).select_from(t.inventory_status).where(t.inventory_status.c.tenant_id == tid)
            )
        ).scalar_one()
        default = (
            (
                await self.session.execute(
                    select(t.inventory_status.c.id, t.inventory_status.c.code).where(
                        t.inventory_status.c.tenant_id == tid, t.inventory_status.c.is_default.is_(True)
                    )
                )
            )
            .mappings()
            .first()
        )
        return {
            "tenant_id": str(tid),
            "sentinel_present": sentinel is not None,
            "sentinel_location_id": sentinel["id"] if sentinel else None,
            "status_count": int(status_count),
            "default_status_code": default["code"] if default else None,
            "seeded": sentinel is not None and int(status_count) >= len(STATUS_SEEDS),
        }

    # ---------------------------------------------------------------- generic primitives ----
    async def get_row(self, table: Any, tenant_id: str | UUID, row_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(table).where(table.c.tenant_id == _as_uuid(tenant_id), table.c.id == _as_uuid(row_id))
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def list_rows(
        self,
        table: Any,
        tenant_id: str | UUID,
        *,
        filters: Mapping[str, Any] | None = None,
        order_by: Sequence[Any] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(table).where(table.c.tenant_id == _as_uuid(tenant_id))
        for key, value in (filters or {}).items():
            if value is not None:
                stmt = stmt.where(table.c[key] == value)
        stmt = stmt.order_by(*(order_by or (table.c.code,))).limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def insert_row(
        self, table: Any, tenant_id: str | UUID, values: Mapping[str, Any], actor_id: str | UUID | None
    ) -> dict[str, Any]:
        row_id = values.get("id") or uuid.uuid4()
        now = _now()
        payload = {
            "id": row_id,
            "tenant_id": _as_uuid(tenant_id),
            **dict(values),
            "created_at": now,
            "updated_at": now,
            "created_by": _as_uuid(actor_id) if actor_id else None,
            "updated_by": _as_uuid(actor_id) if actor_id else None,
            "version": 1,
        }
        async with self.session.begin_nested():
            await self.session.execute(table.insert().values(**payload))
        fetched = await self.get_row(table, tenant_id, row_id)
        assert fetched is not None
        return fetched

    async def update_row(
        self,
        table: Any,
        tenant_id: str | UUID,
        row_id: str | UUID,
        values: Mapping[str, Any],
        actor_id: str | UUID | None,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = await self.get_row(table, tenant_id, row_id)
        if current is None:
            raise NotFoundError("resource not found", details={"id": str(row_id)})
        if expected_version is not None and int(current["version"]) != expected_version:
            from app.modules.traceability.errors import StaleVersion

            raise StaleVersion(
                "resource version does not match If-Match",
                details={"current_version": current["version"], "expected_version": expected_version},
            )
        payload = {
            **dict(values),
            "updated_at": _now(),
            "updated_by": _as_uuid(actor_id) if actor_id else None,
            "version": table.c.version + 1,
        }
        async with self.session.begin_nested():
            await self.session.execute(
                update(table)
                .where(table.c.tenant_id == _as_uuid(tenant_id), table.c.id == _as_uuid(row_id))
                .values(**payload)
            )
        fetched = await self.get_row(table, tenant_id, row_id)
        assert fetched is not None
        return fetched

    # ---------------------------------------------------------------- locations --------------
    async def list_locations(
        self,
        tenant_id: str | UUID,
        *,
        code: str | None = None,
        location_type: str | None = None,
        parent_id: str | UUID | None = None,
        is_inactive: bool | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(t.location).where(t.location.c.tenant_id == _as_uuid(tenant_id))
        if code:
            stmt = stmt.where(t.location.c.code == code.strip().upper())
        if location_type:
            stmt = stmt.where(t.location.c.location_type == location_type.strip().upper())
        if parent_id is not None:
            stmt = stmt.where(t.location.c.parent_id == _as_uuid(parent_id))
        if is_inactive is not None:
            stmt = stmt.where(t.location.c.is_inactive.is_(is_inactive))
        stmt = stmt.order_by(t.location.c.code).limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def get_location(self, tenant_id: str | UUID, location_id: str | UUID) -> dict[str, Any] | None:
        return await self.get_row(t.location, tenant_id, location_id)

    async def get_location_by_code(self, tenant_id: str | UUID, code: str) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.location).where(
                t.location.c.tenant_id == _as_uuid(tenant_id), t.location.c.code == code.strip().upper()
            )
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def children(
        self, tenant_id: str | UUID, location_id: str | UUID, *, active_only: bool = False
    ) -> list[dict[str, Any]]:
        stmt = select(t.location).where(
            t.location.c.tenant_id == _as_uuid(tenant_id), t.location.c.parent_id == _as_uuid(location_id)
        )
        if active_only:
            stmt = stmt.where(t.location.c.is_inactive.is_(False))
        stmt = stmt.order_by(t.location.c.code)
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def descendants(
        self, tenant_id: str | UUID, location_id: str | UUID, *, max_depth: int = 10
    ) -> list[dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                WITH RECURSIVE tree AS (
                    SELECT l.id, l.code, l.name, l.parent_id, l.location_type, l.is_inactive, 1 AS depth
                      FROM traceability.location l
                     WHERE l.tenant_id = :tenant_id AND l.id = :location_id
                    UNION ALL
                    SELECT c.id, c.code, c.name, c.parent_id, c.location_type, c.is_inactive, tree.depth + 1
                      FROM traceability.location c
                      JOIN tree ON c.parent_id = tree.id AND c.tenant_id = :tenant_id
                     WHERE tree.depth < :max_depth
                )
                SELECT id, code, name, parent_id, location_type, is_inactive, depth
                  FROM tree WHERE depth > 1 ORDER BY depth, code
                """
            ),
            {"tenant_id": str(tenant_id), "location_id": str(location_id), "max_depth": int(max_depth)},
        )
        return [_row(mapping) for mapping in result.mappings().all()]

    async def active_children_count(self, tenant_id: str | UUID, location_id: str | UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(t.location)
            .where(
                t.location.c.tenant_id == _as_uuid(tenant_id),
                t.location.c.parent_id == _as_uuid(location_id),
                t.location.c.is_inactive.is_(False),
            )
        )
        return int(result.scalar_one() or 0)

    # ---------------------------------------------------------------- zones -------------------
    async def list_zones(
        self,
        tenant_id: str | UUID,
        *,
        location_id: str | UUID | None = None,
        is_active: bool | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if location_id is not None:
            filters["location_id"] = _as_uuid(location_id)
        if is_active is not None:
            filters["is_active"] = is_active
        return await self.list_rows(
            t.zone,
            tenant_id,
            filters=filters or None,
            order_by=(t.zone.c.location_id, t.zone.c.priority, t.zone.c.code),
            limit=limit,
        )

    # ---------------------------------------------------------------- bins --------------------
    async def list_bins(
        self,
        tenant_id: str | UUID,
        *,
        location_id: str | UUID | None = None,
        zone_id: str | UUID | None = None,
        bin_type: str | None = None,
        is_active: bool | None = None,
        pickable_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(t.bin).where(t.bin.c.tenant_id == _as_uuid(tenant_id))
        if location_id is not None:
            stmt = stmt.where(t.bin.c.location_id == _as_uuid(location_id))
        if zone_id is not None:
            stmt = stmt.where(t.bin.c.zone_id == _as_uuid(zone_id))
        if bin_type:
            stmt = stmt.where(t.bin.c.bin_type == bin_type.strip().upper())
        if is_active is not None:
            stmt = stmt.where(t.bin.c.is_active.is_(is_active))
        if pickable_only:
            stmt = stmt.where(t.bin.c.is_pickable.is_(True), t.bin.c.is_active.is_(True))
        stmt = stmt.order_by(t.bin.c.location_id, t.bin.c.code).limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def active_bins_count(self, tenant_id: str | UUID, location_id: str | UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(t.bin)
            .where(
                t.bin.c.tenant_id == _as_uuid(tenant_id),
                t.bin.c.location_id == _as_uuid(location_id),
                t.bin.c.is_active.is_(True),
            )
        )
        return int(result.scalar_one() or 0)

    async def bin_balance_count(self, tenant_id: str | UUID, bin_id: str | UUID) -> int:
        """Stock-coupled guard: count non-zero ledger bin positions when FS-04's table exists."""
        exists = await self.session.execute(
            text("SELECT to_regclass('inventory_ledger.inventory_balance_bin') IS NOT NULL")
        )
        if not bool(exists.scalar()):
            return 0
        result = await self.session.execute(
            text(
                "SELECT count(*) FROM inventory_ledger.inventory_balance_bin "
                "WHERE tenant_id = :tenant_id AND bin_id = :bin_id AND qty_on_hand <> 0"
            ),
            {"tenant_id": str(tenant_id), "bin_id": str(bin_id)},
        )
        return int(result.scalar_one() or 0)

    async def location_balance_count(self, tenant_id: str | UUID, location_id: str | UUID) -> int:
        exists = await self.session.execute(
            text("SELECT to_regclass('inventory_ledger.inventory_balance') IS NOT NULL")
        )
        if not bool(exists.scalar()):
            return 0
        result = await self.session.execute(
            text(
                "SELECT count(*) FROM inventory_ledger.inventory_balance "
                "WHERE tenant_id = :tenant_id AND location_id = :location_id "
                "AND (qty_on_hand <> 0 OR qty_committed <> 0)"
            ),
            {"tenant_id": str(tenant_id), "location_id": str(location_id)},
        )
        return int(result.scalar_one() or 0)

    async def status_balance_count(self, tenant_id: str | UUID, status_id: str | UUID) -> int:
        exists = await self.session.execute(
            text("SELECT to_regclass('inventory_ledger.inventory_balance') IS NOT NULL")
        )
        if not bool(exists.scalar()):
            return 0
        result = await self.session.execute(
            text(
                "SELECT count(*) FROM inventory_ledger.inventory_balance "
                "WHERE tenant_id = :tenant_id AND inventory_status_id = :status_id AND qty_on_hand <> 0"
            ),
            {"tenant_id": str(tenant_id), "status_id": str(status_id)},
        )
        return int(result.scalar_one() or 0)

    async def stock_guard_installed(self) -> bool:
        """FS-03 §5.4.1 cross-FS migration contract: FS-04 installs the stock-coupled guards."""
        result = await self.session.execute(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                "('trg_location_inactive_guard','trg_bin_inactive_guard','trg_status_inactive_guard') "
                "AND NOT tgisinternal"
            )
        )
        return int(result.scalar_one() or 0) == 3

    # ---------------------------------------------------------------- statuses ----------------
    async def list_statuses(
        self, tenant_id: str | UUID, *, is_active: bool | None = None, include_system: bool = True, limit: int = 200
    ) -> list[dict[str, Any]]:
        stmt = select(t.inventory_status).where(t.inventory_status.c.tenant_id == _as_uuid(tenant_id))
        if is_active is not None:
            stmt = stmt.where(t.inventory_status.c.is_active.is_(is_active))
        if not include_system:
            stmt = stmt.where(t.inventory_status.c.is_system.is_(False))
        stmt = stmt.order_by(t.inventory_status.c.is_default.desc(), t.inventory_status.c.code).limit(
            max(1, min(limit, 500))
        )
        return [_row(mapping) for mapping in (await self.session.execute(stmt)).mappings().all()]

    async def default_status(self, tenant_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.inventory_status).where(
                t.inventory_status.c.tenant_id == _as_uuid(tenant_id), t.inventory_status.c.is_default.is_(True)
            )
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def get_status_by_code(self, tenant_id: str | UUID, code: str) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.inventory_status).where(
                t.inventory_status.c.tenant_id == _as_uuid(tenant_id),
                t.inventory_status.c.code == code.strip().upper(),
            )
        )
        mapping = result.mappings().first()
        return _row(mapping) if mapping else None

    async def clear_default_status(self, tenant_id: str | UUID, *, except_id: str | UUID | None = None) -> None:
        stmt = (
            update(t.inventory_status)
            .where(t.inventory_status.c.tenant_id == _as_uuid(tenant_id), t.inventory_status.c.is_default.is_(True))
            .values(is_default=False)
        )
        if except_id is not None:
            stmt = stmt.where(t.inventory_status.c.id != _as_uuid(except_id))
        await self.session.execute(stmt)

    # ---------------------------------------------------------------- FS-01 read lookups -----
    async def item_flags(self, tenant_id: str | UUID, item_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                SELECT i.id, i.is_active, i.item_number, i.item_category_id, i.matrix_parent_id,
                       inv.use_bins, inv.bin_required_on_txn, inv.use_statuses, inv.default_status_id,
                       EXISTS (SELECT 1 FROM item_master.item_bin ib
                                WHERE ib.tenant_id = i.tenant_id AND ib.item_id = i.id) AS has_item_bins
                  FROM item_master.item i
                  LEFT JOIN item_master.item_inventory inv
                         ON inv.tenant_id = i.tenant_id AND inv.item_id = i.id
                 WHERE i.tenant_id = :tenant_id AND i.id = :item_id
                """
            ),
            {"tenant_id": str(tenant_id), "item_id": str(item_id)},
        )
        mapping = result.mappings().first()
        return dict(mapping) if mapping else None

    async def item_location_flags(
        self, tenant_id: str | UUID, item_id: str | UUID, location_id: str | UUID
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                SELECT il.default_status_id, il.default_bin_id, il.is_consigned_allowed,
                       inv.use_bins, inv.bin_required_on_txn, inv.use_statuses
                  FROM item_master.item_location il
                  LEFT JOIN item_master.item_inventory inv
                         ON inv.tenant_id = il.tenant_id AND inv.item_id = il.item_id
                 WHERE il.tenant_id = :tenant_id AND il.item_id = :item_id AND il.location_id = :location_id
                """
            ),
            {"tenant_id": str(tenant_id), "item_id": str(item_id), "location_id": str(location_id)},
        )
        mapping = result.mappings().first()
        return dict(mapping) if mapping else None

    async def item_bin_exists(
        self, tenant_id: str | UUID, item_id: str | UUID, location_id: str | UUID, bin_id: str | UUID
    ) -> bool:
        result = await self.session.execute(
            text(
                "SELECT 1 FROM item_master.item_bin ib "
                "WHERE ib.tenant_id = :tenant_id AND ib.item_id = :item_id "
                "AND ib.location_id = :location_id AND ib.bin_id = :bin_id"
            ),
            {
                "tenant_id": str(tenant_id),
                "item_id": str(item_id),
                "location_id": str(location_id),
                "bin_id": str(bin_id),
            },
        )
        return result.first() is not None

    async def uom_exists(self, tenant_id: str | UUID, uom_id: str | UUID) -> bool:
        result = await self.session.execute(
            text("SELECT 1 FROM item_master.uom WHERE tenant_id = :tenant_id AND id = :uom_id"),
            {"tenant_id": str(tenant_id), "uom_id": str(uom_id)},
        )
        return result.first() is not None

    async def item_location_default(self, tenant_id: str | UUID, item_id: str | UUID) -> dict[str, Any] | None:
        """Return the single item_location row when unambiguous (posting-eligibility without bin)."""
        result = await self.session.execute(
            text(
                "SELECT location_id, default_bin_id, default_status_id FROM item_master.item_location "
                "WHERE tenant_id = :tenant_id AND item_id = :item_id"
            ),
            {"tenant_id": str(tenant_id), "item_id": str(item_id)},
        )
        rows = [dict(mapping) for mapping in result.mappings().all()]
        if not rows:
            return None
        if len(rows) > 1:
            return None
        return rows[0]

    # ---------------------------------------------------------------- deferred-FK job --------
    async def deferred_fk_orphans(self, tenant_id: str | UUID) -> dict[str, int]:
        checks = {
            "item_location.location_id": "SELECT count(*) FROM item_master.item_location s WHERE s.tenant_id = :t AND s.location_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM traceability.location x WHERE x.tenant_id = s.tenant_id AND x.id = s.location_id)",
            "item_location.default_bin_id": "SELECT count(*) FROM item_master.item_location s WHERE s.tenant_id = :t AND s.default_bin_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM traceability.bin x WHERE x.tenant_id = s.tenant_id AND x.id = s.default_bin_id)",
            "item_location.default_status_id": "SELECT count(*) FROM item_master.item_location s WHERE s.tenant_id = :t AND s.default_status_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM traceability.inventory_status x WHERE x.tenant_id = s.tenant_id AND x.id = s.default_status_id)",
            "item_bin.location_id": "SELECT count(*) FROM item_master.item_bin s WHERE s.tenant_id = :t AND NOT EXISTS (SELECT 1 FROM traceability.location x WHERE x.tenant_id = s.tenant_id AND x.id = s.location_id)",
            "item_bin.bin_id": "SELECT count(*) FROM item_master.item_bin s WHERE s.tenant_id = :t AND NOT EXISTS (SELECT 1 FROM traceability.bin x WHERE x.tenant_id = s.tenant_id AND x.id = s.bin_id)",
            "item_inventory.default_status_id": "SELECT count(*) FROM item_master.item_inventory s WHERE s.tenant_id = :t AND s.default_status_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM traceability.inventory_status x WHERE x.tenant_id = s.tenant_id AND x.id = s.default_status_id)",
        }
        counts: dict[str, int] = {}
        for name, sql in checks.items():
            result = await self.session.execute(text(sql), {"t": str(tenant_id)})
            counts[name] = int(result.scalar_one() or 0)
        return counts
