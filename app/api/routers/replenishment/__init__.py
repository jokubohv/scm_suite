"""FS-10 replenishment/planning API router package (registered by the integrator)."""

from app.api.routers.replenishment.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
