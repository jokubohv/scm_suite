"""`replenishment.planning@1.0.0` in-process facade for FS-11/FS-15 consumers.

Delegates to :class:`app.modules.replenishment.service.PlanningService` with the caller's session and
tenant (no HTTP between modules). Planning is read-mostly for dependants: consumers may read plans,
shortages, ATP and workbench/signal feeds, and FS-11 may request a WMS cycle-count via other contracts
(FS-07); FS-10 never mutates ledger/cost tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.modules.replenishment.service import PlanningService

CONTRACT_VERSION = "1.0.0"


class ReplenishmentPort:
    def __init__(self, session: AsyncSession, tenant_id: Any) -> None:
        from app.modules.replenishment.service import PlanningService

        self.session = session
        self.tenant_id = tenant_id
        self.service: PlanningService = PlanningService(session, tenant_id)

    async def planning_preferences(self) -> dict[str, Any]:
        return await self.service.list_preferences()

    async def atp_query(self, *, item_id: str, location_id: str, qty: Any, required_by: Any = None, method: str | None = None) -> dict[str, Any]:
        return await self.service.atp_query(item_id=item_id, location_id=location_id, qty=qty, required_by=required_by, method=method)

    async def atp_projection(self, *, item_id: str, location_id: str, date_from: Any = None, date_to: Any = None) -> dict[str, Any]:
        return await self.service.atp_projection(item_id=item_id, location_id=location_id, date_from=date_from, date_to=date_to)

    async def list_supply_plans(self, **filters: Any) -> dict[str, Any]:
        return await self.service.list_supply_plans(**filters)

    async def list_demand_plans(self, **filters: Any) -> dict[str, Any]:
        return await self.service.list_demand_plans(**filters)

    async def supply_plan(self, plan_id: str) -> dict[str, Any]:
        return await self.service.get_supply_plan(plan_id, include_lines=False, include_suggestions=False)

    async def supply_plan_lines(self, plan_id: str, **filters: Any) -> dict[str, Any]:
        return await self.service.list_supply_plan_lines(plan_id, **filters)

    async def suggestions(self, plan_id: str, **filters: Any) -> dict[str, Any]:
        return await self.service.list_suggestions(plan_id, **filters)

    async def action_messages(self, **filters: Any) -> dict[str, Any]:
        return await self.service.list_action_messages(**filters)

    async def order_items(self, **filters: Any) -> dict[str, Any]:
        return await self.service.order_items(**filters)

    async def workbench(self, **filters: Any) -> dict[str, Any]:
        return await self.service.workbench(**filters)

    async def signals(self, **filters: Any) -> dict[str, Any]:
        return await self.service.signals(**filters)


__all__ = ["CONTRACT_VERSION", "ReplenishmentPort"]
