"""``manufacturing.execution@1.1.0`` in-process facade for dependant feature sets.

Frozen read/draft surface consumed by FS-10 (open-WO demand/supply, draft WOs, BOM/calendar reads,
available-to-build), FS-11 (work-order component pick requirements, WO status guards, build/completion
triggers), FS-12 (WO-triggered inspections), FS-13/FS-14 (assembly configuration/unbuild data) and
FS-15 (read models). The facade participates in the caller's AsyncSession transaction (never commits,
never uses HTTP) and validates compatibility at configuration boundaries.

The binding spec is ``feature-sets/FS-09.md`` §3 (+ Δ-CFG/Δ-REUSE): contract ``1.1.0`` is additive over
``1.0.0``; consumer ranges ``>=1.0.0 <2.0.0`` remain unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.manufacturing.service import ManufacturingService

CONTRACT_VERSION = "1.1.0"
CONTRACT_NAME = "manufacturing.execution"

EVENT_TYPES: tuple[str, ...] = (
    "work_order.created",
    "work_order.firmed",
    "work_order.released",
    "work_order.built",
    "work_order.closed",
    "work_order.voided",
    "work_order.reopened",
    "work_order.rescheduled",
    "assembly.built",
    "assembly.unbuilt",
    "component.consumed",
    "wip.issued",
    "wip.completed",
    "wip.backflushed",
    "wip.closed",
    "operation.completed",
    "eco.implemented",
    "scheduling.run.completed",
    "available_to_build.calculated",
    "manufacturing.compat.rule_set.activated",
    "manufacturing.config.validated",
    "manufacturing.config.violation",
    "assembly.unbuild.disposition.recorded",
    "manufacturing.preferences.changed",
)

JOB_NAMES: tuple[str, ...] = (
    "mfg.scheduling.run",
    "mfg.capacity.rollup",
    "mfg.wo.demand.sync",
    "mfg.wip.reconcile",
    "mfg.available_to_build.refresh",
)

OPEN_WO_STATUSES = ("PLANNED", "FIRM_PLANNED", "RELEASED", "IN_PROCESS")


class ManufacturingFacade:
    """Frozen `manufacturing.execution@1.1.0` facade bound to one caller transaction/session."""

    def __init__(
        self, session: AsyncSession, tenant_id: str | uuid.UUID, *, actor_id: str | uuid.UUID | None = None
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.service = ManufacturingService(session, tenant_id, actor_id=actor_id)

    # -- work orders / demand + supply ---------------------------------------------------------
    async def get_work_order(self, work_order_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_work_order(work_order_id)

    async def list_open_work_orders(
        self,
        *,
        statuses: Sequence[str] = OPEN_WO_STATUSES,
        assembly_item_id: str | uuid.UUID | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Open WO/output supply and component demand for planning (FS-10 reads)."""
        result: list[dict[str, Any]] = []
        for status in statuses:
            for work_order in await self.service.list_work_orders(
                status=status, assembly_item_id=assembly_item_id, limit=limit
            ):
                result.append(await self.service.get_work_order(work_order["id"]))
        return result

    async def get_component_requirements(self, work_order_id: str | uuid.UUID) -> dict[str, Any]:
        """FS-11 component picking interface (pull endpoint / release-event payload equivalent)."""
        return await self.service.pick_requirements(work_order_id)

    async def create_planned_work_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """FS-10 draft planned-WO creation path (returns a PLANNED WO; no postings)."""
        return await self.service.create_work_order({**dict(payload), "status": "PLANNED"})

    async def create_draft_work_order(
        self, lines: Sequence[Mapping[str, Any]], options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """FS-10 ``CREATE_WO`` suggestion conversion: create PLANNED work order(s); drafts only, no postings.

        ``lines`` carry ``item_id`` (item-master id), ``qty`` and optional ``location_id``; ``options``
        carry ``planned_end_date``/``memo``. Returns the first created work order with its ``id`` (the
        planning suggestion stores that document reference).
        """
        opts = dict(options or {})
        if not lines:
            raise ValueError("at least one work-order line is required")
        created: list[dict[str, Any]] = []
        for line in lines:
            assembly = await self.service.get_assembly_item_by_item_id(line.get("item_id"))
            planned_end = opts.get("planned_end_date") or line.get("due_date") or line.get("planned_end_date")
            payload = {
                "assembly_item_id": assembly["id"],
                "qty_ordered": line.get("qty"),
                "start_date": line.get("start_date") or planned_end,
                "end_date": planned_end,
                "wip_location_id": line.get("location_id") or opts.get("location_id"),
                "memo": opts.get("memo") or "created from planning suggestion",
                "external_sales_ref": opts.get("external_sales_ref"),
            }
            created.append(await self.service.create_work_order(payload))
        result = dict(created[0])
        result["work_order_ids"] = [work_order["id"] for work_order in created]
        return result

    async def release_work_order(self, work_order_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.release_work_order(work_order_id)

    async def complete_work_order(self, work_order_id: str | uuid.UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self.service.complete_work_order(work_order_id, payload)

    async def get_wip_balance(self, work_order_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_wip_balance(work_order_id)

    # -- BOM / routings / calendars ------------------------------------------------------------
    async def resolve_bom(
        self,
        assembly_item_id: str | uuid.UUID,
        *,
        as_of: date | None = None,
        bom_revision_id: str | uuid.UUID | None = None,
        qty_ordered: Any = 1,
    ) -> list[dict[str, Any]]:
        return await self.service.resolve_bom(
            assembly_item_id, as_of=as_of, bom_revision_id=bom_revision_id, qty_ordered=qty_ordered
        )

    async def bom_inquiry(
        self, assembly_item_id: str | uuid.UUID, *, qty: Any = 1, as_of: date | None = None
    ) -> dict[str, Any]:
        return await self.service.bom_inquiry(assembly_item_id, qty=qty, as_of=as_of)

    async def get_assembly_item(self, assembly_item_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_assembly_item(assembly_item_id)

    async def list_work_centers(self, limit: int = 500) -> list[dict[str, Any]]:
        return await self.service.list_work_centers(limit=limit)

    # -- available-to-build / compatibility ----------------------------------------------------
    async def available_to_build(
        self,
        item_id: str | uuid.UUID,
        *,
        location_id: str | uuid.UUID | None = None,
        as_of: date | None = None,
        level_mode: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.available_to_build(
            item_id, location_id=location_id, as_of=as_of, level_mode=level_mode
        )

    async def validate_configuration(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Order/configuration boundary + manufacturing-path compatibility validation (Δ-CFG)."""
        return await self.service.validate_configuration(dict(payload))

    async def list_condition_codes(self) -> list[dict[str, Any]]:
        return await self.service.list_condition_codes()

    async def get_condition_code(self, condition_id: str | uuid.UUID) -> dict[str, Any]:
        codes = {code["id"]: code for code in await self.service.list_condition_codes()}
        code = codes.get(str(condition_id))
        if code is None:
            raise KeyError(f"condition code {condition_id} not found")
        return code

    # -- scheduling / capacity read models -----------------------------------------------------
    async def list_capacity_plans(
        self, *, work_center_id: str | uuid.UUID | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self.service.list_capacity_plans(work_center_id=work_center_id, limit=limit)

    async def get_scheduling_run(self, run_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_scheduling_run(run_id)


__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "EVENT_TYPES",
    "JOB_NAMES",
    "ManufacturingFacade",
    "OPEN_WO_STATUSES",
]
