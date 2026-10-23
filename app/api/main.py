"""FastAPI application factory and liveness/readiness endpoints."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.registry import include_routers
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
