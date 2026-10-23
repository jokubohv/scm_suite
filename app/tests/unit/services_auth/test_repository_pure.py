"""Pure repository helper/record tests (no DB; run without Postgres)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.core.repositories.auth import (
    MembershipRecord,
    SessionRecord,
    hash_refresh_token,
)


def _membership(**overrides: object) -> MembershipRecord:
    values: dict[str, object] = {
        "app_user_id": uuid.uuid4(),
        "identity_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "tenant_code": "TEN-1",
        "tenant_name": "Tenant 1",
        "tenant_status": "ACTIVE",
        "email": "user@example.com",
        "display_name": "User",
        "status": "ACTIVE",
        "membership_status": "ACTIVE",
        "is_service_account": False,
        "token_version": 0,
        "joined_at": None,
        "last_login_at": None,
    }
    values.update(overrides)
    return MembershipRecord(**values)  # type: ignore[arg-type]


def _session(**overrides: object) -> SessionRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "app_user_id": uuid.uuid4(),
        "identity_id": uuid.uuid4(),
        "refresh_token_hash": "a" * 64,
        "status": "ACTIVE",
        "issued_at": now,
        "expires_at": now + timedelta(days=30),
        "revoked_at": None,
        "revoked_reason": None,
    }
    values.update(overrides)
    return SessionRecord(**values)  # type: ignore[arg-type]


def test_hash_refresh_token_is_deterministic_and_not_plaintext() -> None:
    token = "a-refresh-token-value"
    first = hash_refresh_token(token)
    assert first == hash_refresh_token(token)
    assert first != token
    assert len(first) == 64
    assert hash_refresh_token(token + "x") != first


def test_membership_active_requires_membership_and_tenant_active() -> None:
    assert _membership().is_active is True
    assert _membership(status="INACTIVE").is_active is False
    assert _membership(membership_status="SUSPENDED").is_active is False
    assert _membership(tenant_status="SUSPENDED").is_active is False


def test_session_active_respects_status_and_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    active = _session()
    assert active.is_active(now) is True
    assert active.is_active(now + timedelta(days=31)) is False
    assert _session(status="REVOKED").is_active(now) is False
    assert _session(status="EXPIRED").is_active(now) is False
