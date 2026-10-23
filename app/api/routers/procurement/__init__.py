"""FS-08 procurement API router package (exposes ``router`` and ``ROUTE_SCOPES``)."""

from __future__ import annotations

from app.api.routers.procurement.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
