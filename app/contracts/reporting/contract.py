"""FS-15 ``reporting.readmodels@1.0.0`` builder facade.

Implemented, read-only contract surface over the 21 ``reporting.v_*`` views. The heavy lifting stays in
``app.modules.reporting.service``; this module is the stable facade other sets/exports (and the FS-15
contract tests) may import without depending on transport/router internals.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reporting.catalogue import ENDPOINTS, REPORT_ALIASES, VIEW_NAMES, VIEWS_BY_NAME
from app.modules.reporting.service import ReportService

CONTRACT = "reporting.readmodels"
VERSION = "1.0.0"
VIEWS = VIEW_NAMES
EVENTS = ("reporting.view.refreshed", "reporting.refresh.failed")
JOBS = ("reporting.refresh_views", "reporting.reconcile_readmodels")


def endpoint_paths() -> tuple[str, ...]:
    return tuple("/api/v1" + spec.path for spec in ENDPOINTS)


async def query_view(session: AsyncSession, *, tenant_id: str, app_user_id: str | None, view_name: str,
                     filters: Mapping[str, Any] | None = None, cursor: str | None = None,
                     limit: int = 100, fields: str | Sequence[str] | None = None) -> dict[str, Any]:
    """Query one read model; returns ``{items, next_cursor, freshness}``."""
    service = ReportService(session, tenant_id=tenant_id, app_user_id=app_user_id)
    page = await service.query(view_name, filters=filters, cursor=cursor, limit=limit, fields=fields)
    return {"items": page.items, "next_cursor": page.next_cursor, "freshness": page.freshness}


async def get_freshness(session: AsyncSession, *, tenant_id: str, view_name: str) -> dict[str, Any]:
    service = ReportService(session, tenant_id=tenant_id)
    return await service.get_freshness(view_name)


async def export_view(session: AsyncSession, *, tenant_id: str, app_user_id: str | None, report_name: str,
                      filters: Mapping[str, Any] | None = None, cursor: str | None = None,
                      limit: int = 500, format: str = "json", fields: str | None = None) -> tuple[str, str]:
    """Export one report; returns ``(body, media_type)``; report names are the catalogue aliases."""
    service = ReportService(session, tenant_id=tenant_id, app_user_id=app_user_id)
    return await service.export_view(report_name, filters=filters, cursor=cursor, limit=limit,
                                     format=format, fields=fields)


def view_metadata() -> list[dict[str, Any]]:
    return [
        {"view_name": spec.name, "view_number": spec.number, "purpose": spec.purpose,
         "materialized": spec.materialized, "cost_visible": spec.cost_visible, "order_key": spec.order_key}
        for spec in VIEWS_BY_NAME.values()
    ]


__all__ = ["CONTRACT", "VERSION", "VIEWS", "EVENTS", "JOBS", "REPORT_ALIASES", "endpoint_paths",
           "query_view", "get_freshness", "export_view", "view_metadata"]
