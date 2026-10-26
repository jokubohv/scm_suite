"""FastAPI application factory and liveness/readiness endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import authenticate_api_key, reject_empty_create_body
from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.registry import include_routers, register_router
from app.api.request_schemas import apply_request_schemas
from app.api.routers.consigned import router as consigned_router
from app.api.routers.costing import router as costing_router
from app.api.routers.counts import router as counts_router
from app.api.routers.extensions import router as extensions_router
from app.api.routers.inventory_ledger import router as inventory_ledger_router
from app.api.routers.item_master import router as item_master_router
from app.api.routers.manufacturing import router as manufacturing_router
from app.api.routers.platform import api_keys as platform_api_keys
from app.api.routers.platform import approval_actions as platform_approval_actions
from app.api.routers.platform import approvals as platform_approvals
from app.api.routers.platform import artifacts as platform_artifacts
from app.api.routers.platform import auth as platform_auth
from app.api.routers.platform import bootstrap as platform_bootstrap
from app.api.routers.platform import boundary as platform_boundary
from app.api.routers.platform import config as platform_config
from app.api.routers.platform import extras as platform_extras
from app.api.routers.platform import internal as platform_internal
from app.api.routers.platform import mcp as platform_mcp
from app.api.routers.platform import ops as platform_ops
from app.api.routers.platform import platform_admin as platform_admin
from app.api.routers.platform import rbac as platform_rbac
from app.api.routers.platform import sso as platform_sso
from app.api.routers.platform import tenants as platform_tenants
from app.api.routers.pricing import router as pricing_router
from app.api.routers.procurement import router as procurement_router
from app.api.routers.quality import router as quality_router
from app.api.routers.replenishment import router as replenishment_router
from app.api.routers.reporting.router import router as reporting_router
from app.api.routers.traceability import router as traceability_router
from app.api.routers.traceability_numbered import router as traceability_numbered_router
from app.api.routers.wms import router as wms_router
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
        # The runtime spec and docs are not public surfaces; the spec is served by the explicitly
        # API-key-gated route below. Disabled built-in URLs also prevent an unauthenticated bypass.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(reject_empty_create_body)],
    )
    async def _require_openapi_key(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        configured = settings.openapi_api_key.strip()
        is_prod = settings.env.strip().lower() in {"production", "prod"}
        # 1) deployment-wide OpenAPI key (for operators/tooling)
        if configured and len(configured) >= 32 and x_api_key and compare_digest(x_api_key, configured):
            return
        # 2) any active tenant API key (the tenant key is validated by prefix/hash and tenant status)
        if x_api_key:
            try:
                tenant_auth = await authenticate_api_key(x_api_key)
            except Exception:  # noqa: BLE001 - a broken key lookup must not make the spec endpoint 500
                tenant_auth = None
            if tenant_auth is not None:
                return
        # 3) dev/test remains usable without a configured key for local tooling
        if not configured:
            if not is_prod and not x_api_key:
                return
            if is_prod:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "OPENAPI_KEY_NOT_CONFIGURED", "message": "OpenAPI access is not configured"},
                )
        if configured and len(configured) < 32:
            raise HTTPException(
                status_code=503,
                detail={"code": "OPENAPI_KEY_INVALID", "message": "OpenAPI access key is misconfigured"},
            )
        raise HTTPException(
            status_code=401,
            detail={"code": "API_KEY_REQUIRED", "message": "a valid X-API-Key header is required"},
            headers={"WWW-Authenticate": "ApiKey"},
        )

    @app.get(f"{settings.api_prefix}/openapi.json", include_in_schema=False)
    async def openapi_document(_: None = Depends(_require_openapi_key)) -> JSONResponse:
        """Runtime OpenAPI document, gated by a dedicated API key in production."""
        return JSONResponse(
            app.openapi(),
            headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
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
    register_router(platform_bootstrap.router)
    register_router(platform_internal.router)
    register_router(platform_mcp.router)
    register_router(item_master_router)
    register_router(pricing_router)
    register_router(traceability_router)
    register_router(inventory_ledger_router)
    register_router(costing_router)
    register_router(traceability_numbered_router)
    register_router(counts_router)
    register_router(procurement_router)
    register_router(manufacturing_router)
    register_router(replenishment_router)
    register_router(wms_router)
    register_router(quality_router)
    register_router(consigned_router)
    register_router(extensions_router)
    register_router(reporting_router)
    include_routers(app)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "env": settings.env}

    @app.get("/readyz", tags=["platform"])
    async def readyz(response: Response) -> dict[str, object]:
        """Readiness probe: real dependency checks (DB + migration head, Redis)."""

        from redis.asyncio import from_url as redis_from_url

        from app.db.session import get_engine

        checks: dict[str, object] = {}
        try:
            async with get_engine().connect() as connection:
                await connection.execute(text("SELECT 1"))
                migration_head = await connection.scalar(
                    text("SELECT version_num FROM public.alembic_version")
                )
            checks["database"] = "ok"
            checks["migrations_head"] = migration_head
        except Exception as exc:  # noqa: BLE001 - readiness must never raise
            checks["database"] = "error"
            checks["database_error"] = type(exc).__name__
        redis_client = redis_from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
        )
        try:
            checks["redis"] = "ok" if await redis_client.ping() else "error"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = "error"
            checks["redis_error"] = type(exc).__name__
        finally:
            await redis_client.aclose()
        ready = checks.get("database") == "ok" and checks.get("redis") == "ok"
        response.status_code = 200 if ready else 503
        return {"status": "ok" if ready else "unavailable", "checks": checks}

    # Runtime request-body schemas: the routers still accept free-form dicts, but the OpenAPI
    # contract (and therefore MCP tool input schemas) carries the real fields/types/enums.
    original_openapi = app.openapi

    def _openapi_with_request_schemas() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original_openapi()
        apply_request_schemas(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi_with_request_schemas  # type: ignore[method-assign]
    return app


app = create_app()
