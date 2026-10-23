"""FastAPI application factory and liveness/readiness endpoints."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.registry import include_routers, register_router
from app.api.routers.costing import router as costing_router
from app.api.routers.inventory_ledger import router as inventory_ledger_router
from app.api.routers.item_master import router as item_master_router
from app.api.routers.platform import api_keys as platform_api_keys
from app.api.routers.platform import approval_actions as platform_approval_actions
from app.api.routers.platform import approvals as platform_approvals
from app.api.routers.platform import artifacts as platform_artifacts
from app.api.routers.platform import auth as platform_auth
from app.api.routers.platform import boundary as platform_boundary
from app.api.routers.platform import config as platform_config
from app.api.routers.platform import extras as platform_extras
from app.api.routers.platform import ops as platform_ops
from app.api.routers.platform import platform_admin as platform_admin
from app.api.routers.platform import rbac as platform_rbac
from app.api.routers.platform import sso as platform_sso
from app.api.routers.platform import tenants as platform_tenants
from app.api.routers.pricing import router as pricing_router
from app.api.routers.traceability import router as traceability_router
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Inventory Platform",
        version=__version__,
        description="Multi-tenant SCM inventory/manufacturing backend (plan v1.5.0).",
        lifespan=lifespan,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    register_router(platform_ops.router)
    register_router(platform_approvals.router)
    register_router(platform_approval_actions.router)
    register_router(platform_api_keys.router)
    register_router(platform_boundary.router)
    register_router(platform_artifacts.router)
    register_router(platform_rbac.router)
    register_router(platform_auth.router)
    register_router(platform_config.router)
    register_router(platform_extras.router)
    register_router(platform_sso.router)
    register_router(platform_tenants.router)
    register_router(platform_admin.router)
    register_router(item_master_router)
    register_router(pricing_router)
    register_router(traceability_router)
    register_router(inventory_ledger_router)
    register_router(costing_router)
    include_routers(app)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "env": settings.env}

    @app.get("/readyz", tags=["platform"])
    async def readyz() -> dict[str, object]:
        checks = {
            "database": "unknown" if not os.environ.get("DATABASE_URL") else "configured",
            "redis": "unknown" if not os.environ.get("REDIS_URL") else "configured",
        }
        return {"status": "ok", "checks": checks}

    @app.get("/internal/deps", tags=["platform"], include_in_schema=False)
    async def internal_deps() -> dict[str, object]:
        return {"version": __version__, "config": settings.public_dict()}

    return app


app = create_app()
