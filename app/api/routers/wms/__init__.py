"""FS-11 WMS API router package (exposes ``router`` and ``ROUTE_SCOPES``)."""

from __future__ import annotations

from app.api.routers.wms.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
