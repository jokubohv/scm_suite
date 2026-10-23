"""Async SQLAlchemy Core repository for FS-12 quality management (10 tables + platform lookups)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.quality import tables as q

__all__ = ["QualityRepository", "utc_now"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    return value


def _row(row: Any) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in dict(row).items()}


class QualityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ platform -------------
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
                diff={key: _jsonable(value) for key, value in (diff or {}).items()},
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
            aggregate_type="quality",
            aggregate_id=str(aggregate_id),
            payload={**{key: _jsonable(value) for key, value in payload.items()}, "tenant_id": str(tenant_id)},
        )

    async def domain_enabled(self, tenant_id: UUID | str) -> bool:
        table = platform_models.metadata.tables["platform.feature_flag"]
        return bool(
            await self.session.scalar(
                select(table.c.is_enabled).where(
                    table.c.tenant_id == tenant_id, table.c.flag_key == "quality.management.enabled"
                )
            )
        )

    async def get_setting(self, tenant_id: UUID | str, key: str) -> Any:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        return await self.session.scalar(
            select(table.c.value).where(
                table.c.tenant_id == tenant_id, table.c.namespace == "quality", table.c.key == key
            )
        )

    async def set_setting(self, tenant_id: UUID | str, key: str, value: Any, value_type: str = "json") -> None:
        table = platform_models.metadata.tables["platform.tenant_setting"]
        existing = await self.session.scalar(
            select(table.c.id).where(table.c.tenant_id == tenant_id, table.c.namespace == "quality", table.c.key == key)
        )
        if existing is None:
            await self.session.execute(
                table.insert().values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    namespace="quality",
                    key=key,
                    value=value,
                    value_type=value_type,
                    is_default=False,
                )
            )
        else:
            await self.session.execute(
                update(table)
                .where(table.c.id == existing)
                .values(value=value, value_type=value_type, updated_at=utc_now())
            )

    async def next_number(self, tenant_id: UUID | str, doc_type: str) -> str:
        table = platform_models.metadata.tables["platform.number_sequence"]
        row = (
            (
                await self.session.execute(
                    update(table)
                    .where(table.c.tenant_id == tenant_id, table.c.doc_type == doc_type)
                    .values(next_value=table.c.next_value + 1, updated_at=utc_now())
                    .returning(table.c.prefix, table.c.padding, table.c.next_value)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeError(f"missing number sequence {doc_type!r} for tenant {tenant_id}")
        number = int(row["next_value"]) - 1
        return f"{row['prefix']}{number:0{int(row['padding'])}d}"

    async def attachment(self, tenant_id: UUID | str, attachment_id: UUID | str) -> dict[str, Any] | None:
        table = platform_models.metadata.tables["platform.attachment_metadata"]
        row = (
            (
                await self.session.execute(
                    select(table).where(table.c.tenant_id == tenant_id, table.c.id == attachment_id)
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def link_document(
        self,
        *,
        tenant_id: UUID | str,
        link_type: str,
        from_type: str,
        from_id: UUID | str,
        to_type: str,
        to_id: UUID | str,
        qty: Decimal | None = None,
    ) -> None:
        table = platform_models.metadata.tables["platform.document_link"]
        await self.session.execute(
            table.insert().values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                link_type=link_type,
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                qty=qty,
            )
        )

    async def links(self, tenant_id: UUID | str, *, from_type: str, from_id: UUID | str) -> list[dict[str, Any]]:
        table = platform_models.metadata.tables["platform.document_link"]
        rows = (
            (
                await self.session.execute(
                    select(table)
                    .where(table.c.tenant_id == tenant_id, table.c.from_type == from_type, table.c.from_id == from_id)
                    .order_by(table.c.created_at)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    # ------------------------------------------------------------------ specs ---------------
    async def insert_spec(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(q.quality_spec.insert().values(**dict(values)).returning(*q.quality_spec.c))
        return _row(result.mappings().one())

    async def insert_spec_lines(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            await self.session.execute(q.quality_spec_line.insert(), [dict(row) for row in rows])

    async def get_spec(self, tenant_id: UUID | str, spec_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.quality_spec).where(
                        q.quality_spec.c.tenant_id == tenant_id, q.quality_spec.c.id == spec_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def spec_lines(self, tenant_id: UUID | str, spec_id: UUID | str) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(q.quality_spec_line)
                    .where(
                        q.quality_spec_line.c.tenant_id == tenant_id, q.quality_spec_line.c.quality_spec_id == spec_id
                    )
                    .order_by(q.quality_spec_line.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def list_specs(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(q.quality_spec).where(q.quality_spec.c.tenant_id == tenant_id)
        if item_id is not None:
            stmt = stmt.where(q.quality_spec.c.item_id == item_id)
        if status:
            stmt = stmt.where(q.quality_spec.c.status == status)
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(q.quality_spec.c.code, q.quality_spec.c.spec_version).limit(limit).offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def update_spec(
        self, tenant_id: UUID | str, spec_id: UUID | str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(q.quality_spec)
            .where(q.quality_spec.c.tenant_id == tenant_id, q.quality_spec.c.id == spec_id)
            .values(**dict(values), updated_at=utc_now(), version=q.quality_spec.c.version + 1)
            .returning(*q.quality_spec.c)
        )
        row = result.mappings().first()
        return _row(row) if row else None

    async def spec_max_version(self, tenant_id: UUID | str, code: str) -> int:
        return int(
            await self.session.scalar(
                select(func.coalesce(func.max(q.quality_spec.c.spec_version), 0)).where(
                    q.quality_spec.c.tenant_id == tenant_id, q.quality_spec.c.code == code
                )
            )
            or 0
        )

    async def spec_referenced_by_active_plan(self, tenant_id: UUID | str, spec_id: UUID | str) -> bool:
        return bool(
            await self.session.scalar(
                select(func.count())
                .select_from(q.inspection_plan)
                .where(
                    q.inspection_plan.c.tenant_id == tenant_id,
                    q.inspection_plan.c.quality_spec_id == spec_id,
                    q.inspection_plan.c.is_active.is_(True),
                )
            )
        )

    # ------------------------------------------------------------------ plans ---------------
    async def insert_plan(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(
            q.inspection_plan.insert().values(**dict(values)).returning(*q.inspection_plan.c)
        )
        return _row(result.mappings().one())

    async def insert_plan_lines(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            await self.session.execute(q.inspection_plan_line.insert(), [dict(row) for row in rows])

    async def get_plan(self, tenant_id: UUID | str, plan_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection_plan).where(
                        q.inspection_plan.c.tenant_id == tenant_id, q.inspection_plan.c.id == plan_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def plan_lines(self, tenant_id: UUID | str, plan_id: UUID | str) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(q.inspection_plan_line)
                    .where(
                        q.inspection_plan_line.c.tenant_id == tenant_id,
                        q.inspection_plan_line.c.inspection_plan_id == plan_id,
                    )
                    .order_by(q.inspection_plan_line.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def list_plans(
        self, tenant_id: UUID | str, *, item_id: UUID | str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        stmt = select(q.inspection_plan).where(q.inspection_plan.c.tenant_id == tenant_id)
        if item_id is not None:
            stmt = stmt.where(q.inspection_plan.c.item_id == item_id)
        rows = (
            (await self.session.execute(stmt.order_by(q.inspection_plan.c.code).limit(limit).offset(offset)))
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def update_plan(
        self, tenant_id: UUID | str, plan_id: UUID | str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(q.inspection_plan)
            .where(q.inspection_plan.c.tenant_id == tenant_id, q.inspection_plan.c.id == plan_id)
            .values(**dict(values), updated_at=utc_now(), version=q.inspection_plan.c.version + 1)
            .returning(*q.inspection_plan.c)
        )
        row = result.mappings().first()
        return _row(row) if row else None

    # ------------------------------------------------------------------ queues --------------
    async def insert_queue(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(
            q.inspection_queue.insert().values(**dict(values)).returning(*q.inspection_queue.c)
        )
        return _row(result.mappings().one())

    async def get_queue(self, tenant_id: UUID | str, queue_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection_queue).where(
                        q.inspection_queue.c.tenant_id == tenant_id, q.inspection_queue.c.id == queue_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def get_queue_by_code(self, tenant_id: UUID | str, code: str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection_queue).where(
                        q.inspection_queue.c.tenant_id == tenant_id, q.inspection_queue.c.code == code
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def list_queues(self, tenant_id: UUID | str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(q.inspection_queue)
                    .where(q.inspection_queue.c.tenant_id == tenant_id)
                    .order_by(q.inspection_queue.c.code)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    # ------------------------------------------------------------------ triggers -------------
    async def insert_trigger(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(
            q.inspection_trigger.insert().values(**dict(values)).returning(*q.inspection_trigger.c)
        )
        return _row(result.mappings().one())

    async def get_trigger(self, tenant_id: UUID | str, trigger_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection_trigger).where(
                        q.inspection_trigger.c.tenant_id == tenant_id, q.inspection_trigger.c.id == trigger_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def list_triggers(
        self,
        tenant_id: UUID | str,
        *,
        trigger_event: str | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(q.inspection_trigger).where(q.inspection_trigger.c.tenant_id == tenant_id)
        if trigger_event:
            stmt = stmt.where(q.inspection_trigger.c.trigger_event == trigger_event)
        if active_only:
            stmt = stmt.where(q.inspection_trigger.c.is_active.is_(True))
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(q.inspection_trigger.c.priority, q.inspection_trigger.c.code)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def update_trigger(
        self, tenant_id: UUID | str, trigger_id: UUID | str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(q.inspection_trigger)
            .where(q.inspection_trigger.c.tenant_id == tenant_id, q.inspection_trigger.c.id == trigger_id)
            .values(**dict(values), updated_at=utc_now(), version=q.inspection_trigger.c.version + 1)
            .returning(*q.inspection_trigger.c)
        )
        row = result.mappings().first()
        return _row(row) if row else None

    async def reset_trigger_counters(self, tenant_id: UUID | str, trigger_id: UUID | str | None = None) -> int:
        stmt = update(q.inspection_trigger).where(q.inspection_trigger.c.tenant_id == tenant_id)
        if trigger_id is not None:
            stmt = stmt.where(q.inspection_trigger.c.id == trigger_id)
        result = await self.session.execute(
            stmt.values(evaluation_counter=0, counter_reset_at=utc_now(), updated_at=utc_now())
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def bump_trigger_counter(self, tenant_id: UUID | str, trigger_id: UUID | str) -> int:
        value = await self.session.scalar(
            update(q.inspection_trigger)
            .where(q.inspection_trigger.c.tenant_id == tenant_id, q.inspection_trigger.c.id == trigger_id)
            .values(evaluation_counter=q.inspection_trigger.c.evaluation_counter + 1)
            .returning(q.inspection_trigger.c.evaluation_counter)
        )
        return int(value or 0)

    # ------------------------------------------------------------------ inspections ---------
    async def insert_inspection(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(q.inspection.insert().values(**dict(values)).returning(*q.inspection.c))
        return _row(result.mappings().one())

    async def get_inspection(self, tenant_id: UUID | str, inspection_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection).where(
                        q.inspection.c.tenant_id == tenant_id, q.inspection.c.id == inspection_id
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def inspection_for_trigger_source(
        self, tenant_id: UUID | str, *, trigger_id: UUID | str, source_entity_type: str, source_entity_id: UUID | str
    ) -> dict[str, Any] | None:
        row = (
            (
                await self.session.execute(
                    select(q.inspection).where(
                        q.inspection.c.tenant_id == tenant_id,
                        q.inspection.c.trigger_id == trigger_id,
                        q.inspection.c.source_entity_type == source_entity_type,
                        q.inspection.c.source_entity_id == source_entity_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def list_inspections(
        self,
        tenant_id: UUID | str,
        *,
        status: str | None = None,
        item_id: UUID | str | None = None,
        source_entity_type: str | None = None,
        queue_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(q.inspection).where(q.inspection.c.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(q.inspection.c.status == status)
        if item_id is not None:
            stmt = stmt.where(q.inspection.c.item_id == item_id)
        if source_entity_type:
            stmt = stmt.where(q.inspection.c.source_entity_type == source_entity_type)
        if queue_id is not None:
            stmt = stmt.where(q.inspection.c.inspection_queue_id == queue_id)
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(q.inspection.c.created_at.desc(), q.inspection.c.inspection_number)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def update_inspection(
        self, tenant_id: UUID | str, inspection_id: UUID | str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(q.inspection)
            .where(q.inspection.c.tenant_id == tenant_id, q.inspection.c.id == inspection_id)
            .values(**dict(values), updated_at=utc_now(), version=q.inspection.c.version + 1)
            .returning(*q.inspection.c)
        )
        row = result.mappings().first()
        return _row(row) if row else None

    async def replace_results(
        self, tenant_id: UUID | str, inspection_id: UUID | str, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        await self.session.execute(
            q.inspection_result.delete().where(
                q.inspection_result.c.tenant_id == tenant_id, q.inspection_result.c.inspection_id == inspection_id
            )
        )
        if rows:
            await self.session.execute(
                q.inspection_result.insert(),
                [{**dict(row), "tenant_id": tenant_id, "inspection_id": inspection_id} for row in rows],
            )

    async def list_results(self, tenant_id: UUID | str, inspection_id: UUID | str) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(q.inspection_result)
                    .where(
                        q.inspection_result.c.tenant_id == tenant_id,
                        q.inspection_result.c.inspection_id == inspection_id,
                    )
                    .order_by(q.inspection_result.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    # ------------------------------------------------------------------ COA -----------------
    async def insert_coa(self, values: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.execute(q.coa.insert().values(**dict(values)).returning(*q.coa.c))
        return _row(result.mappings().one())

    async def insert_coa_lines(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            await self.session.execute(q.coa_line.insert(), [dict(row) for row in rows])

    async def get_coa(self, tenant_id: UUID | str, coa_id: UUID | str) -> dict[str, Any] | None:
        row = (
            (await self.session.execute(select(q.coa).where(q.coa.c.tenant_id == tenant_id, q.coa.c.id == coa_id)))
            .mappings()
            .first()
        )
        return _row(row) if row else None

    async def coa_lines(self, tenant_id: UUID | str, coa_id: UUID | str) -> list[dict[str, Any]]:
        rows = (
            (
                await self.session.execute(
                    select(q.coa_line)
                    .where(q.coa_line.c.tenant_id == tenant_id, q.coa_line.c.coa_id == coa_id)
                    .order_by(q.coa_line.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def list_coas(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        stmt = select(q.coa).where(q.coa.c.tenant_id == tenant_id)
        if item_id is not None:
            stmt = stmt.where(q.coa.c.item_id == item_id)
        if status:
            stmt = stmt.where(q.coa.c.status == status)
        rows = (
            (await self.session.execute(stmt.order_by(q.coa.c.created_at.desc()).limit(limit).offset(offset)))
            .mappings()
            .all()
        )
        return [_row(row) for row in rows]

    async def update_coa(
        self, tenant_id: UUID | str, coa_id: UUID | str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            update(q.coa)
            .where(q.coa.c.tenant_id == tenant_id, q.coa.c.id == coa_id)
            .values(**dict(values), updated_at=utc_now(), version=q.coa.c.version + 1)
            .returning(*q.coa.c)
        )
        row = result.mappings().first()
        return _row(row) if row else None

    # ------------------------------------------------------------------ hold bridge ----------
    async def hold_status(self, tenant_id: UUID | str, code: str) -> dict[str, Any] | None:
        table = text(
            "SELECT id::text AS id, code, is_committable FROM traceability.inventory_status "
            "WHERE tenant_id = :tenant AND code = :code"
        )
        row = (await self.session.execute(table, {"tenant": tenant_id, "code": code})).mappings().first()
        return dict(row) if row else None

    async def balance_position(
        self,
        tenant_id: UUID | str,
        *,
        item_id: UUID | str,
        location_id: UUID | str,
        inventory_status_id: UUID | str,
        lot_id: UUID | str | None = None,
        serial_id: UUID | str | None = None,
    ) -> dict[str, Any] | None:
        table = text(
            """
            SELECT qty_on_hand::text AS qty_on_hand, qty_committed::text AS qty_committed
              FROM inventory_ledger.inventory_balance
             WHERE tenant_id = :tenant AND item_id = :item AND location_id = :location
               AND inventory_status_id = :status
               AND lot_id IS NOT DISTINCT FROM :lot AND serial_id IS NOT DISTINCT FROM :serial
            """
        )
        row = (
            (
                await self.session.execute(
                    table,
                    {
                        "tenant": tenant_id,
                        "item": item_id,
                        "location": location_id,
                        "status": inventory_status_id,
                        "lot": lot_id,
                        "serial": serial_id,
                    },
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None
