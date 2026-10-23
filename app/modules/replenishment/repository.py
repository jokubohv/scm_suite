"""FS-10 planning persistence + dependency-contract bridge (async SQLAlchemy Core).

Writes only ``replenishment.*`` tables. Reads on-hand/committed/on-order via the frozen
``inventory.balances`` port, item planning settings via ``item.master`` facade, and creates drafts via
FS-08/FS-09/FS-04 facades only on explicit suggestion conversion; planning never writes ledger/cost rows
(DM-PLAN-01/INV-20, DM-PLAN-10).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import platform as platform_models
from app.core.outbox import emit as emit_outbox
from app.modules.replenishment import tables as t
from app.modules.replenishment.engine import PlanningError, q12

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
        raise PlanningError(f"{field} must be a UUID", code="VALIDATION_FAILED") from exc


class PlanningRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID | str) -> None:
        self.session = session
        self.tenant_id = _uuid(tenant_id, field="tenant_id")

    # ---------------------------------------------------------------- generic helpers ------
    async def _select(self, table: Any, *conditions: Any, limit: int | None = None, order_by: Any = None) -> list[dict[str, Any]]:
        statement = select(table).where(table.c.tenant_id == self.tenant_id, *conditions)
        if order_by is not None:
            statement = statement.order_by(order_by)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return [_row(dict(mapping)) for mapping in result.mappings().all()]

    async def _get(self, table: Any, row_id: str | UUID) -> dict[str, Any] | None:
        result = await self.session.execute(
            select(table).where(table.c.tenant_id == self.tenant_id, table.c.id == _uuid(row_id))
        )
        return _one(result.mappings().first())

    async def _insert(self, table: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        row_id = values.get("id") or uuid4()
        await self.session.execute(insert(table).values({"id": row_id, "tenant_id": self.tenant_id, **dict(values)}))
        await self.session.flush()
        row = await self._get(table, row_id)
        if row is None:  # pragma: no cover
            raise PlanningError("inserted row could not be read back")
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
            raise PlanningError(f"{table.name} row not found", code="NOT_FOUND", status_code=404)
        return _row(dict(row))

    async def _delete(self, table: Any, *conditions: Any) -> None:
        await self.session.execute(delete(table).where(table.c.tenant_id == self.tenant_id, *conditions))

    async def _count(self, table: Any, *conditions: Any) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(table).where(table.c.tenant_id == self.tenant_id, *conditions)
        )
        return int(result.scalar_one())

    async def next_number(self, doc_type: str) -> str:
        table = platform_models.metadata.tables["platform.number_sequence"]
        row = (
            await self.session.execute(
                select(table)
                .where(table.c.tenant_id == self.tenant_id, table.c.doc_type == doc_type)
                .with_for_update()
            )
        ).mappings().first()
        if row is not None:
            value = int(row["next_value"])
            await self.session.execute(
                table.update().where(table.c.id == row["id"]).values(next_value=value + 1, version=table.c.version + 1)
            )
            return f"{row['prefix']}{value:0{int(row['padding'])}d}"
        return f"{doc_type}-{uuid4().hex[:8].upper()}"

    async def emit(self, event_type: str, *, aggregate_id: str, payload: Mapping[str, Any], aggregate_type: str = "planning_run") -> None:
        await emit_outbox(
            self.session,
            event_type=event_type,
            tenant_id=str(self.tenant_id),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=dict(payload),
        )

    async def enqueue_job(self, name: str, *, run_key: str, payload: Mapping[str, Any] | None = None) -> str | None:
        definition = platform_models.metadata.tables["platform.job_definition"]
        run_table = platform_models.metadata.tables["platform.job_run"]
        definition_row = (
            await self.session.execute(
                select(definition).where(definition.c.tenant_id == self.tenant_id, definition.c.name == name)
            )
        ).mappings().first()
        if definition_row is None:
            return None
        row_id = uuid4()
        await self.session.execute(
            insert(run_table).values(
                id=row_id, tenant_id=self.tenant_id, job_definition_id=definition_row["id"],
                run_key=run_key, status="QUEUED", attempt=1, cursor=dict(payload or {}), version=1,
            )
        )
        await self.session.flush()
        return str(row_id)

    # ------------------------------------------------------------------ config -------------
    async def list_rules(self, *, engine: str | None = None, is_active: bool | None = None) -> list[dict[str, Any]]:
        conditions = []
        if engine is not None:
            conditions.append(t.planning_rule.c.engine == engine.upper())
        if is_active is not None:
            conditions.append(t.planning_rule.c.is_active.is_(is_active))
        rules = await self._select(t.planning_rule, *conditions, order_by=t.planning_rule.c.code)
        for rule in rules:
            rule["scopes"] = await self.list_rule_scopes(rule["id"])
        return rules

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        rule = await self._get(t.planning_rule, rule_id)
        if rule is not None:
            rule["scopes"] = await self.list_rule_scopes(rule_id)
        return rule

    async def create_rule(self, values: Mapping[str, Any], scopes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        rule = await self._insert(t.planning_rule, values)
        for scope in scopes:
            await self._insert(t.planning_rule_scope, {"planning_rule_id": rule["id"], **dict(scope)})
        return await self.get_rule(rule["id"]) or rule

    async def update_rule(self, rule_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.planning_rule, rule_id, values)

    async def list_rule_scopes(self, rule_id: str) -> list[dict[str, Any]]:
        return await self._select(t.planning_rule_scope, t.planning_rule_scope.c.planning_rule_id == _uuid(rule_id))

    async def replace_rule_scopes(self, rule_id: str, scopes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._delete(t.planning_rule_scope, t.planning_rule_scope.c.planning_rule_id == _uuid(rule_id))
        for scope in scopes:
            await self._insert(t.planning_rule_scope, {"planning_rule_id": rule_id, **dict(scope)})
        return await self.list_rule_scopes(rule_id)

    async def list_time_fences(self, **filters: Any) -> list[dict[str, Any]]:
        conditions = []
        if filters.get("scope_type") is not None:
            conditions.append(t.time_fence.c.scope_type == str(filters["scope_type"]).upper())
        if filters.get("is_active") is not None:
            conditions.append(t.time_fence.c.is_active.is_(filters["is_active"]))
        return await self._select(t.time_fence, *conditions)

    async def get_time_fence(self, fence_id: str) -> dict[str, Any] | None:
        return await self._get(t.time_fence, fence_id)

    async def upsert_time_fence(self, values: Mapping[str, Any]) -> dict[str, Any]:
        existing = await self.session.execute(
            select(t.time_fence).where(
                t.time_fence.c.tenant_id == self.tenant_id,
                t.time_fence.c.scope_type == values["scope_type"],
                t.time_fence.c.scope_id == (values.get("scope_id") or None),
            )
        )
        mapping = existing.mappings().first()
        if mapping is not None:
            return await self._update(t.time_fence, mapping["id"], values)
        return await self._insert(t.time_fence, values)

    async def deactivate_time_fence(self, fence_id: str) -> None:
        await self._update(t.time_fence, fence_id, {"is_active": False})

    # ------------------------------------------------------------------ demand history -----
    async def list_demand_history(self, *, item_id: str | None = None, location_id: str | None = None, date_from: date | None = None, date_to: date | None = None, granularity: str | None = None, source: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        conditions = []
        if item_id:
            conditions.append(t.demand_history.c.item_id == _uuid(item_id))
        if location_id:
            conditions.append(t.demand_history.c.location_id == _uuid(location_id))
        if date_from:
            conditions.append(t.demand_history.c.bucket_date >= date_from)
        if date_to:
            conditions.append(t.demand_history.c.bucket_date <= date_to)
        if granularity:
            conditions.append(t.demand_history.c.bucket_granularity == granularity.upper())
        if source:
            conditions.append(t.demand_history.c.source == source.upper())
        return await self._select(t.demand_history, *conditions, limit=max(1, min(limit, 2000)), order_by=t.demand_history.c.bucket_date)

    async def upsert_demand_history(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for row in rows:
            existing = await self.session.execute(
                select(t.demand_history).where(
                    t.demand_history.c.tenant_id == self.tenant_id,
                    t.demand_history.c.item_id == _uuid(row["item_id"]),
                    t.demand_history.c.location_id == _uuid(row["location_id"]),
                    t.demand_history.c.bucket_date == row["bucket_date"],
                    t.demand_history.c.bucket_granularity == row["bucket_granularity"],
                    t.demand_history.c.source == row["source"],
                )
            )
            mapping = existing.mappings().first()
            if mapping is not None:
                created.append(await self._update(t.demand_history, mapping["id"], {"qty": row["qty"]}))
            else:
                created.append(await self._insert(t.demand_history, dict(row)))
        return created

    # ------------------------------------------------------------------ demand plans -------
    async def list_demand_plans(self, *, status: str | None = None, method: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.demand_plan.c.status == status.upper())
        if method:
            conditions.append(t.demand_plan.c.method == method.upper())
        return await self._select(t.demand_plan, *conditions, limit=max(1, min(limit, 500)), order_by=t.demand_plan.c.created_at.desc())

    async def get_demand_plan(self, plan_id: str) -> dict[str, Any] | None:
        plan = await self._get(t.demand_plan, plan_id)
        if plan is not None:
            plan["line_count"] = await self._count(t.demand_plan_line, t.demand_plan_line.c.demand_plan_id == _uuid(plan_id))
        return plan

    async def create_demand_plan(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.demand_plan, values)

    async def update_demand_plan(self, plan_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.demand_plan, plan_id, values)

    async def list_demand_plan_lines(self, plan_id: str, *, item_id: str | None = None, location_id: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        conditions = [t.demand_plan_line.c.demand_plan_id == _uuid(plan_id)]
        if item_id:
            conditions.append(t.demand_plan_line.c.item_id == _uuid(item_id))
        if location_id:
            conditions.append(t.demand_plan_line.c.location_id == _uuid(location_id))
        return await self._select(t.demand_plan_line, *conditions, limit=limit, order_by=t.demand_plan_line.c.bucket_date)

    async def get_demand_plan_line(self, line_id: str) -> dict[str, Any] | None:
        return await self._get(t.demand_plan_line, line_id)

    async def replace_demand_plan_lines(self, plan_id: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._delete(t.demand_plan_line, t.demand_plan_line.c.demand_plan_id == _uuid(plan_id))
        created = [await self._insert(t.demand_plan_line, {"demand_plan_id": plan_id, **dict(row)}) for row in rows]
        return created

    async def update_demand_plan_line(self, line_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.demand_plan_line, line_id, values)

    # ------------------------------------------------------------------ supply plans ------
    async def list_supply_plans(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.supply_plan.c.status == status.upper())
        return await self._select(t.supply_plan, *conditions, limit=max(1, min(limit, 500)), order_by=t.supply_plan.c.created_at.desc())

    async def get_supply_plan(self, plan_id: str) -> dict[str, Any] | None:
        plan = await self._get(t.supply_plan, plan_id)
        if plan is not None:
            plan["line_count"] = await self._count(t.supply_plan_line, t.supply_plan_line.c.supply_plan_id == _uuid(plan_id))
            plan["suggestion_count"] = await self._count(t.supply_plan_suggestion, t.supply_plan_suggestion.c.supply_plan_id == _uuid(plan_id))
        return plan

    async def get_supply_plan_by_run_key(self, run_key: str) -> dict[str, Any] | None:
        rows = await self._select(t.supply_plan, t.supply_plan.c.run_key == run_key, limit=1)
        return rows[0] if rows else None

    async def create_supply_plan(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.supply_plan, values)

    async def update_supply_plan(self, plan_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.supply_plan, plan_id, values)

    async def replace_supply_results(self, plan_id: str, lines: Sequence[Mapping[str, Any]], suggestions: Sequence[Mapping[str, Any]], messages: Sequence[Mapping[str, Any]]) -> None:
        await self._delete(t.supply_plan_line, t.supply_plan_line.c.supply_plan_id == _uuid(plan_id))
        for line in lines:
            await self._insert(t.supply_plan_line, {"supply_plan_id": plan_id, **dict(line)})
        await self._delete(t.supply_plan_suggestion, t.supply_plan_suggestion.c.supply_plan_id == _uuid(plan_id))
        for suggestion in suggestions:
            await self._insert(t.supply_plan_suggestion, {"supply_plan_id": plan_id, **dict(suggestion)})
        for message in messages:
            await self._insert(t.planning_action_message, {"supply_plan_id": plan_id, **dict(message)})

    async def list_supply_plan_lines(self, plan_id: str, *, item_id: str | None = None, location_id: str | None = None, shortage_only: bool = False) -> list[dict[str, Any]]:
        conditions = [t.supply_plan_line.c.supply_plan_id == _uuid(plan_id)]
        if item_id:
            conditions.append(t.supply_plan_line.c.item_id == _uuid(item_id))
        if location_id:
            conditions.append(t.supply_plan_line.c.location_id == _uuid(location_id))
        if shortage_only:
            conditions.append(t.supply_plan_line.c.is_shortage.is_(True))
        return await self._select(t.supply_plan_line, *conditions, order_by=t.supply_plan_line.c.bucket_date)

    async def list_suggestions(self, plan_id: str, *, action_type: str | None = None, status: str | None = None, item_id: str | None = None) -> list[dict[str, Any]]:
        conditions = [t.supply_plan_suggestion.c.supply_plan_id == _uuid(plan_id)]
        if action_type:
            conditions.append(t.supply_plan_suggestion.c.action_type == action_type.upper())
        if status:
            conditions.append(t.supply_plan_suggestion.c.status == status.upper())
        if item_id:
            conditions.append(t.supply_plan_suggestion.c.item_id == _uuid(item_id))
        return await self._select(t.supply_plan_suggestion, *conditions, order_by=t.supply_plan_suggestion.c.due_date)

    async def get_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        return await self._get(t.supply_plan_suggestion, suggestion_id)

    async def update_suggestion(self, suggestion_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.supply_plan_suggestion, suggestion_id, values)

    async def list_action_messages(self, *, status: str | None = None, severity: str | None = None, message_type: str | None = None, plan_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.planning_action_message.c.status == status.upper())
        if severity:
            conditions.append(t.planning_action_message.c.severity == severity.upper())
        if message_type:
            conditions.append(t.planning_action_message.c.message_type == message_type.upper())
        if plan_id:
            conditions.append(t.planning_action_message.c.supply_plan_id == _uuid(plan_id))
        return await self._select(t.planning_action_message, *conditions, limit=max(1, min(limit, 500)), order_by=t.planning_action_message.c.created_at.desc())

    async def get_action_message(self, message_id: str) -> dict[str, Any] | None:
        return await self._get(t.planning_action_message, message_id)

    async def update_action_message(self, message_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.planning_action_message, message_id, values)

    # ------------------------------------------------------------------ DRP ----------------
    async def list_networks(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.drp_network.c.status == status.upper())
        return await self._select(t.drp_network, *conditions, order_by=t.drp_network.c.code)

    async def get_network(self, network_id: str) -> dict[str, Any] | None:
        network = await self._get(t.drp_network, network_id)
        if network is not None:
            network["members"] = await self.list_network_members(network_id)
        return network

    async def create_network(self, values: Mapping[str, Any], members: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        network = await self._insert(t.drp_network, values)
        if members:
            await self.replace_network_members(network["id"], members)
        return await self.get_network(network["id"]) or network

    async def update_network(self, network_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.drp_network, network_id, values)

    async def list_network_members(self, network_id: str) -> list[dict[str, Any]]:
        return await self._select(t.drp_network_member, t.drp_network_member.c.drp_network_id == _uuid(network_id))

    async def replace_network_members(self, network_id: str, members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._delete(t.drp_network_member, t.drp_network_member.c.drp_network_id == _uuid(network_id))
        for member in members:
            await self._insert(t.drp_network_member, {"drp_network_id": network_id, **dict(member)})
        return await self.list_network_members(network_id)

    async def list_bods(self, *, network_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if network_id:
            conditions.append(t.bill_of_distribution.c.drp_network_id == _uuid(network_id))
        if status:
            conditions.append(t.bill_of_distribution.c.status == status.upper())
        return await self._select(t.bill_of_distribution, *conditions, order_by=t.bill_of_distribution.c.created_at.desc())

    async def get_bod(self, bod_id: str) -> dict[str, Any] | None:
        bod = await self._get(t.bill_of_distribution, bod_id)
        if bod is not None:
            bod["levels"] = await self.list_bod_levels(bod_id)
        return bod

    async def create_bod(self, values: Mapping[str, Any], levels: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        bod = await self._insert(t.bill_of_distribution, values)
        if levels:
            await self.replace_bod_levels(bod["id"], levels)
        return await self.get_bod(bod["id"]) or bod

    async def list_bod_levels(self, bod_id: str) -> list[dict[str, Any]]:
        return await self._select(t.bill_of_distribution_level, t.bill_of_distribution_level.c.bill_of_distribution_id == _uuid(bod_id), order_by=t.bill_of_distribution_level.c.level)

    async def replace_bod_levels(self, bod_id: str, levels: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._delete(t.bill_of_distribution_level, t.bill_of_distribution_level.c.bill_of_distribution_id == _uuid(bod_id))
        for level in levels:
            await self._insert(t.bill_of_distribution_level, {"bill_of_distribution_id": bod_id, **dict(level)})
        return await self.list_bod_levels(bod_id)

    async def list_distribution_categories(self, *, is_active: bool | None = None) -> list[dict[str, Any]]:
        conditions = []
        if is_active is not None:
            conditions.append(t.distribution_category.c.is_active.is_(is_active))
        return await self._select(t.distribution_category, *conditions, order_by=t.distribution_category.c.code)

    async def get_distribution_category(self, category_id: str) -> dict[str, Any] | None:
        category = await self._get(t.distribution_category, category_id)
        if category is not None:
            rows = await self._select(t.distribution_category_member, t.distribution_category_member.c.distribution_category_id == _uuid(category_id))
            category["item_ids"] = [row["item_id"] for row in rows]
        return category

    async def create_distribution_category(self, values: Mapping[str, Any], item_ids: Sequence[str] = ()) -> dict[str, Any]:
        category = await self._insert(t.distribution_category, values)
        if item_ids:
            await self.replace_distribution_members(category["id"], item_ids)
        return await self.get_distribution_category(category["id"]) or category

    async def replace_distribution_members(self, category_id: str, item_ids: Sequence[str]) -> list[dict[str, Any]]:
        await self._delete(t.distribution_category_member, t.distribution_category_member.c.distribution_category_id == _uuid(category_id))
        for item_id in item_ids:
            await self._insert(t.distribution_category_member, {"distribution_category_id": category_id, "item_id": _uuid(item_id)})
        category = await self.get_distribution_category(category_id)
        return (category or {}).get("item_ids", [])

    # ------------------------------------------------------------------ allocation ---------
    async def list_strategies(self, *, is_active: bool | None = None, method: str | None = None) -> list[dict[str, Any]]:
        conditions: list[Any] = []
        if is_active is not None:
            conditions.append(t.allocation_strategy.c.is_active.is_(is_active))
        if method:
            conditions.append(t.allocation_strategy.c.method == method.upper())
        strategies = await self._select(t.allocation_strategy, *conditions, order_by=t.allocation_strategy.c.priority)
        for strategy in strategies:
            strategy["rules"] = await self.list_strategy_rules(strategy["id"])
        return strategies

    async def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        strategy = await self._get(t.allocation_strategy, strategy_id)
        if strategy is not None:
            strategy["rules"] = await self.list_strategy_rules(strategy_id)
        return strategy

    async def create_strategy(self, values: Mapping[str, Any], rules: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        strategy = await self._insert(t.allocation_strategy, values)
        if rules:
            await self.replace_strategy_rules(strategy["id"], rules)
        return await self.get_strategy(strategy["id"]) or strategy

    async def update_strategy(self, strategy_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.allocation_strategy, strategy_id, values)

    async def list_strategy_rules(self, strategy_id: str) -> list[dict[str, Any]]:
        return await self._select(t.allocation_strategy_rule, t.allocation_strategy_rule.c.allocation_strategy_id == _uuid(strategy_id), order_by=t.allocation_strategy_rule.c.sequence)

    async def replace_strategy_rules(self, strategy_id: str, rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        await self._delete(t.allocation_strategy_rule, t.allocation_strategy_rule.c.allocation_strategy_id == _uuid(strategy_id))
        for rule in rules:
            await self._insert(t.allocation_strategy_rule, {"allocation_strategy_id": strategy_id, **dict(rule)})
        return await self.list_strategy_rules(strategy_id)

    async def list_allocation_runs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.allocation_run.c.status == status.upper())
        return await self._select(t.allocation_run, *conditions, order_by=t.allocation_run.c.created_at.desc())

    async def get_allocation_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._get(t.allocation_run, run_id)

    async def create_allocation_run(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.allocation_run, values)

    async def update_allocation_run(self, run_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.allocation_run, run_id, values)

    async def replace_allocation_results(self, run_id: str, results: Sequence[Mapping[str, Any]], exceptions: Sequence[Mapping[str, Any]]) -> None:
        await self._delete(t.allocation_result, t.allocation_result.c.allocation_run_id == _uuid(run_id))
        for result in results:
            await self._insert(t.allocation_result, {"allocation_run_id": run_id, **dict(result)})
        await self._delete(t.allocation_exception, t.allocation_exception.c.allocation_run_id == _uuid(run_id))
        for exception in exceptions:
            await self._insert(t.allocation_exception, {"allocation_run_id": run_id, **dict(exception)})

    async def list_allocation_results(self, run_id: str, *, item_id: str | None = None) -> list[dict[str, Any]]:
        conditions = [t.allocation_result.c.allocation_run_id == _uuid(run_id)]
        if item_id:
            conditions.append(t.allocation_result.c.item_id == _uuid(item_id))
        return await self._select(t.allocation_result, *conditions)

    async def list_allocation_exceptions(self, run_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = [t.allocation_exception.c.allocation_run_id == _uuid(run_id)]
        if status:
            conditions.append(t.allocation_exception.c.status == status.upper())
        return await self._select(t.allocation_exception, *conditions)

    # ------------------------------------------------------------------ ATP / AIM -----------
    async def list_atp(self, *, item_id: str, location_id: str, method: str | None = None, date_from: date | None = None, date_to: date | None = None) -> list[dict[str, Any]]:
        conditions = [
            t.atp_projection.c.item_id == _uuid(item_id),
            t.atp_projection.c.location_id == _uuid(location_id),
        ]
        if method:
            conditions.append(t.atp_projection.c.method == method)
        if date_from:
            conditions.append(t.atp_projection.c.bucket_date >= date_from)
        if date_to:
            conditions.append(t.atp_projection.c.bucket_date <= date_to)
        return await self._select(t.atp_projection, *conditions, order_by=t.atp_projection.c.bucket_date)

    async def replace_atp_projection(self, *, item_id: str, location_id: str, method: str, rows: Sequence[Mapping[str, Any]]) -> int:
        await self._delete(
            t.atp_projection,
            t.atp_projection.c.item_id == _uuid(item_id),
            t.atp_projection.c.location_id == _uuid(location_id),
            t.atp_projection.c.method == method,
        )
        for row in rows:
            await self._insert(t.atp_projection, {"item_id": item_id, "location_id": location_id, "method": method, **dict(row)})
        return len(rows)

    async def list_replenishment_calculations(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if status:
            conditions.append(t.replenishment_calculation.c.status == status.upper())
        return await self._select(t.replenishment_calculation, *conditions, order_by=t.replenishment_calculation.c.created_at.desc())

    async def get_replenishment_calculation(self, calculation_id: str) -> dict[str, Any] | None:
        return await self._get(t.replenishment_calculation, calculation_id)

    async def create_replenishment_calculation(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._insert(t.replenishment_calculation, values)

    async def update_replenishment_calculation(self, calculation_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.replenishment_calculation, calculation_id, values)

    async def insert_calculation_lines(self, calculation_id: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        created = [await self._insert(t.replenishment_calculation_line, {"replenishment_calculation_id": calculation_id, **dict(row)}) for row in rows]
        return created

    async def list_calculation_lines(self, calculation_id: str, *, item_id: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
        conditions = [t.replenishment_calculation_line.c.replenishment_calculation_id == _uuid(calculation_id)]
        if item_id:
            conditions.append(t.replenishment_calculation_line.c.item_id == _uuid(item_id))
        if action:
            conditions.append(t.replenishment_calculation_line.c.action == action.upper())
        return await self._select(t.replenishment_calculation_line, *conditions)

    async def update_calculation_line(self, line_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return await self._update(t.replenishment_calculation_line, line_id, values)

    # ------------------------------------------------------------------ dependency reads ---
    async def item_planning_states(self, *, item_ids: Sequence[str] = (), location_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
        """Read on-hand/committed + FS-01 planning attributes for the planning scope."""
        from app.modules.inventory_ledger.facade import BalanceReadPort
        from app.modules.item_master.facade import get_item_location_settings

        port = BalanceReadPort(self.session, self.tenant_id)
        states: dict[tuple[str, str], dict[str, Any]] = {}
        for balance in (await port.balances(limit=500)).get("items") or []:
            item_id, location_id = str(balance["item_id"]), str(balance["location_id"])
            if item_ids and item_id not in {str(item) for item in item_ids}:
                continue
            if location_ids and location_id not in {str(location) for location in location_ids}:
                continue
            settings = await get_item_location_settings(self.session, self.tenant_id, item_id, location_id=location_id) or {}
            key = (item_id, location_id)
            entry = states.setdefault(
                key,
                {
                    "item_id": item_id, "location_id": location_id, "qty_on_hand": Decimal("0"),
                    "qty_committed": Decimal("0"), "qty_on_order": Decimal("0"),
                    "safety_stock": Decimal("0"), "lead_time_days": 0, "uom_id": None,
                },
            )
            entry["qty_on_hand"] = q12(Decimal(str(entry["qty_on_hand"])) + Decimal(str(balance.get("qty_on_hand") or 0)))
            entry["qty_committed"] = q12(Decimal(str(entry["qty_committed"])) + Decimal(str(balance.get("qty_committed") or 0)))
            entry["qty_on_order"] = q12(Decimal(str(entry["qty_on_order"])) + Decimal(str(balance.get("qty_on_order") or 0)))
            entry["safety_stock"] = q12(settings.get("safety_stock") or entry["safety_stock"])
            entry["lead_time_days"] = int(settings.get("lead_time_days") or entry["lead_time_days"])
            entry["uom_id"] = str(settings.get("stock_uom_id") or entry["uom_id"] or "") or None
        return list(states.values())

    async def open_purchase_supply(self, *, item_id: str, location_id: str) -> Decimal:
        from app.modules.procurement.facade import ProcurementFacade

        facade = ProcurementFacade(self.session, self.tenant_id)
        try:
            supply = await facade.get_open_po_supply(item_id, location_id=location_id)
        except Exception:  # noqa: BLE001 - procurement contract optional in unit/dev fixtures
            return Decimal("0")
        total = Decimal("0")
        for row in supply.get("items") or []:
            total += Decimal(str(row.get("open_qty") or row.get("qty_open") or 0))
        return q12(total)

    async def apply_item_planning_values(self, *, item_id: str, location_id: str, values: Mapping[str, Any], policy_version: str) -> dict[str, Any]:
        from app.modules.item_master.facade import apply_planning_values

        return await apply_planning_values(
            self.session, self.tenant_id, item_id, values, location_id=location_id or None,
            source="AIM", policy_version=policy_version,
        )

    async def create_draft_po(self, *, vendor_id: str, lines: Sequence[Mapping[str, Any]], options: Mapping[str, Any]) -> dict[str, Any]:
        from app.modules.procurement.facade import ProcurementFacade

        facade = ProcurementFacade(self.session, self.tenant_id)
        return await facade.create_draft_po(lines, vendor_id, options)

    async def create_draft_wo(self, *, lines: Sequence[Mapping[str, Any]], options: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from app.modules.manufacturing.facade import ManufacturingFacade

            facade = ManufacturingFacade(self.session, self.tenant_id)
            return await facade.create_draft_work_order(lines, options)
        except Exception as exc:  # noqa: BLE001 - FS-09 not deployed yet (migration 0021 chain note)
            raise PlanningError("work-order draft creation requires the manufacturing contract (FS-09)", code="DEPENDENCY_UNAVAILABLE", status_code=409) from exc


__all__ = ["PlanningRepository"]
