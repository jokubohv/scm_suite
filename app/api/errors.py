"""problem+json error envelope and exception handlers (FS-00 API conventions)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_context
from app.core.security.errors import AuthenticationError, SecurityError

PROBLEM_MEDIA_TYPE = "application/problem+json"
log = logging.getLogger(__name__)


def problem_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> JSONResponse:
    ctx = get_request_context()
    raw_details = details or []
    if isinstance(raw_details, dict):
        detail_list: list[dict[str, Any]] = [raw_details] if raw_details else []
    else:
        detail_list = raw_details
    payload: dict[str, Any] = {
        "type": "about:blank",
        "title": code,
        "status": status_code,
        "code": code,
        "message": message,
        "details": detail_list,
        "correlation_id": ctx.correlation_id if ctx else None,
        "trace_id": ctx.trace_id if ctx else None,
    }
    response = JSONResponse(status_code=status_code, content=payload, media_type=PROBLEM_MEDIA_TYPE)
    if isinstance(details, dict):
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SecurityError)
    async def _security_error(request: Request, exc: SecurityError) -> JSONResponse:
        return problem_response(
            request,
            exc.http_status,
            exc.code,
            exc.message,
            dict(exc.details) if exc.details else None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        message = str(exc.detail)
        details: list[dict[str, Any]] | dict[str, Any] = []
        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("code", code))
            message = str(exc.detail.get("message", message))
            raw = exc.detail.get("details")
            if isinstance(raw, list | dict):
                details = raw
            elif raw:
                details = [{"detail": raw}]
        return problem_response(request, exc.status_code, code, message, details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(item.get("loc", [])), "msg": item.get("msg", ""), "type": item.get("type", "")} for item in exc.errors()]
        return problem_response(request, 422, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(AuthenticationError)
    async def _authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        return problem_response(request, exc.http_status, exc.code, exc.message, dict(exc.details) if exc.details else None)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return problem_response(request, 500, "INTERNAL_ERROR", "An internal error occurred")
