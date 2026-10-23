"""FS-03 storage + inventory-status registry API router package (exposes ``router`` for the integrator)."""

from __future__ import annotations

from app.api.routers.traceability.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
