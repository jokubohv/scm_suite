"""Shared fixtures/helpers for the pure FS-00 security unit suites (no DB/network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.security.authn import SsoConnectionStatus, SsoConnectionView, SsoProtocol
from app.core.security.scopes import PermissionCatalogue, ScopeRegistry, load_catalogue

# Loaded once per test session; read-only catalogue snapshot (plan v1.5.0).
CATALOGUE: PermissionCatalogue = load_catalogue()
REGISTRY = ScopeRegistry(CATALOGUE)

HMAC_SECRET = "unit-test-hmac-secret-0123456789abcdef"  # >= 32 bytes (RFC 7518)
PEPPER = "unit-test-api-key-pepper"


class StepClock:
    """Deterministic UTC clock (callable) for token/idempotency/SSO tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def make_connection(
    *,
    connection_id: str = "conn-1",
    tenant_id: str = "tenant-1",
    tenant_code: str = "TEN0001",
    tenant_name: str = "Tenant One",
    protocol: SsoProtocol = SsoProtocol.OIDC,
    status: SsoConnectionStatus = SsoConnectionStatus.ACTIVE,
    issuer: str = "https://sso-tenant1.auth.example.com",
    client_id: str = "inventory-platform",
    email_domains: tuple[str, ...] = ("example.com",),
    is_enforced: bool = False,
    jit_provisioning: bool = False,
    default_role_keys: tuple[str, ...] = ("readonly",),
    group_claim_name: str = "groups",
) -> SsoConnectionView:
    return SsoConnectionView(
        id=connection_id,
        tenant_id=tenant_id,
        tenant_code=tenant_code,
        tenant_name=tenant_name,
        protocol=protocol,
        status=status,
        dex_issuer_url=issuer,
        client_id=client_id,
        email_domains=email_domains,
        is_enforced=is_enforced,
        jit_provisioning=jit_provisioning,
        default_role_keys=default_role_keys,
        group_claim_name=group_claim_name,
    )


def role_scope_sets() -> dict[str, frozenset[str]]:
    return {key: template.scopes for key, template in CATALOGUE.templates_by_key.items()}


def assignable_role_keys() -> tuple[str, ...]:
    return tuple(CATALOGUE.templates_by_key)


__all__ = [
    "CATALOGUE",
    "HMAC_SECRET",
    "PEPPER",
    "REGISTRY",
    "StepClock",
    "assignable_role_keys",
    "make_connection",
    "role_scope_sets",
]
