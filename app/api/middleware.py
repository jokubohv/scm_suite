"""Request correlation middleware: X-Request-ID, trace id and request-scoped context."""

from __future__ import annotations

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.context import RequestContext, reset_request_context, set_request_context
from app.core.logging import get_logger

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
log = get_logger("api.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        correlation_id = incoming if REQUEST_ID_RE.match(incoming) else f"req_{uuid.uuid4().hex}"
        traceparent = request.headers.get("traceparent", "")
        match = TRACEPARENT_RE.match(traceparent)
        trace_id = match.group(1) if match else uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        request.state.trace_id = trace_id
        token = set_request_context(RequestContext(correlation_id=correlation_id, trace_id=trace_id))
        started = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.failed", extra={"correlation_id": correlation_id, "event": "request.failed"})
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if response is not None:
                response.headers["X-Request-ID"] = correlation_id
                response.headers["X-Trace-ID"] = trace_id
            log.info(
                "request.completed",
                extra={"correlation_id": correlation_id, "duration_ms": duration_ms, "event": "request.completed"},
            )
            reset_request_context(token)
        return response
