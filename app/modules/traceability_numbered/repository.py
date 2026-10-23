"""Async SQLAlchemy Core repository for FS-06 numbered inventory (lots, serials, history).

Owns DB access for the three ``traceability`` identity tables plus read-only lookups of FS-01 item
master, FS-03 registry and FS-04 ledger tables (never writes another context's tables). No commits: the
caller owns the transaction. Serial projection/history writes are performed by the 0013 posting triggers.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.inventory_ledger import tables as ledger_tables
from app.modules.item_master import tables as item_tables
from app.modules.traceability import tables as registry_tables
from app.modules.traceability_numbered import tables as t

__all__ = ["NumberedRepository", "utc_now"]

SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in dict(row).items()}


class NumberedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ helpers ---------------
    async def audit(
        self,
        *,
        tenant_id: UUID | str,
        actor_id: UUID | str | None,
        action: str,
        resource_type: str,
        resource_id: UUID | str | None,
        diff: Mapping[str, Any] | None = None,
    ) -> None:
        table = platform_models.metadata.tables["platform.audit_event"]
        await self.session.execute(
            table.insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                event_id=uuid.uuid4(),
                actor_type="USER" if actor_id else "SYSTEM",
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                diff=dict(diff or {}),
                source="API",
                occurred_at=utc_now(),
            )
        )

    async def emit_event(
        self, *, event_type: str, tenant_id: UUID | str, aggregate_id: UUID | str, payload: Mapping[str, Any]
    ) -> None:
        await emit_outbox(
            self.session,
            event_type=event_type,
            tenant_id=str(tenant_id),
            aggregate_type="traceability.numbered",
            aggregate_id=str(aggregate_id),
            payload={**{key: _jsonable(value) for key, value in payload.items()}, "tenant_id": str(tenant_id)},
        )

    # ------------------------------------------------------------------ item policy ----------
    async def item_numbering(self, tenant_id: UUID | str, item_id: UUID | str) -> dict[str, Any] | None:
        stmt = (
            select(
                item_tables.item.c.id,
                item_tables.item.c.item_number,
                item_tables.item.c.item_type_id,
                item_tables.item.c.is_active,
                item_tables.item_type_definition.c.code.label("item_type_code"),
                item_tables.item_type_definition.c.requires_numbering,
            )
            .select_from(
                item_tables.item.outerjoin(
                    item_tables.item_type_definition,
                    and_(
                        item_tables.item_type_definition.c.tenant_id == item_tables.item.c.tenant_id,
                        item_tables.item_type_definition.c.id == item_tables.item.c.item_type_id,
                    ),
                )
            )
            .where(item_tables.item.c.tenant_id == tenant_id, item_tables.item.c.id == item_id)
        )
        row = (await self.session.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def domain_enabled(self, tenant_id: UUID | str) -> bool:
        table = platform_models.metadata.tables["platform.feature_flag"]
        result = await self.session.execute(
            select(table.c.is_enabled).where(
                table.c.tenant_id == tenant_id, table.c.flag_key == "inventory.numbered.enabled"
            )
        )
        return bool(result.scalar_one_or_none())

    async def tenant_setting(self, tenant_id: UUID | str, key: str) -> Any:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        result = await self.session.execute(
            select(table.c.value).where(
                table.c.tenant_id == tenant_id,
                table.c.namespace == "inventory",
                table.c.key == key,
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ lots -----------------
    async def insert_lot(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(t.lot.insert().values(**dict(values)).returning(*t.lot.c))
        return _row_dict(result.mappings().one())

    async def get_lot(self, tenant_id: UUID | str, lot_id: UUID | str) -> dict[str, Any] | None:
        result = await self.session.execute(select(t.lot).where(t.lot.c.tenant_id == tenant_id, t.lot.c.id == lot_id))
        row = result.mappings().first()
        return _row_dict(row) if row else None

    async def get_lot_by_number(
        self, tenant_id: UUID | str, item_id: UUID | str, lot_number: str
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.lot).where(
                t.lot.c.tenant_id == tenant_id,
                t.lot.c.item_id == item_id,
                t.lot.c.lot_number == lot_number,
            )
        )
        row = result.mappings().first()
        return _row_dict(row) if row else None

    async def update_lot(
        self,
        tenant_id: UUID | str,
        lot_id: UUID | str,
        values: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        stmt = update(t.lot).where(t.lot.c.tenant_id == tenant_id, t.lot.c.id == lot_id)
        if expected_version is not None:
            stmt = stmt.where(t.lot.c.version == expected_version)
        stmt = stmt.values(**dict(values), updated_at=utc_now(), version=t.lot.c.version + 1).returning(*t.lot.c)
        result = await self.session.execute(stmt)
        row = result.mappings().first()
        return _row_dict(row) if row else None

    async def list_lots(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str | None = None,
        lot_number_prefix: str | None = None,
        expiry_before: date | None = None,
        expiry_after: date | None = None,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(t.lot).where(t.lot.c.tenant_id == tenant_id)
        if item_id is not None:
            stmt = stmt.where(t.lot.c.item_id == item_id)
        if lot_number_prefix:
            stmt = stmt.where(t.lot.c.lot_number.ilike(f"{lot_number_prefix}%"))
        if expiry_before is not None:
            stmt = stmt.where(t.lot.c.expiry_date <= expiry_before)
        if expiry_after is not None:
            stmt = stmt.where(t.lot.c.expiry_date >= expiry_after)
        if is_active is not None:
            stmt = stmt.where(t.lot.c.is_active.is_(is_active))
        stmt = stmt.order_by(t.lot.c.expiry_date.asc().nulls_last(), t.lot.c.lot_number).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).mappings().all()
        lots = [_row_dict(row) for row in rows]
        for lot in lots:
            lot["on_hand"] = str(await self.lot_on_hand(tenant_id, lot["id"]))
        return lots

    async def lot_on_hand(self, tenant_id: UUID | str, lot_id: UUID | str) -> Decimal:
        table = ledger_tables.inventory_balance_lot
        result = await self.session.execute(
            select(func.coalesce(func.sum(table.c.qty_on_hand), 0)).where(
                table.c.tenant_id == tenant_id, table.c.lot_id == lot_id
            )
        )
        return Decimal(result.scalar_one() or 0)

    async def lot_committed(self, tenant_id: UUID | str, lot_id: UUID | str) -> Decimal:
        table = ledger_tables.inventory_balance_lot
        result = await self.session.execute(
            select(func.coalesce(func.sum(table.c.qty_committed), 0)).where(
                table.c.tenant_id == tenant_id, table.c.lot_id == lot_id
            )
        )
        return Decimal(result.scalar_one() or 0)

    async def lot_positions(self, tenant_id: UUID | str, lot_id: UUID | str) -> list[dict[str, Any]]:
        balance = ledger_tables.inventory_balance_lot
        status = registry_tables.inventory_status
        location = registry_tables.location
        stmt = (
            select(
                balance.c.location_id,
                location.c.code.label("location_code"),
                balance.c.inventory_status_id,
                status.c.code.label("status_code"),
                balance.c.ownership,
                balance.c.qty_on_hand,
                balance.c.qty_committed,
                balance.c.expiry_date,
            )
            .select_from(
                balance.join(
                    location, and_(location.c.tenant_id == balance.c.tenant_id, location.c.id == balance.c.location_id)
                ).join(
                    status,
                    and_(status.c.tenant_id == balance.c.tenant_id, status.c.id == balance.c.inventory_status_id),
                )
            )
            .where(balance.c.tenant_id == tenant_id, balance.c.lot_id == lot_id)
        )
        return [_row_dict(row) for row in (await self.session.execute(stmt)).mappings().all()]

    async def lot_has_movements(self, tenant_id: UUID | str, lot_id: UUID | str) -> bool:
        line = ledger_tables.inventory_transaction_line
        result = await self.session.execute(
            select(func.count()).select_from(line).where(line.c.tenant_id == tenant_id, line.c.lot_id == lot_id)
        )
        return int(result.scalar_one() or 0) > 0

    async def fefo_lot_rows(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str,
        location_id: UUID | str | None,
        as_of: date,
        inventory_status_id: UUID | str | None = None,
        ownership: str | None = None,
    ) -> list[dict[str, Any]]:
        balance = ledger_tables.inventory_balance_lot
        stmt = (
            select(
                t.lot.c.id.label("lot_id"),
                t.lot.c.lot_number,
                t.lot.c.expiry_date,
                t.lot.c.cost_basis,
                balance.c.location_id,
                balance.c.inventory_status_id,
                balance.c.ownership,
                balance.c.qty_on_hand,
                balance.c.qty_committed,
            )
            .select_from(
                t.lot.join(
                    balance,
                    and_(
                        balance.c.tenant_id == t.lot.c.tenant_id,
                        balance.c.lot_id == t.lot.c.id,
                        balance.c.item_id == t.lot.c.item_id,
                    ),
                )
            )
            .where(
                t.lot.c.tenant_id == tenant_id,
                t.lot.c.item_id == item_id,
                t.lot.c.is_active.is_(True),
                balance.c.qty_on_hand > 0,
                or_(t.lot.c.expiry_date.is_(None), t.lot.c.expiry_date >= as_of),
            )
        )
        if location_id is not None:
            stmt = stmt.where(balance.c.location_id == location_id)
        if inventory_status_id is not None:
            stmt = stmt.where(balance.c.inventory_status_id == inventory_status_id)
        if ownership is not None:
            stmt = stmt.where(balance.c.ownership == ownership)
        stmt = stmt.order_by(t.lot.c.expiry_date.asc().nulls_last(), t.lot.c.lot_number, t.lot.c.id)
        return [_row_dict(row) for row in (await self.session.execute(stmt)).mappings().all()]

    # ------------------------------------------------------------------ serials --------------
    async def insert_serial(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(
            t.serial_number.insert().values(**dict(values)).returning(*t.serial_number.c)
        )
        return _row_dict(result.mappings().one())

    async def get_serial(self, tenant_id: UUID | str, serial_id: UUID | str) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.serial_number).where(t.serial_number.c.tenant_id == tenant_id, t.serial_number.c.id == serial_id)
        )
        row = result.mappings().first()
        return _row_dict(row) if row else None

    async def get_serial_by_number(
        self, tenant_id: UUID | str, item_id: UUID | str, serial_number: str
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(t.serial_number).where(
                t.serial_number.c.tenant_id == tenant_id,
                t.serial_number.c.item_id == item_id,
                t.serial_number.c.serial_number == serial_number,
            )
        )
        row = result.mappings().first()
        return _row_dict(row) if row else None

    async def list_serials(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str | None = None,
        serial_number: str | None = None,
        state: str | None = None,
        current_location_id: UUID | str | None = None,
        current_status_id: UUID | str | None = None,
        ownership: str | None = None,
        customer_ref: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(t.serial_number).where(t.serial_number.c.tenant_id == tenant_id)
        if item_id is not None:
            stmt = stmt.where(t.serial_number.c.item_id == item_id)
        if serial_number:
            stmt = stmt.where(t.serial_number.c.serial_number.ilike(f"{serial_number}%"))
        if state:
            stmt = stmt.where(t.serial_number.c.state == state)
        if current_location_id is not None:
            stmt = stmt.where(t.serial_number.c.current_location_id == current_location_id)
        if current_status_id is not None:
            stmt = stmt.where(t.serial_number.c.current_status_id == current_status_id)
        if ownership:
            stmt = stmt.where(t.serial_number.c.ownership == ownership)
        if customer_ref:
            stmt = stmt.where(t.serial_number.c.customer_ref == customer_ref)
        stmt = stmt.order_by(t.serial_number.c.serial_number).limit(limit).offset(offset)
        return [_row_dict(row) for row in (await self.session.execute(stmt)).mappings().all()]

    async def list_serial_history(
        self,
        tenant_id: UUID | str,
        serial_id: UUID | str,
        *,
        event: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(t.serial_number_history).where(
            t.serial_number_history.c.tenant_id == tenant_id,
            t.serial_number_history.c.serial_id == serial_id,
        )
        if event:
            stmt = stmt.where(t.serial_number_history.c.event == event)
        if from_ts is not None:
            stmt = stmt.where(t.serial_number_history.c.occurred_at >= from_ts)
        if to_ts is not None:
            stmt = stmt.where(t.serial_number_history.c.occurred_at <= to_ts)
        stmt = (
            stmt.order_by(t.serial_number_history.c.occurred_at.desc(), t.serial_number_history.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_row_dict(row) for row in (await self.session.execute(stmt)).mappings().all()]

    async def latest_serial_history(self, tenant_id: UUID | str, serial_id: UUID | str) -> dict[str, Any] | None:
        stmt = (
            select(t.serial_number_history)
            .where(
                t.serial_number_history.c.tenant_id == tenant_id,
                t.serial_number_history.c.serial_id == serial_id,
            )
            .order_by(t.serial_number_history.c.occurred_at.desc(), t.serial_number_history.c.id.desc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).mappings().first()
        return _row_dict(row) if row else None

    # ------------------------------------------------------------------ reconciliation -------
    async def deferred_fk_counts(self) -> dict[str, int]:
        """Count orphan (or missing-constraint) rows on the deferred identity columns."""
        bind = self.session
        rows = await bind.execute(
            text(
                """
                SELECT table_schema, table_name, column_name
                  FROM information_schema.columns
                 WHERE table_schema IN ('inventory_ledger', 'costing')
                   AND column_name IN ('lot_id', 'serial_id')
                """
            )
        )
        counts: dict[str, int] = {}
        for row in rows.mappings():
            schema, table, column = row["table_schema"], row["table_name"], row["column_name"]
            ident = "traceability.lot" if column == "lot_id" else "traceability.serial_number"
            key = f"{schema}.{table}.{column}"
            counts[key] = int(
                await bind.scalar(
                    text(
                        f'SELECT count(*) FROM {schema}."{table}" s WHERE s.{column} IS NOT NULL '
                        f"AND NOT EXISTS (SELECT 1 FROM {ident} identity "
                        f"WHERE identity.tenant_id = s.tenant_id AND identity.id = s.{column})"
                    )
                )
                or 0
            )
        return counts
