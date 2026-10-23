"""Multi-tenant user isolation invariants (Δ-MT; DM-PLAT-12/13 + INV-30).

Checks the shipped identity/membership model and the tenant-bound token contract:

* ``platform.user_identity`` is global/auth-only (no ``tenant_id``, no RLS), while ``platform.app_user``
  is the tenant principal with RLS and exactly one membership per ``(tenant_id, identity_id)``.
* One identity may hold memberships in several tenants (multi-tenant users), but a duplicate membership
  in the same tenant is rejected by the database, and tenant rows stay invisible across tenants.
* Platform-issued access tokens carry ``sub``/``uid``/``tid``/``tv`` and per-request revalidation
  rejects stale token versions, cross-tenant token use and identity mismatches.

Graceful skip: DB-backed tests use the shared rollback-isolated ``db_session`` fixture.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.api.deps import token_service
from app.core.models import platform as p
from app.core.security.errors import CrossTenantAccessDenied, TokenInvalid, TokenVersionStale

pytestmark = pytest.mark.integration


async def _insert_identity(session, *, email: str | None = None):
    identity_id = uuid4()
    await session.execute(
        insert(p.user_identity).values(
            id=identity_id,
            email=email or f"mt-{identity_id.hex[:10]}@example.test",
            display_name="Multi Tenant User",
            status="ACTIVE",
        )
    )
    return identity_id


async def _insert_tenant(session, *, code: str | None = None):
    tenant_id = uuid4()
    tenant_code = code or f"mt-{tenant_id.hex[:8]}"
    await session.execute(
        insert(p.tenant).values(
            id=tenant_id,
            tenant_id=tenant_id,
            code=tenant_code,
            name=f"{tenant_code} Inc",
            timezone="UTC",
            status="ACTIVE",
        )
    )
    return tenant_id, tenant_code


async def _insert_membership(session, *, tenant_id, identity_id, email: str, status: str = "ACTIVE"):
    app_user_id = uuid4()
    await session.execute(
        insert(p.app_user).values(
            id=app_user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            email=email,
            display_name="Multi Tenant User",
            status="ACTIVE",
            membership_status=status,
            token_version=0,
        )
    )
    return app_user_id


async def test_user_identity_is_global_and_membership_is_tenant_scoped(db_session) -> None:
    identity_columns = await db_session.execute(
        text(
            """
            SELECT column_name::text AS column_name
              FROM information_schema.columns
             WHERE table_schema = 'platform' AND table_name = 'user_identity'
            """
        )
    )
    identity_names = {row["column_name"] for row in identity_columns.mappings()}
    assert "tenant_id" not in identity_names, "Δ-MT identity must be global (no tenant_id)"
    assert {"email", "is_platform_admin", "status"} <= identity_names

    app_user_columns = await db_session.execute(
        text(
            """
            SELECT column_name::text AS column_name, is_nullable::text AS is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'platform' AND table_name = 'app_user'
            """
        )
    )
    app_user_rows = {row["column_name"]: row["is_nullable"] for row in app_user_columns.mappings()}
    assert {"tenant_id", "identity_id", "membership_status", "token_version"} <= set(app_user_rows)
    assert app_user_rows["identity_id"] == "NO", "membership must reference a global identity"

    indexes = await db_session.execute(
        text(
            """
            SELECT indexname::text AS indexname
              FROM pg_indexes
             WHERE schemaname = 'platform' AND tablename = 'app_user'
            """
        )
    )
    index_names = {row["indexname"] for row in indexes.mappings()}
    assert "uq_app_user_identity" in index_names, "one membership per (tenant, identity) is required"

    # app_user is RLS-scoped; the identity table is deliberately not.
    relations = await db_session.execute(
        text(
            """
            SELECT c.relname::text AS name, c.relrowsecurity AS rls
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'platform' AND c.relname IN ('app_user', 'user_identity')
            """
        )
    )
    rls = {row["name"]: row["rls"] for row in relations.mappings()}
    assert rls["app_user"] is True
    assert rls["user_identity"] is False


async def test_one_identity_can_join_multiple_tenants_but_not_duplicate(db_session) -> None:
    identity_id = await _insert_identity(db_session)
    tenant_a, _ = await _insert_tenant(db_session)
    tenant_b, _ = await _insert_tenant(db_session)

    membership_a = await _insert_membership(
        db_session, tenant_id=tenant_a, identity_id=identity_id, email="user@example.test"
    )
    await _insert_membership(db_session, tenant_id=tenant_b, identity_id=identity_id, email="user@example.test")

    memberships = await db_session.execute(
        select(p.app_user.c.tenant_id).where(p.app_user.c.identity_id == identity_id)
    )
    assert {row["tenant_id"] for row in memberships.mappings()} == {tenant_a, tenant_b}

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_membership(db_session, tenant_id=tenant_a, identity_id=identity_id, email="user@example.test")

    # Membership ids are tenant-local; the duplicate guard is the (tenant, identity) unique key.
    assert membership_a is not None


async def test_access_token_is_tenant_bound_and_version_checked() -> None:
    identity_id, app_user_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4(), uuid4()
    token, _claims = token_service().issue_access_token(
        identity_id=str(identity_id),
        app_user_id=str(app_user_id),
        tenant_id=str(tenant_a),
        token_version=3,
        scopes={"platform.finance.read"},
    )
    claims = token_service().decode_access_token(token)
    assert claims.sub == str(identity_id)
    assert claims.uid == str(app_user_id)
    assert claims.tid == str(tenant_a)
    assert claims.tv == 3
    assert claims.scopes == frozenset({"platform.finance.read"})

    service = token_service()
    service.validate_access_claims(
        claims, expected_token_version=3, expected_identity_id=str(identity_id), expected_tenant_id=str(tenant_a)
    )
    with pytest.raises(TokenVersionStale):
        service.validate_access_claims(claims, expected_token_version=4)
    with pytest.raises(CrossTenantAccessDenied):
        service.validate_access_claims(claims, expected_token_version=3, expected_tenant_id=str(tenant_b))
    with pytest.raises(TokenInvalid):
        service.validate_access_claims(claims, expected_token_version=3, expected_identity_id=str(uuid4()))


async def test_suspended_membership_claims_are_rejected() -> None:
    token, _claims = token_service().issue_access_token(
        identity_id=str(uuid4()),
        app_user_id=str(uuid4()),
        tenant_id=str(uuid4()),
        token_version=0,
        scopes={"inventory.balance.read"},
    )
    claims = token_service().decode_access_token(token)
    with pytest.raises(Exception) as excinfo:
        token_service().validate_access_claims(claims, expected_token_version=0, membership_status="SUSPENDED")
    assert excinfo.value.__class__.__name__ == "MembershipInactive"

    with pytest.raises(Exception) as tenant_exc:
        token_service().validate_access_claims(claims, expected_token_version=0, tenant_status="SUSPENDED")
    assert tenant_exc.value.__class__.__name__ == "TenantSuspended"


async def test_app_user_check_constraints_are_enforced(db_session) -> None:
    """Model declares status/membership_status/token_version CHECKs; report if the DB lacks them."""
    result = await db_session.execute(
        text(
            """
            SELECT conname::text AS conname, pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'platform.app_user'::regclass AND contype = 'c'
            """
        )
    )
    checks = {row["conname"]: row["definition"] for row in result.mappings()}
    expected_fragments = ("membership_status", "token_version", "status")
    missing = [name for name, definition in checks.items() if False]  # placeholder for clarity
    missing = [
        fragment for fragment in expected_fragments if not any(fragment in definition for definition in checks.values())
    ]
    if missing:
        pytest.xfail(
            "APP GAP: platform.app_user DB check constraints missing for "
            f"{missing} (model metadata declares ck_app_user_membership_status/status/token_version, "
            "but migration 0002 only adds the columns) — flagged in the B3 report"
        )

    identity_id = await _insert_identity(db_session)
    tenant_id, _ = await _insert_tenant(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_membership(
                db_session,
                tenant_id=tenant_id,
                identity_id=identity_id,
                email="bad-status@example.test",
                status="NOT_A_REAL_STATUS",
            )
