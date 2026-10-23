"""FS-12 quality-management API router (registered by the integration owner in app/api/main.py)."""

from app.api.routers.quality.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
