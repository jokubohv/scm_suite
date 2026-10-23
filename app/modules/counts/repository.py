"""FS-07 counts persistence and FS-04/FS-05 integration bridge (async SQLAlchemy Core).

Owns all writes to the six ``counts`` tables. Balance snapshots are read through the frozen
``inventory.balances@1.0.0`` read port; T-20 variance postings go through the in-process
``inventory.ledger@1.0.0`` ``PostingPort.post_count_variance`` facade (no HTTP, no direct writes to
FS-04 tables, costing valuation happens inside the posting transaction).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import emit as emit_outbox
from app.modules.counts import tables as t
from app.modules.counts.engine import (
    CountPosition,
    CountSettings,
    build_t20_command,
    count_ref,
    q12,
)
from app.modules.counts.errors import CountsNotFound, CountsValidationError

TWELVE = Decimal("0.000000000001")


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(TWELVE), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    return value


def _row(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json(value) for key, value in mapping.items()}


def _one(mapping: Any) -> dict[str, Any] | None:
    return _row(dict(mapping)) if mapping is not None else None


def _uuid(value: str | UUID, *, field: str = "id") -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise CountsValidationError(f"{field} must be a UUID") from exc


class CountsRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")

    # ------------------------------------------------------------------ plans -----------------
    async def _select(self, table: Any, *conditions: Any, limit: int | None = None) -> list[dict[str, Any]]:
        statement = select(table).where(table.c.tenant_id == self.tenant_id, *conditions)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return [_row(dict(row)) for row in result.mappings().all()]

    async def _get(self, table: Any, row_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(table).where(table.c.tenant_id == self.tenant_id, table.c.id == _uuid(row_id))
        )
        return _one(result.mappings().first())

    async def _insert(self, table: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        row_id = values.get("id") or uuid4()
        payload = {"id": row_id, "tenant_id": self.tenant_id, **dict(values)}
        await self.session.execute(insert(table).values(**payload))
        await self.session.flush()
        row = await self._get(table, row_id)
        if row is None:  # pragma: no cover - insert/read race only
            raise CountsValidationError("inserted row could not be read back")
        return row

    async def _update(self, table: Any, row_id: str | UUID, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        if "version" in table.c and "version" not in payload:
            payload["version"] = table.c.version + 1
        result = await self.session.execute(
            update(table)
            .where(table.c.tenant_id == self.tenant_id, table.c.id == _uuid(row_id))
            .values(**payload)
            .returning(table)
        )
        row = result.mappings().first()
        if row is None:
            raise CountsNotFound(f"{table.name} row not found")
        return _row(dict(row))

    async def list_plans(
        self, *, status: str | None = None, frequency: str | None = None, next_run_before: date | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.count_plan.c.status == status.upper())
        if frequency is not None:
            conditions.append(t.count_plan.c.frequency == frequency.upper())
        if next_run_before is not None:
            conditions.append(t.count_plan.c.next_run_date <= next_run_before)
        plans = await self._select(t.count_plan, *conditions, limit=max(1, min(limit, 500)))
        for plan in plans:
            plan["filters"] = await self.list_filters(plan["id"])
        return plans

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        plan = await self._get(t.count_plan, plan_id)
        if plan is not None:
            plan["filters"] = await self.list_filters(plan_id)
        return plan

    async def list_filters(self, plan_id: str) -> list[dict[str, Any]]:
        return await self._select(
            t.count_plan_filter,
            t.count_plan_filter.c.count_plan_id == _uuid(plan_id),
        )

    async def create_plan(self, values: Mapping[str, Any], filters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        plan = await self._insert(t.count_plan, values)
        for spec in filters:
            await self._insert(t.count_plan_filter, {"count_plan_id": plan["id"], **dict(spec)})
        return await self.get_plan(plan["id"]) or plan

    async def update_plan(
        self, plan_id: str, values: Mapping[str, Any], filters: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        await self._update(t.count_plan, plan_id, values)
        if filters is not None:
            await self.session.execute(
                delete(t.count_plan_filter).where(
                    t.count_plan_filter.c.tenant_id == self.tenant_id,
                    t.count_plan_filter.c.count_plan_id == _uuid(plan_id),
                )
            )
            for spec in filters:
                await self._insert(t.count_plan_filter, {"count_plan_id": plan_id, **dict(spec)})
        return await self.get_plan(plan_id) or {}

    async def set_plan_status(self, plan_id: str, status: str, *, next_run_date: date | None = None, last_run_at: datetime | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {"status": status}
        if next_run_date is not None:
            values["next_run_date"] = next_run_date
        if last_run_at is not None:
            values["last_run_at"] = last_run_at
        return await self._update(t.count_plan, plan_id, values)

    async def due_plans(self, *, as_of: date | None = None) -> list[dict[str, Any]]:
        today = as_of or date.today()
        return await self._select(
            t.count_plan,
            t.count_plan.c.status == "ACTIVE",
            t.count_plan.c.next_run_date <= today,
        )

    # ------------------------------------------------------------------ counts ----------------
    async def list_counts(
        self, *, status: str | None = None, count_type: str | None = None, plan_id: str | None = None,
        count_date_from: date | None = None, count_date_to: date | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.inventory_count.c.status == status.upper())
        if count_type is not None:
            conditions.append(t.inventory_count.c.count_type == count_type.upper())
        if plan_id is not None:
            conditions.append(t.inventory_count.c.count_plan_id == _uuid(plan_id))
        if count_date_from is not None:
            conditions.append(t.inventory_count.c.count_date >= count_date_from)
        if count_date_to is not None:
            conditions.append(t.inventory_count.c.count_date <= count_date_to)
        return await self._select(t.inventory_count, *conditions, limit=max(1, min(limit, 500)))

    async def get_count(self, count_id: str) -> dict[str, Any] | None:
        count = await self._get(t.inventory_count, count_id)
        if count is not None:
            count["line_count"] = int(
                (
                    await self.session.execute(
                        select(func.count()).select_from(t.inventory_count_line).where(
                            t.inventory_count_line.c.tenant_id == self.tenant_id,
                            t.inventory_count_line.c.inventory_count_id == _uuid(count_id),
                        )
                    )
                ).scalar_one()
            )
        return count

    async def create_count(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.inventory_count, values)

    async def update_count(self, count_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.inventory_count, count_id, values)

    async def insert_lines(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for row in rows:
            created.append(await self._insert(t.inventory_count_line, dict(row)))
        return created

    async def list_lines(
        self, count_id: str, *, count_status: str | None = None, item_id: str | None = None,
        bin_id: str | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = [t.inventory_count_line.c.inventory_count_id == _uuid(count_id)]
        if count_status is not None:
            conditions.append(t.inventory_count_line.c.count_status == count_status.upper())
        if item_id is not None:
            conditions.append(t.inventory_count_line.c.item_id == _uuid(item_id))
        if bin_id is not None:
            conditions.append(t.inventory_count_line.c.bin_id == _uuid(bin_id))
        statement = (
            select(t.inventory_count_line)
            .where(t.inventory_count_line.c.tenant_id == self.tenant_id, *conditions)
            .order_by(t.inventory_count_line.c.line_number)
            .limit(max(1, min(limit, 2000)))
        )
        result = await self.session.execute(statement)
        return [_row(dict(row)) for row in result.mappings().all()]

    async def get_line(self, line_id: str) -> dict[str, Any] | None:
        return await self._get(t.inventory_count_line, line_id)

    async def update_line(self, line_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.inventory_count_line, line_id, values)

    async def insert_snapshots(self, rows: Sequence[Mapping[str, Any]]) -> int:
        for row in rows:
            await self.session.execute(insert(t.inventory_count_snapshot).values({**dict(row), "tenant_id": self.tenant_id}))
        await self.session.flush()
        return len(rows)

    async def list_snapshots(self, count_id: str) -> list[dict[str, Any]]:
        statement = (
            select(t.inventory_count_snapshot)
            .where(
                t.inventory_count_snapshot.c.tenant_id == self.tenant_id,
                t.inventory_count_snapshot.c.inventory_count_id == _uuid(count_id),
            )
            .order_by(t.inventory_count_snapshot.c.snapshot_at, t.inventory_count_snapshot.c.item_id)
        )
        result = await self.session.execute(statement)
        return [_row(dict(row)) for row in result.mappings().all()]

    async def insert_recount(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.inventory_count_recount, values)

    async def list_recount(self, count_id: str) -> list[dict[str, Any]]:
        line_ids = [row["id"] for row in await self.list_lines(count_id)]
        if not line_ids:
            return []
        statement = select(t.inventory_count_recount).where(
            t.inventory_count_recount.c.tenant_id == self.tenant_id,
            t.inventory_count_recount.c.inventory_count_line_id.in_([_uuid(line_id) for line_id in line_ids]),
        ).order_by(t.inventory_count_recount.c.counted_at)
        result = await self.session.execute(statement)
        return [_row(dict(row)) for row in result.mappings().all()]

    # ------------------------------------------------------------------ ledger bridge ---------
    async def load_positions(
        self,
        *,
        item_ids: Sequence[str] = (),
        location_ids: Sequence[str] = (),
        status_ids: Sequence[str] = (),
        include_bins: bool = False,
    ) -> list[CountPosition]:
        """Read on-hand positions through the FS-04 balances read contract and enrich item meta."""
        from app.modules.inventory_ledger.facade import BalanceReadPort

        port = BalanceReadPort(self.session, self.tenant_id)
        positions: dict[tuple[Any, ...], CountPosition] = {}
        for item_id in item_ids or (None,):
            for location_id in location_ids or (None,):
                response = await port.balances(item_id=item_id, location_id=location_id, limit=500)
                for raw in response.get("items") or []:
                    if status_ids and str(raw["inventory_status_id"]) not in {str(s) for s in status_ids}:
                        continue
                    qty = q12(raw.get("qty_on_hand", 0))
                    if qty == 0 and not include_bins:
                        # zero positions are only returned to callers that opt in
                        pass
                    meta = await self._item_meta(str(raw["item_id"]))
                    position = CountPosition(
                        item_id=str(raw["item_id"]),
                        location_id=str(raw["location_id"]),
                        inventory_status_id=str(raw["inventory_status_id"]),
                        uom_id=str(meta.get("base_uom_id")) if meta.get("base_uom_id") else str(raw.get("uom_id") or ""),
                        qty_on_hand=qty,
                        value_base=Decimal(str(raw.get("value_base") or 0)),
                        ledger_sequence=int(raw["last_ledger_sequence"]) if raw.get("last_ledger_sequence") is not None else None,
                        bin_id=str(raw["bin_id"]) if raw.get("bin_id") else None,
                        lot_id=str(raw["lot_id"]) if raw.get("lot_id") else None,
                        serial_id=str(raw["serial_id"]) if raw.get("serial_id") else None,
                        ownership=str(raw.get("ownership") or "OWNED"),
                        item_category_id=str(meta.get("item_category_id")) if meta.get("item_category_id") else None,
                        abc_class=meta.get("abc_class"),
                    )
                    positions[position.key] = position
        if include_bins:
            for item_id in item_ids or (None,):
                for location_id in location_ids or (None,):
                    response = await port.by_bin(item_id=item_id, location_id=location_id)
                    for raw in response.get("items") or []:
                        if status_ids and str(raw["inventory_status_id"]) not in {str(s) for s in status_ids}:
                            continue
                        meta = await self._item_meta(str(raw["item_id"]))
                        position = CountPosition(
                            item_id=str(raw["item_id"]),
                            location_id=str(raw["location_id"]),
                            inventory_status_id=str(raw["inventory_status_id"]),
                            uom_id=str(meta.get("base_uom_id")) if meta.get("base_uom_id") else "",
                            qty_on_hand=q12(raw.get("qty_on_hand", 0)),
                            bin_id=str(raw["bin_id"]),
                            lot_id=str(raw["lot_id"]) if raw.get("lot_id") else None,
                            serial_id=str(raw["serial_id"]) if raw.get("serial_id") else None,
                            ownership=str(raw.get("ownership") or "OWNED"),
                            item_category_id=str(meta.get("item_category_id")) if meta.get("item_category_id") else None,
                        )
                        positions[position.key] = position
        return sorted(positions.values(), key=lambda position: tuple("" if value is None else value for value in position.key))

    async def _item_meta(self, item_id: str) -> dict[str, Any]:
        result = await self.session.execute(
            text(
                "SELECT i.base_uom_id, i.item_category_id, ii.costing_method, ii.allow_negative_inventory "
                "FROM item_master.item i LEFT JOIN item_master.item_inventory ii "
                "ON ii.tenant_id = i.tenant_id AND ii.item_id = i.id "
                "WHERE i.tenant_id = :tenant_id AND i.id = :item_id"
            ),
            {"tenant_id": self.tenant_id, "item_id": _uuid(item_id)},
        )
        mapping = result.mappings().first()
        return dict(mapping) if mapping else {}

    async def item_costing_methods(self, item_ids: Sequence[str]) -> dict[str, str]:
        if not item_ids:
            return {}
        rows = await self.session.execute(
            text(
                "SELECT ii.item_id, ii.costing_method FROM item_master.item_inventory ii "
                "WHERE ii.tenant_id = :tenant_id AND ii.item_id = ANY(:item_ids)"
            ),
            {"tenant_id": self.tenant_id, "item_ids": [_uuid(item_id) for item_id in item_ids]},
        )
        return {str(row[0]): str(row[1]) for row in rows}

    async def reason_code_id(self, code: str = "CYCLE_COUNT") -> str | None:
        result = await self.session.execute(
            text(
                "SELECT id FROM inventory_ledger.reason_code "
                "WHERE tenant_id = :tenant_id AND reason_type = 'COUNT' AND code = :code LIMIT 1"
            ),
            {"tenant_id": self.tenant_id, "code": code},
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None

    async def last_posted_count(self, *, item_id: str, location_id: str | None = None) -> date | None:
        clauses = ["c.tenant_id = :tenant_id", "c.status = 'POSTED'", "l.item_id = :item_id"]
        params: dict[str, Any] = {"tenant_id": self.tenant_id, "item_id": _uuid(item_id)}
        if location_id:
            clauses.append("l.location_id = :location_id")
            params["location_id"] = _uuid(location_id)
        result = await self.session.execute(
            text(
                "SELECT max(c.count_date) FROM counts.inventory_count c "
                "JOIN counts.inventory_count_line l ON l.tenant_id = c.tenant_id AND l.inventory_count_id = c.id "
                "WHERE " + " AND ".join(clauses)
            ),
            params,
        )
        value = result.scalar_one_or_none()
        return value if isinstance(value, date) else None

    async def post_count_variance(self, count: Mapping[str, Any], lines: Sequence[Mapping[str, Any]], settings: CountSettings) -> dict[str, Any]:
        from app.modules.inventory_ledger.facade import PostingPort

        reason_code_id = await self.reason_code_id()
        command = build_t20_command(
            count=count, lines=lines, reason_code_id=reason_code_id,
            idempotency_key=f"count:{count['id']}:post",
        )
        port = PostingPort(self.session, self.tenant_id)
        result = await port.post_count_variance(
            transaction_date=count["count_date"],
            source_doc_type="INVENTORY_COUNT",
            source_doc_id=str(count["id"]),
            external_ref=str(count["count_number"]),
            idempotency_key=command["idempotency_key"],
            movements=command["movements"],
        )
        return result

    async def emit(self, event_type: str, *, aggregate_id: str, payload: Mapping[str, Any], aggregate_type: str = "inventory_count") -> None:
        await emit_outbox(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=dict(payload),
        )

    async def document_links(self, count_id: str, transaction_id: str) -> None:
        from app.core.models import platform as platform_models

        table = platform_models.metadata.tables["platform.document_link"]
        await self.session.execute(
            insert(table).values(
                id=uuid4(), tenant_id=self.tenant_id,
                link_type="INVENTORY_COUNT_VARIANCE",
                from_type="INVENTORY_COUNT", from_id=_uuid(count_id),
                to_type="INVENTORY_TRANSACTION", to_id=_uuid(transaction_id),
                version=1,
            )
        )


__all__ = ["CountsRepository"]
