"""FS-05 costing persistence (async SQLAlchemy Core; no ORM cross-imports).

Owns every write to the ``costing`` tables. FS-04 calls :class:`CostingWriter` in step 13 of its posting
transaction so cost artefacts commit atomically with the ledger; the API service calls the higher-level
repository helpers. No commits here — callers own the transaction boundary.

Read helpers return JSON-safe dicts (UUID/date/Decimal serialised) for the API/contract layers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox import emit as emit_outbox
from app.modules.costing import tables as t
from app.modules.costing.engine import (
    ItemCostState,
    LayerState,
    ValueResult,
)
from app.modules.costing.errors import CostingNotFound, CostingValidationError

TWELVE = Decimal("0.000000000001")


def _q(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value)).quantize(TWELVE)


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


def _one_or_none(mapping: Any) -> dict[str, Any] | None:
    return _row(dict(mapping)) if mapping is not None else None


class CostingRepository:
    """Read/write repository for the 25 costing tables (tenant-scoped)."""

    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = UUID(str(tenant_id))

    # ------------------------------------------------------------------ generic helpers ------
    async def _select(self, table: Any, *conditions: Any, limit: int | None = None) -> list[dict[str, Any]]:
        statement = select(table).where(table.c.tenant_id == self.tenant_id, *conditions)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return [_row(dict(mapping)) for mapping in result.mappings().all()]

    async def _get(self, table: Any, row_id: UUID | str) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(table).where(table.c.tenant_id == self.tenant_id, table.c.id == UUID(str(row_id)))
        )
        mapping = result.mappings().first()
        return _one_or_none(mapping)

    async def _insert(self, table: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        row_id = uuid4()
        payload = {"id": row_id, "tenant_id": self.tenant_id, **dict(values)}
        await self.session.execute(insert(table).values(**payload))
        await self.session.flush()
        row = await self._get(table, row_id)
        if row is None:  # pragma: no cover - insert/read race only
            raise CostingValidationError("inserted row could not be read back")
        return row

    async def _update(self, table: Any, row_id: UUID | str, values: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        if "version" in table.c and "version" not in payload:
            payload["version"] = table.c.version + 1
        result = await self.session.execute(
            update(table)
            .where(table.c.tenant_id == self.tenant_id, table.c.id == UUID(str(row_id)))
            .values(**payload)
            .returning(table)
        )
        mapping = result.mappings().first()
        if mapping is None:
            raise CostingNotFound(f"{table.name} row not found", code=f"{table.name.upper()}_NOT_FOUND")
        return _row(dict(mapping))

    async def _count(self, table: Any, *conditions: Any) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(table).where(table.c.tenant_id == self.tenant_id, *conditions)
        )
        return int(result.scalar_one())

    # ------------------------------------------------------------- item costs / layers ------
    async def list_item_costs(
        self,
        *,
        item_id: str | None = None,
        location_id: str | None = None,
        location_costing_group_id: str | None = None,
        lot_id: str | None = None,
        serial_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions = []
        for column, value in (
            (t.item_cost.c.item_id, item_id),
            (t.item_cost.c.location_id, location_id),
            (t.item_cost.c.location_costing_group_id, location_costing_group_id),
            (t.item_cost.c.lot_id, lot_id),
            (t.item_cost.c.serial_id, serial_id),
        ):
            if value is not None:
                conditions.append(column == UUID(str(value)))
        if status is not None:
            conditions.append(t.item_cost.c.status == status.upper())
        return await self._select(t.item_cost, *conditions, limit=max(1, min(limit, 500)))

    async def get_item_cost(self, item_cost_id: str) -> dict[str, Any] | None:
        row = await self._get(t.item_cost, item_cost_id)
        if row is None:
            return None
        row["layer_count"] = await self._count(t.cost_layer, t.cost_layer.c.item_cost_id == UUID(item_cost_id))
        row["ledger_entry_count"] = await self._count(
            t.cost_ledger_entry, t.cost_ledger_entry.c.item_cost_id == UUID(item_cost_id)
        )
        return row

    async def item_cost_state(self, item_cost_id: str) -> tuple[ItemCostState, list[LayerState]]:
        row = await self._get(t.item_cost, item_cost_id)
        if row is None:
            raise CostingNotFound("item cost not found", code="ITEM_COST_NOT_FOUND")
        state = ItemCostState(
            qty_on_hand=Decimal(row["qty_on_hand"]),
            value_base=Decimal(row["value_base"]),
            avg_cost=Decimal(row["avg_cost"]),
            last_purchase_price=(
                None if row["last_purchase_price"] is None else Decimal(row["last_purchase_price"])
            ),
            status=row["status"],
            cost_provisional=bool(row["cost_provisional"]),
            last_cost_ledger_seq=row["last_cost_ledger_seq"],
            item_cost_id=str(row["id"]),
        )
        layers = [
            LayerState(
                layer_id=str(layer["id"]),
                layer_seq=int(layer["layer_seq"]),
                layer_date=date.fromisoformat(layer["layer_date"]),
                qty_original=Decimal(layer["qty_original"]),
                qty_remaining=Decimal(layer["qty_remaining"]),
                unit_cost=Decimal(layer["unit_cost"]),
                value_remaining=Decimal(layer["value_remaining"]),
                status=layer["status"],
                estimated=bool(layer["estimated"]),
                derived_from_layer_id=layer["derived_from_layer_id"],
            )
            for layer in await self.list_layers(item_cost_id)
        ]
        return state, layers

    async def get_or_create_item_cost(
        self,
        *,
        item_id: str,
        costing_method: str,
        location_id: str | None = None,
        location_costing_group_id: str | None = None,
        lot_id: str | None = None,
        serial_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the cost object for a posting, inserting a zero CURRENT row when absent."""
        conditions = [
            t.item_cost.c.item_id == UUID(str(item_id)),
            t.item_cost.c.location_id.is_(None) if location_id is None else t.item_cost.c.location_id == UUID(str(location_id)),
            t.item_cost.c.location_costing_group_id.is_(None)
            if location_costing_group_id is None
            else t.item_cost.c.location_costing_group_id == UUID(str(location_costing_group_id)),
            t.item_cost.c.lot_id.is_(None) if lot_id is None else t.item_cost.c.lot_id == UUID(str(lot_id)),
            t.item_cost.c.serial_id.is_(None) if serial_id is None else t.item_cost.c.serial_id == UUID(str(serial_id)),
        ]
        statement = select(t.item_cost).where(t.item_cost.c.tenant_id == self.tenant_id, *conditions)
        mapping = (await self.session.execute(statement)).mappings().first()
        if mapping is not None:
            return _row(dict(mapping))
        return await self._insert(
            t.item_cost,
            {
                "item_id": UUID(str(item_id)),
                "location_id": UUID(str(location_id)) if location_id else None,
                "location_costing_group_id": UUID(str(location_costing_group_id)) if location_costing_group_id else None,
                "lot_id": UUID(str(lot_id)) if lot_id else None,
                "serial_id": UUID(str(serial_id)) if serial_id else None,
                "costing_method": str(costing_method).upper(),
                "qty_on_hand": Decimal("0"),
                "value_base": Decimal("0"),
                "avg_cost": Decimal("0"),
                "status": "CURRENT",
                "cost_provisional": False,
            },
        )

    async def list_layers(
        self, item_cost_id: str, *, status: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        conditions = [t.cost_layer.c.item_cost_id == UUID(str(item_cost_id))]
        if status is not None:
            conditions.append(t.cost_layer.c.status == status.upper())
        statement = (
            select(t.cost_layer)
            .where(t.cost_layer.c.tenant_id == self.tenant_id, *conditions)
            .order_by(t.cost_layer.c.layer_date, t.cost_layer.c.layer_seq)
            .limit(max(1, min(limit, 1000)))
        )
        result = await self.session.execute(statement)
        return [_row(dict(mapping)) for mapping in result.mappings().all()]

    async def consumption_trail(self, layer_id: str) -> list[dict[str, Any]]:
        statement = select(t.cost_layer_consumption).where(
            t.cost_layer_consumption.c.tenant_id == self.tenant_id,
            t.cost_layer_consumption.c.cost_layer_id == UUID(str(layer_id)),
        ).order_by(t.cost_layer_consumption.c.consumption_seq)
        result = await self.session.execute(statement)
        return [_row(dict(mapping)) for mapping in result.mappings().all()]

    async def valuation_summary(self, *, item_id: str | None = None, location_id: str | None = None) -> dict[str, Any]:
        conditions = []
        if item_id is not None:
            conditions.append(t.item_cost.c.item_id == UUID(str(item_id)))
        if location_id is not None:
            conditions.append(t.item_cost.c.location_id == UUID(str(location_id)))
        rows = await self._select(t.item_cost, *conditions)
        items = [
            {
                "item_id": row["item_id"],
                "location_id": row["location_id"],
                "location_costing_group_id": row["location_costing_group_id"],
                "qty": row["qty_on_hand"],
                "value_base": row["value_base"],
                "avg_or_standard_cost": row["avg_cost"],
                "method": row["costing_method"],
                "status": row["status"],
            }
            for row in rows
        ]
        total_qty = _q(sum((Decimal(item["qty"]) for item in items), Decimal("0")))
        total_value = _q(sum((Decimal(item["value_base"]) for item in items), Decimal("0")))
        return {"items": items, "totals": {"qty": format(total_qty, "f"), "value_base": format(total_value, "f")}}

    async def valuation_by_layer(self, *, item_id: str | None = None, location_id: str | None = None) -> dict[str, Any]:
        conditions = []
        if item_id is not None:
            conditions.append(t.item_cost.c.item_id == UUID(str(item_id)))
        if location_id is not None:
            conditions.append(t.item_cost.c.location_id == UUID(str(location_id)))
        costs = await self._select(t.item_cost, *conditions)
        items: list[dict[str, Any]] = []
        for cost in costs:
            for layer in await self.list_layers(cost["id"]):
                items.append(
                    {
                        "item_id": cost["item_id"],
                        "location_id": cost["location_id"],
                        "layer_id": layer["id"],
                        "layer_seq": layer["layer_seq"],
                        "layer_date": layer["layer_date"],
                        "qty_remaining": layer["qty_remaining"],
                        "unit_cost": layer["unit_cost"],
                        "value_remaining": layer["value_remaining"],
                        "status": layer["status"],
                    }
                )
        return {"items": items}

    async def pending_recalculations(self, *, item_id: str | None = None) -> dict[str, Any]:
        conditions = [t.item_cost.c.status == "PENDING_RECALC"]
        if item_id is not None:
            conditions.append(t.item_cost.c.item_id == UUID(str(item_id)))
        rows = await self._select(t.item_cost, *conditions)
        return {"items": rows, "counts": {"PENDING_RECALC": len(rows)}}

    # ------------------------------------------------------------------ categories ----------
    async def list_categories(self, *, active: bool | None = None) -> list[dict[str, Any]]:
        conditions = []
        if active is not None:
            conditions.append(t.cost_category.c.is_active.is_(active))
        return await self._select(t.cost_category, *conditions)

    async def get_category(self, category_id: str) -> dict[str, Any] | None:
        return await self._get(t.cost_category, category_id)

    async def create_category(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.cost_category, values)

    async def update_category(self, category_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cost_category, category_id, values)

    # ------------------------------------------------------------------ templates -----------
    async def list_templates(self, *, inactive: bool | None = None) -> list[dict[str, Any]]:
        conditions = []
        if inactive is not None:
            conditions.append(t.cost_template.c.is_inactive.is_(inactive))
        return await self._select(t.cost_template, *conditions)

    async def get_template(self, template_id: str) -> dict[str, Any] | None:
        template = await self._get(t.cost_template, template_id)
        if template is not None:
            template["lines"] = await self._select(
                t.cost_template_line, t.cost_template_line.c.cost_template_id == UUID(str(template_id))
            )
        return template

    async def create_template(self, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        template = await self._insert(t.cost_template, values)
        for line in lines:
            await self._insert(t.cost_template_line, {"cost_template_id": template["id"], **dict(line)})
        return await self.get_template(template["id"]) or template

    async def update_template(self, template_id: str, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        await self._update(t.cost_template, template_id, values)
        if lines is not None:
            await self.session.execute(
                delete(t.cost_template_line).where(
                    t.cost_template_line.c.tenant_id == self.tenant_id,
                    t.cost_template_line.c.cost_template_id == UUID(str(template_id)),
                )
            )
            for line in lines:
                await self._insert(t.cost_template_line, {"cost_template_id": template_id, **dict(line)})
        return await self.get_template(template_id) or {}

    # ------------------------------------------------------------------ versions ------------
    async def list_versions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.cost_version.c.status == status.upper())
        return await self._select(t.cost_version, *conditions)

    async def get_version(self, version_id: str) -> dict[str, Any] | None:
        return await self._get(t.cost_version, version_id)

    async def create_version(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.cost_version, values)

    async def update_version(self, version_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cost_version, version_id, values)

    async def list_planned_standards(self, version_id: str, *, item_id: str | None = None, approved: bool | None = None) -> list[dict[str, Any]]:
        conditions = [t.planned_standard_cost.c.cost_version_id == UUID(str(version_id))]
        if item_id is not None:
            conditions.append(t.planned_standard_cost.c.item_id == UUID(str(item_id)))
        if approved is not None:
            conditions.append(t.planned_standard_cost.c.is_approved.is_(approved))
        return await self._select(t.planned_standard_cost, *conditions)

    async def upsert_planned_standard(self, version_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        existing = await self.session.execute(
            select(t.planned_standard_cost).where(
                t.planned_standard_cost.c.tenant_id == self.tenant_id,
                t.planned_standard_cost.c.cost_version_id == UUID(str(version_id)),
                t.planned_standard_cost.c.item_id == UUID(str(values["item_id"])),
                t.planned_standard_cost.c.cost_category_id == UUID(str(values["cost_category_id"])),
            )
        )
        mapping = existing.mappings().first()
        if mapping is not None:
            return await self._update(t.planned_standard_cost, mapping["id"], {"unit_cost": values["unit_cost"]})
        return await self._insert(t.planned_standard_cost, {"cost_version_id": version_id, **dict(values)})

    async def approve_planned_standards(self, version_id: str, *, item_ids: Sequence[str] | None = None) -> int:
        statement = (
            update(t.planned_standard_cost)
            .where(
                t.planned_standard_cost.c.tenant_id == self.tenant_id,
                t.planned_standard_cost.c.cost_version_id == UUID(str(version_id)),
                t.planned_standard_cost.c.is_approved.is_(False),
            )
            .values(is_approved=True, approved_at=datetime.now(UTC))
        )
        if item_ids:
            statement = statement.where(
                t.planned_standard_cost.c.item_id.in_([UUID(str(item)) for item in item_ids])
            )
        result = await self.session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_standard_costs(self, *, item_id: str | None = None, cost_category_id: str | None = None, as_of: date | None = None) -> list[dict[str, Any]]:
        conditions = []
        if item_id is not None:
            conditions.append(t.standard_cost.c.item_id == UUID(str(item_id)))
        if cost_category_id is not None:
            conditions.append(t.standard_cost.c.cost_category_id == UUID(str(cost_category_id)))
        if as_of is not None:
            conditions.append(t.standard_cost.c.effective_from <= as_of)
            conditions.append((t.standard_cost.c.effective_to.is_(None)) | (t.standard_cost.c.effective_to >= as_of))
        return await self._select(t.standard_cost, *conditions)

    async def create_standard_cost(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.standard_cost, values)

    # ------------------------------------------------------------------ rollups -------------
    async def create_rollup_run(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.cost_rollup_run, values)

    async def list_rollup_runs(self, *, status: str | None = None, cost_version_id: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.cost_rollup_run.c.status == status.upper())
        if cost_version_id is not None:
            conditions.append(t.cost_rollup_run.c.cost_version_id == UUID(str(cost_version_id)))
        return await self._select(t.cost_rollup_run, *conditions)

    async def get_rollup_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._get(t.cost_rollup_run, run_id)

    async def update_rollup_run(self, run_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cost_rollup_run, run_id, values)

    async def list_rollup_lines(self, run_id: str, *, item_id: str | None = None, cost_category_id: str | None = None, bom_level: int | None = None) -> list[dict[str, Any]]:
        conditions = [t.cost_rollup_line.c.cost_rollup_run_id == UUID(str(run_id))]
        if item_id is not None:
            conditions.append(t.cost_rollup_line.c.item_id == UUID(str(item_id)))
        if cost_category_id is not None:
            conditions.append(t.cost_rollup_line.c.cost_category_id == UUID(str(cost_category_id)))
        if bom_level is not None:
            conditions.append(t.cost_rollup_line.c.bom_level == bom_level)
        return await self._select(t.cost_rollup_line, *conditions)

    async def insert_rollup_lines(self, run_id: str, lines: Sequence[Mapping[str, Any]]) -> int:
        for line in lines:
            await self._insert(t.cost_rollup_line, {"cost_rollup_run_id": run_id, **dict(line)})
        return len(lines)

    # ---------------------------------------------------------------- revaluations ----------
    async def create_revaluation(self, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        document = await self._insert(t.cost_revaluation, values)
        for line in lines:
            await self._insert(t.cost_revaluation_line, {"cost_revaluation_id": document["id"], **dict(line)})
        return await self.get_revaluation(document["id"]) or document

    async def list_revaluations(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.cost_revaluation.c.status == status.upper())
        return await self._select(t.cost_revaluation, *conditions)

    async def get_revaluation(self, revaluation_id: str) -> dict[str, Any] | None:
        document = await self._get(t.cost_revaluation, revaluation_id)
        if document is not None:
            document["lines"] = await self._select(
                t.cost_revaluation_line,
                t.cost_revaluation_line.c.cost_revaluation_id == UUID(str(revaluation_id)),
            )
        return document

    async def update_revaluation(self, revaluation_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cost_revaluation, revaluation_id, values)

    # ---------------------------------------------------------------- landed cost ------------
    async def create_landed_cost(self, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        document = await self._insert(t.landed_cost, values)
        for line in lines:
            await self._insert(t.landed_cost_line, {"landed_cost_id": document["id"], **dict(line)})
        return await self.get_landed_cost(document["id"]) or document

    async def list_landed_costs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.landed_cost.c.status == status.upper())
        return await self._select(t.landed_cost, *conditions)

    async def get_landed_cost(self, landed_cost_id: str) -> dict[str, Any] | None:
        document = await self._get(t.landed_cost, landed_cost_id)
        if document is not None:
            document["lines"] = await self._select(
                t.landed_cost_line, t.landed_cost_line.c.landed_cost_id == UUID(str(landed_cost_id))
            )
            document["allocations"] = await self._select(
                t.landed_cost_allocation,
                t.landed_cost_allocation.c.landed_cost_id == UUID(str(landed_cost_id)),
            )
        return document

    async def update_landed_cost(self, landed_cost_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.landed_cost, landed_cost_id, values)

    async def insert_allocations(self, allocations: Sequence[Mapping[str, Any]]) -> int:
        for allocation in allocations:
            await self._insert(t.landed_cost_allocation, dict(allocation))
        return len(allocations)

    async def list_allocations(self, landed_cost_id: str, *, line_id: str | None = None) -> list[dict[str, Any]]:
        conditions = [t.landed_cost_allocation.c.landed_cost_id == UUID(str(landed_cost_id))]
        if line_id is not None:
            conditions.append(t.landed_cost_allocation.c.landed_cost_line_id == UUID(str(line_id)))
        return await self._select(t.landed_cost_allocation, *conditions)

    # ------------------------------------------------------------- ELC templates -------------
    async def list_elc_templates(self) -> list[dict[str, Any]]:
        return await self._select(t.estimated_landed_cost_template)

    async def get_elc_template(self, template_id: str) -> dict[str, Any] | None:
        template = await self._get(t.estimated_landed_cost_template, template_id)
        if template is not None:
            template["lines"] = await self._select(
                t.estimated_landed_cost_template_line,
                t.estimated_landed_cost_template_line.c.template_id == UUID(str(template_id)),
            )
        return template

    async def create_elc_template(self, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        template = await self._insert(t.estimated_landed_cost_template, values)
        for line in lines:
            await self._insert(t.estimated_landed_cost_template_line, {"template_id": template["id"], **dict(line)})
        return await self.get_elc_template(template["id"]) or template

    async def update_elc_template(self, template_id: str, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        await self._update(t.estimated_landed_cost_template, template_id, values)
        if lines is not None:
            await self.session.execute(
                delete(t.estimated_landed_cost_template_line).where(
                    t.estimated_landed_cost_template_line.c.tenant_id == self.tenant_id,
                    t.estimated_landed_cost_template_line.c.template_id == UUID(str(template_id)),
                )
            )
            for line in lines:
                await self._insert(
                    t.estimated_landed_cost_template_line, {"template_id": template_id, **dict(line)}
                )
        return await self.get_elc_template(template_id) or {}

    # ------------------------------------------------------------- COGS adjustment -----------
    async def create_cogs_adjustment(self, values: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        document = await self._insert(t.cogs_adjustment, values)
        for line in lines:
            await self._insert(t.cogs_adjustment_line, {"cogs_adjustment_id": document["id"], **dict(line)})
        return await self.get_cogs_adjustment(document["id"]) or document

    async def list_cogs_adjustments(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status is not None:
            conditions.append(t.cogs_adjustment.c.status == status.upper())
        return await self._select(t.cogs_adjustment, *conditions)

    async def get_cogs_adjustment(self, adjustment_id: str) -> dict[str, Any] | None:
        document = await self._get(t.cogs_adjustment, adjustment_id)
        if document is not None:
            document["lines"] = await self._select(
                t.cogs_adjustment_line,
                t.cogs_adjustment_line.c.cogs_adjustment_id == UUID(str(adjustment_id)),
            )
        return document

    async def update_cogs_adjustment(self, adjustment_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cogs_adjustment, adjustment_id, values)

    # ------------------------------------------------------------------ variances ------------
    async def list_variances(
        self,
        *,
        variance_type: str | None = None,
        item_id: str | None = None,
        fiscal_period_id: str | None = None,
        work_order_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        conditions = []
        for column, value in (
            (t.cost_variance.c.variance_type, variance_type),
            (t.cost_variance.c.item_id, item_id),
            (t.cost_variance.c.fiscal_period_id, fiscal_period_id),
            (t.cost_variance.c.work_order_id, work_order_id),
        ):
            if value is not None:
                conditions.append(column == value if column is t.cost_variance.c.variance_type else column == UUID(str(value)))
        if status is not None:
            conditions.append(t.cost_variance.c.status == status.upper())
        items = await self._select(t.cost_variance, *conditions, limit=max(1, min(limit, 500)))
        total = _q(sum((Decimal(item["amount_base"]) for item in items), Decimal("0")))
        return {"items": items, "totals": {"amount_base": format(total, "f"), "count": len(items)}}

    async def create_variance(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.cost_variance, values)

    async def update_variance(self, variance_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.cost_variance, variance_id, values)

    # ------------------------------------------------------------- costing groups -------------
    async def list_groups(self, *, active: bool | None = None) -> list[dict[str, Any]]:
        conditions = []
        if active is not None:
            conditions.append(t.location_costing_group.c.is_active.is_(active))
        groups = await self._select(t.location_costing_group, *conditions)
        for group in groups:
            group["members"] = await self._select(
                t.location_costing_group_member,
                t.location_costing_group_member.c.location_costing_group_id == UUID(str(group["id"])),
            )
        return groups

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        group = await self._get(t.location_costing_group, group_id)
        if group is not None:
            group["members"] = await self._select(
                t.location_costing_group_member,
                t.location_costing_group_member.c.location_costing_group_id == UUID(str(group_id)),
            )
        return group

    async def create_group(self, values: Mapping[str, Any], member_location_ids: Sequence[str]) -> dict[str, Any]:
        group = await self._insert(t.location_costing_group, values)
        for location_id in member_location_ids:
            await self._insert(
                t.location_costing_group_member,
                {"location_costing_group_id": group["id"], "location_id": location_id},
            )
        return await self.get_group(group["id"]) or group

    async def update_group(self, group_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.location_costing_group, group_id, values)

    async def add_group_member(self, group_id: str, location_id: str) -> dict[str, Any]:
        await self._insert(t.location_costing_group_member, {"location_costing_group_id": group_id, "location_id": location_id})
        return await self.get_group(group_id) or {}

    async def remove_group_member(self, group_id: str, location_id: str) -> dict[str, Any]:
        await self.session.execute(
            delete(t.location_costing_group_member).where(
                t.location_costing_group_member.c.tenant_id == self.tenant_id,
                t.location_costing_group_member.c.location_costing_group_id == UUID(str(group_id)),
                t.location_costing_group_member.c.location_id == UUID(str(location_id)),
            )
        )
        return await self.get_group(group_id) or {}

    async def list_group_average_balances(self, *, item_id: str | None = None, group_id: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if item_id is not None:
            conditions.append(t.group_average_balance.c.item_id == UUID(str(item_id)))
        if group_id is not None:
            conditions.append(t.group_average_balance.c.location_costing_group_id == UUID(str(group_id)))
        return await self._select(t.group_average_balance, *conditions)

    # ------------------------------------------------------------------ writes (posting) ----
    async def load_state(self, item_cost_id: str) -> tuple[ItemCostState, list[LayerState]]:
        return await self.item_cost_state(item_cost_id)

    async def persist_value_result(
        self,
        *,
        item_cost_id: str,
        transaction_id: str | None,
        inventory_transaction_line_id: str | None,
        result: ValueResult,
        effective_date: date,
        fiscal_period_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one valuation result inside the caller's transaction (FS-04 step 13)."""

        state, _layers = await self.item_cost_state(item_cost_id)
        item_cost_row = await self._get(t.item_cost, item_cost_id)
        method = str(item_cost_row["costing_method"]) if item_cost_row else "AVERAGE"
        entry_seq_result = await self.session.execute(
            select(func.coalesce(func.max(t.cost_ledger_entry.c.entry_seq), 0)).where(
                t.cost_ledger_entry.c.tenant_id == self.tenant_id
            )
        )
        entry_seq = int(entry_seq_result.scalar_one()) + 1
        layer_ids: dict[str, str] = {}
        for action in result.layer_actions:
            if action.action == "CREATE":
                values = {
                    "item_cost_id": item_cost_id,
                    "layer_seq": action.layer_seq,
                    "layer_date": action.layer_date or effective_date,
                    "qty_original": action.qty,
                    "qty_remaining": action.qty,
                    "unit_cost": action.unit_cost,
                    "value_remaining": action.value,
                    "status": "OPEN",
                    "estimated": action.estimated,
                    "derived_from_layer_id": action.derived_from_layer_id,
                }
                row = await self._insert(t.cost_layer, values)
                # CostEntryDraft.layer_id is the engine's in-memory UUID; the database assigns the
                # persisted layer id. Entries must reference the persisted id or fk_entry_layer fails.
                layer_ids[action.layer_id] = row["id"]
            elif action.action == "CONSUME":
                await self.session.execute(
                    update(t.cost_layer)
                    .where(
                        t.cost_layer.c.tenant_id == self.tenant_id,
                        t.cost_layer.c.id == UUID(str(action.layer_id)),
                    )
                    .values(
                        qty_remaining=_q(action.qty + Decimal("0")),
                        value_remaining=_q(action.value),
                        status="EXHAUSTED" if action.qty == 0 else "OPEN",
                        version=t.cost_layer.c.version + 1,
                    )
                )
                await self._insert(
                    t.cost_layer_consumption,
                    {
                        "cost_layer_id": action.layer_id,
                        "consuming_transaction_line_id": inventory_transaction_line_id,
                        "qty": action.qty,
                        "value": action.value,
                        "consumption_seq": action.layer_seq or 1,
                    },
                )
        entries: list[dict[str, Any]] = []
        for draft in result.cost_entries:
            row = await self._insert(
                t.cost_ledger_entry,
                {
                    "entry_seq": entry_seq,
                    "inventory_transaction_id": transaction_id,
                    "inventory_transaction_line_id": inventory_transaction_line_id,
                    "item_cost_id": item_cost_id,
                    "movement_type": draft.movement_type,
                    "qty_delta": draft.qty_delta,
                    "unit_cost": draft.unit_cost,
                    "value_delta": draft.value_delta,
                    "costing_method": method,
                    "layer_id": layer_ids.get(draft.layer_id, draft.layer_id),
                    "estimated": draft.estimated,
                    "rounding_residual": draft.rounding_residual,
                    "effective_date": effective_date,
                    "fiscal_period_id": fiscal_period_id,
                },
            )
            entries.append(row)
            entry_seq += 1
        if result.new_state is not None:
            await self.session.execute(
                update(t.item_cost)
                .where(t.item_cost.c.tenant_id == self.tenant_id, t.item_cost.c.id == UUID(str(item_cost_id)))
                .values(
                    qty_on_hand=result.new_state.qty_on_hand,
                    value_base=result.new_state.value_base,
                    avg_cost=result.new_state.avg_cost,
                    last_purchase_price=result.new_state.last_purchase_price,
                    status=result.new_state.status,
                    cost_provisional=result.new_state.cost_provisional,
                    last_cost_ledger_seq=entry_seq - 1,
                    version=t.item_cost.c.version + 1,
                )
            )
        for event in result.events:
            await emit_outbox(
                self.session,
                event_type=event.event_type,
                tenant_id=str(self.tenant_id),
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                payload=dict(event.payload),
                aggregate_version=event.aggregate_version,
            )
        return {"entries": entries, "entry_count": len(entries), "last_entry_seq": entry_seq - 1}

    async def persist_reversal(self, *, item_cost_id: str, transaction_id: str, result: ValueResult, effective_date: date) -> dict[str, Any]:
        return await self.persist_value_result(
            item_cost_id=item_cost_id,
            transaction_id=transaction_id,
            inventory_transaction_line_id=None,
            result=result,
            effective_date=effective_date,
        )


# Convenience alias used by the contract documentation/FS-04 adapter.
CostingWriter = CostingRepository


__all__ = ["CostingRepository", "CostingWriter"]
