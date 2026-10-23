"""`wms.execution@1.0.0` in-process facade for dependant feature sets.

The facade participates in the caller's AsyncSession transaction (never commits, never uses HTTP) and
exposes read surfaces for reporting/quality/consignment consumers, plus the event/job catalogues used
by the provider contract test. Mutations stay with the WMS API/service (FS-12/13/14/15 only read WMS
task/shipment status dimensions).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.wms.service import WmsService

WMS_CONTRACT_VERSION = "1.0.0"

EVENT_TYPES: tuple[str, ...] = (
    "wms.inbound_shipment.arrived",
    "wms.inbound_shipment.created",
    "wms.pack.completed",
    "wms.receiving.reversed",
    "wms.replenishment.generated",
    "wms.task.completed",
    "wms.task.created",
    "wms.task.exception",
    "wms.task.reversed",
    "wms.task.watchdog.alert",
)

JOB_NAMES: tuple[str, ...] = (
    "wms.wave.release_schedule",
    "wms.putaway.assign",
    "wms.replenishment.generate",
    "wms.task.watchdog",
    "wms.shipment.manifest.reconcile",
    "wms.fulfillment.post_retry",
    "wms.device_session.purge",
    "wms.backorder.release",
)


class WmsFacade:
    """Frozen `wms.execution@1.0.0` read facade bound to one caller transaction/session."""

    def __init__(self, session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.service = WmsService(session, tenant_id)

    async def get_warehouse(self, warehouse_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_warehouse(warehouse_id)

    async def list_warehouses(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self.service.list_warehouses(limit=limit)

    async def get_task(self, task_id: str | uuid.UUID, *, task_type: str) -> dict[str, Any]:
        normalised = str(task_type).upper()
        if normalised == "PUTAWAY":
            return await self.service.get_putaway_task(task_id)
        if normalised == "PICK":
            return await self.service.get_pick_task(task_id)
        raise ValueError(f"unsupported task type {task_type!r} (putaway|pick)")

    async def list_tasks(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        options = dict(filters or {})
        return await self.service.list_tasks(
            task_type=options.get("task_type"),
            status=options.get("status"),
            warehouse_id=options.get("warehouse_id"),
            assigned_user_id=options.get("assigned_user_id"),
            limit=int(options.get("limit", 100)),
        )

    async def get_shipment(self, shipment_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_shipment(shipment_id)

    async def list_shipments(self, filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        options = dict(filters or {})
        return await self.service.list_shipments(
            location_id=options.get("location_id"),
            status=options.get("status"),
            limit=int(options.get("limit", 100)),
        )

    async def get_receiving_order(self, receiving_order_id: str | uuid.UUID) -> dict[str, Any]:
        return await self.service.get_receiving_order(receiving_order_id)

    async def order_release_status(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        options = dict(filters or {})
        return await self.service.order_release_status(
            location_id=options.get("location_id"),
            status=options.get("status"),
            limit=int(options.get("limit", 100)),
        )


__all__ = ["EVENT_TYPES", "JOB_NAMES", "WMS_CONTRACT_VERSION", "WmsFacade"]
