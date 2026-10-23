"""FS-06 numbered-inventory API router (registered by the integration owner in app/api/main.py)."""

from app.api.routers.traceability_numbered.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
