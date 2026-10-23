"""Tenant/principal request-context placeholders.

Until FS-00 lands the real middleware, tests bind an explicit context with :func:`bind_context` so
services that read the current tenant can be unit-tested deterministically. FS-00 will replace the
storage with its request middleware/contextvars but keep these accessor names stable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.tests.utils.factories import Principal, Tenant


@dataclass(frozen=True)
class TenantContext:
    tenant_id: UUID
    tenant_code: str
    actor_id: UUID | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    data_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    is_platform_admin: bool = False
    request_id: str | None = None
    correlation_id: str | None = None

    @classmethod
    def from_principal(cls, principal: Principal, tenant: Tenant | None = None) -> TenantContext:
        return cls(
            tenant_id=principal.tenant_id,
            tenant_code=tenant.code if tenant is not None else "",
            actor_id=principal.id,
            scopes=principal.scopes,
            data_scopes=principal.data_scopes,
            is_platform_admin=principal.is_platform_admin,
        )

    def has_scope(self, scope: str) -> bool:
        return self.is_platform_admin or "platform.admin" in self.scopes or scope in self.scopes


CURRENT_TENANT: ContextVar[TenantContext | None] = ContextVar("current_tenant_context", default=None)


@contextmanager
def bind_context(context: TenantContext) -> Iterator[TenantContext]:
    """Bind a tenant context for the duration of the ``with`` block."""
    token: Token[TenantContext | None] = CURRENT_TENANT.set(context)
    try:
        yield context
    finally:
        CURRENT_TENANT.reset(token)


def current_context() -> TenantContext | None:
    return CURRENT_TENANT.get()


def require_context() -> TenantContext:
    context = CURRENT_TENANT.get()
    if context is None:
        raise RuntimeError("no tenant context bound; use app.tests.utils.context.bind_context(...)")
    return context


def current_tenant_id() -> UUID:
    return require_context().tenant_id


def clear_context() -> None:
    CURRENT_TENANT.set(None)


def context_metadata() -> dict[str, Any]:
    """Small helper for asserting audit/log fields without exposing secrets."""
    context = current_context()
    if context is None:
        return {}
    return {
        "tenant_id": str(context.tenant_id),
        "actor_id": str(context.actor_id) if context.actor_id else None,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
    }
