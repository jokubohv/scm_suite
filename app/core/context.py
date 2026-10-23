"""Request-scoped context values (correlation, tenant, actor).

Security/token parsing fills tenant/user context after auth; logging and audit use these accessors.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    correlation_id: str
    trace_id: str
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    request_id: str | None = None


_context: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar("request_context", default=None)


def get_request_context() -> RequestContext | None:
    return _context.get()


def set_request_context(ctx: RequestContext) -> contextvars.Token[RequestContext | None]:
    return _context.set(ctx)


def reset_request_context(token: contextvars.Token[RequestContext | None]) -> None:
    _context.reset(token)
