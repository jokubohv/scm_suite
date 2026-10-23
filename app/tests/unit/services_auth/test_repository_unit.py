"""AuthRepository edge-case tests against the live integration database (skip when unavailable)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from app.core.models.platform import user_data_scope
from app.core.repositories.auth import AuthRepository, SessionRecord
from app.tests.integration.platform_auth.support import (
    create_identity,
    create_membership,
    create_sso_connection,
    create_tenant,
)

pytestmark = pytest.mark.integration


async def test_identity_lookup_and_missing(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-A")
    identity_id = await create_identity(db_session, email="Lookup@Example.com")
    await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="lookup@example.com")
    repository = AuthRepository(db_session)

    found = await repository.get_identity_by_email("LOOKUP@example.com")
    assert found is not None and found.id == identity_id
    assert found.email == "lookup@example.com"
    assert await repository.get_identity_by_email("missing@example.com") is None
    assert await repository.get_identity(uuid.uuid4()) is None


async def test_memberships_resolve_only_effective_roles_and_scopes(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-B")
    identity_id = await create_identity(db_session, email="effective@example.com")
    app_user_id = await create_membership(
        db_session,
        tenant_id=tenant_id,
        identity_id=identity_id,
        email="effective@example.com",
        scopes=("items.read", "items.create"),
    )
    # A second membership in another tenant whose role grant has already expired must not leak scopes.
    other_tenant = await create_tenant(db_session, code="UNIT-C")
    expired_user_id = await create_membership(
        db_session,
        tenant_id=other_tenant,
        identity_id=identity_id,
        email="effective@example.com",
        scopes=("inventory.adjustment.post",),
        effective_from=datetime.now(UTC) - timedelta(days=10),
        effective_to=datetime.now(UTC) - timedelta(days=1),
    )
    repository = AuthRepository(db_session)
    memberships = await repository.list_memberships(identity_id)
    assert len(memberships) == 2
    current = next(item for item in memberships if item.app_user_id == app_user_id)
    assert current.roles == ("admin",)
    assert current.scopes == frozenset({"items.read", "items.create"})
    expired = next(item for item in memberships if item.app_user_id == expired_user_id)
    assert expired.roles == ()
    assert expired.scopes == frozenset()

    assert await repository.effective_scopes(tenant_id, app_user_id) == frozenset({"items.read", "items.create"})
    membership = await repository.get_membership(tenant_id, app_user_id)
    assert membership is not None and membership.tenant_code == "UNIT-B"
    assert await repository.get_membership(tenant_id, uuid.uuid4()) is None


async def test_data_scopes_merge_role_and_user_entries(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-D")
    identity_id = await create_identity(db_session, email="datascope@example.com")
    app_user_id = await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="datascope@example.com")
    await db_session.execute(
        insert(user_data_scope).values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=app_user_id,
            scope_type="LOCATION",
            scope_value="LOC-1",
        )
    )
    repository = AuthRepository(db_session)
    assert await repository.data_scopes(tenant_id, app_user_id) == {"LOCATION": ("LOC-1",)}


async def test_session_lifecycle_and_replay_rotation(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-E")
    identity_id = await create_identity(db_session, email="session@example.com")
    app_user_id = await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="session@example.com")
    repository = AuthRepository(db_session)
    now = datetime.now(UTC)
    sid = uuid.uuid4()

    created = await repository.create_session(
        session_id=sid,
        tenant_id=tenant_id,
        app_user_id=app_user_id,
        identity_id=identity_id,
        refresh_token_hash="old" + "0" * 61,
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )
    assert created.id == sid
    loaded = await repository.get_session(sid)
    assert isinstance(loaded, SessionRecord) and loaded.is_active(now) is True

    rotated = await repository.rotate_session_refresh_hash(
        sid,
        old_hash="old" + "0" * 61,
        new_hash="new" + "0" * 61,
        issued_at=now + timedelta(seconds=10),
        expires_at=now + timedelta(days=30),
    )
    assert rotated is True
    # Replaying the previous digest fails atomically.
    assert (
        await repository.rotate_session_refresh_hash(
            sid,
            old_hash="old" + "0" * 61,
            new_hash="other" + "0" * 59,
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
        is False
    )
    assert await repository.revoke_session(sid, reason="logout") is True
    assert await repository.revoke_session(sid, reason="logout") is False  # idempotent
    assert (await repository.get_session(sid)).status == "REVOKED"


async def test_revoke_all_sessions_and_bump_token_version(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-F")
    identity_id = await create_identity(db_session, email="all@example.com")
    app_user_id = await create_membership(db_session, tenant_id=tenant_id, identity_id=identity_id, email="all@example.com")
    repository = AuthRepository(db_session)
    now = datetime.now(UTC)
    for _ in range(3):
        await repository.create_session(
            tenant_id=tenant_id,
            app_user_id=app_user_id,
            identity_id=identity_id,
            refresh_token_hash=uuid.uuid4().hex,
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    assert await repository.revoke_all_sessions(tenant_id=tenant_id, app_user_id=app_user_id, reason="logout_all") == 3
    assert await repository.bump_token_version(tenant_id=tenant_id, app_user_id=app_user_id) == 1
    assert await repository.bump_token_version(tenant_id=tenant_id, app_user_id=app_user_id) == 2


async def test_active_sso_connections_only_and_invitation_lookup(db_session) -> None:
    tenant_id = await create_tenant(db_session, code="UNIT-G")
    await create_sso_connection(db_session, tenant_id=tenant_id, email_domains=("unit-g.example",))
    await create_sso_connection(
        db_session, tenant_id=tenant_id, name="Disabled", status="DISABLED", issuer="https://sso-disabled.example", email_domains=("disabled.example",)
    )
    repository = AuthRepository(db_session)
    connections = await repository.list_active_sso_connections()
    ours = [connection for connection in connections if connection.tenant_id == tenant_id]
    assert len(ours) == 1
    assert ours[0].email_domains == ("unit-g.example",)
    assert await repository.get_invitation_by_token_hash("missing-token-hash") is None
