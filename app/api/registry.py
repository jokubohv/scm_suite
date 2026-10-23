"""Deterministic router registry.

Builders export an ``APIRouter`` from their module package; the integrator (or FS-00) adds one
``register_router(...)`` line. Builders must not edit this file.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

ROUTERS: list[APIRouter] = []


def register_router(router: APIRouter) -> APIRouter:
    """Register one router exactly once (idempotent by object identity)."""
    if router not in ROUTERS:
        ROUTERS.append(router)
    return router


def include_routers(app: FastAPI) -> None:
    """Include registered routers in a deterministic order."""
    for router in sorted(ROUTERS, key=lambda r: (r.prefix or "", id(r))):
        app.include_router(router)
