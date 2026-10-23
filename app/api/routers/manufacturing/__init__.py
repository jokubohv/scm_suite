"""FS-09 manufacturing REST API package (exposes ``router`` + ``ROUTE_SCOPES``)."""

from app.api.routers.manufacturing.router import ROUTE_SCOPES, router

__all__ = ["ROUTE_SCOPES", "router"]
