"""Reporting boundary DTOs: typed page envelope with freshness metadata.

Report rows are dynamic (view column allow-lists), so the envelope is strongly typed while each row
remains an open ``dict[str, Any]`` of JSON-safe scalars — never an untyped top-level response.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from app.api.schemas.common import APIModel


class ReportingDTO(APIModel):
    """Forward-compatible reporting boundary base (declared fields stay typed)."""

    model_config = ConfigDict(extra="allow", from_attributes=True, populate_by_name=True)


class FreshnessOut(ReportingDTO):
    view_name: str | None = None
    materialized: bool | None = None
    last_refreshed_at: str | None = None
    refresh_status: str | None = None
    max_staleness_seconds: int | None = None
    degraded: bool | None = None


class ReportPage(ReportingDTO):
    """Paginated report envelope: dynamic rows + cursor + mandatory freshness metadata."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None
    freshness: FreshnessOut


class ReportCatalogueEntry(ReportingDTO):
    view_name: str
    view_number: str | None = None
    purpose: str | None = None
    materialized: bool | None = None
    cost_visible: bool | None = None
    order_key: str | None = None
    freshness: FreshnessOut | None = None


class ReportCatalogue(ReportingDTO):
    contract: str
    version: str
    items: list[ReportCatalogueEntry]
    total: int | None = None


class ReportExportOut(ReportingDTO):
    """Non-JSON export descriptor (kept for OpenAPI completeness; export returns a Response)."""

    media_type: str | None = None
    row_count: int | None = None


__all__ = [
    "FreshnessOut",
    "ReportCatalogue",
    "ReportCatalogueEntry",
    "ReportExportOut",
    "ReportPage",
    "ReportingDTO",
]
