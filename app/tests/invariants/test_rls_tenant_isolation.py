"""RLS tenant-isolation invariants for the FS-00 platform schema (DATA-MODEL §5.1, INV-25/INV-30).

Two layers of evidence:

1. **Catalog invariants (always run when a database is available):** every platform table that owns a
   ``tenant_id`` must have row-level security enabled and carry the two reviewed policies
   (``tenant_isolation`` for reads/writes, ``system_tenant_read`` for the reserved SYSTEM tenant).
   ``platform.user_identity`` is the one documented auth-only exception (global, no ``tenant_id``).
2. **Behavioural cross-tenant probe (skipped when the DB role cannot create a probe role):** a
   non-owner, non-superuser role with ``app.tenant_id`` set must see only its own tenant's rows; an
   insert carrying another tenant's id must be rejected by the policy ``WITH CHECK`` clause. The probe
   runs entirely inside the rollback-isolated ``db_session`` transaction, so no real state is created.

Graceful skip: when no integration database is configured the shared ``db_session`` fixture skips.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.models.platform import ALL_TABLES

pytestmark = pytest.mark.integration

SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"

#: Tenant-owned tables = tables whose metadata declares ``tenant_id`` (user_identity is global/auth-only).
EXPECTED_TENANT_TABLES: frozenset[str] = frozenset(table.name for table in ALL_TABLES if "tenant_id" in table.c)

#: Documented exception: global identity is auth-service-only and deliberately not RLS-scoped.
GLOBAL_EXCEPTION_TABLES = frozenset({"user_identity"})


async def _relation_rows(session):
    result = await session.execute(
        text(
            """
            SELECT c.relname::text AS name,
                   c.relkind::text AS kind,
                   c.relrowsecurity AS rls
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'platform'
               AND c.relkind IN ('r', 'p')
            """
        )
    )
    return {row["name"]: dict(row) for row in result.mappings()}


async def _policy_rows(session):
    result = await session.execute(
        text(
            """
            SELECT tablename::text AS tablename,
                   policyname::text AS policyname,
                   cmd::text AS cmd,
                   qual::text AS qual,
                   with_check::text AS with_check
              FROM pg_policies
             WHERE schemaname = 'platform'
            """
        )
    )
    policies: dict[str, list[dict]] = {}
    for row in result.mappings():
        policies.setdefault(row["tablename"], []).append(dict(row))
    return policies


def test_expected_tenant_table_set_is_derived_from_metadata() -> None:
    """Guard against silent exclusions: every tenant_id table in the model must be expected."""
    assert "tenant" in EXPECTED_TENANT_TABLES
    assert EXPECTED_TENANT_TABLES.isdisjoint(GLOBAL_EXCEPTION_TABLES)
    # Partitioned append-only parents are tenant-owned and must be covered.
    assert {"audit_event", "outbox_event", "finance_journal_intent"} <= EXPECTED_TENANT_TABLES


async def test_every_tenant_table_has_rls_and_tenant_isolation_policy(db_session) -> None:
    relations = await _relation_rows(db_session)
    policies = await _policy_rows(db_session)

    missing = sorted(EXPECTED_TENANT_TABLES - set(relations))
    assert missing == [], f"tenant tables missing from the platform schema: {missing}"

    rls_disabled = sorted(name for name in EXPECTED_TENANT_TABLES if not relations[name]["rls"])
    assert rls_disabled == [], "row-level security is not enabled on tenant-owned tables: " + ", ".join(rls_disabled)

    missing_tenant_policies = sorted(
        name
        for name in EXPECTED_TENANT_TABLES
        if not any(p["policyname"] == "tenant_isolation" for p in policies.get(name, []))
    )
    assert missing_tenant_policies == [], "tenant_isolation policy missing on: " + ", ".join(missing_tenant_policies)

    missing_system_policies = sorted(
        name
        for name in EXPECTED_TENANT_TABLES
        if not any(p["policyname"] == "system_tenant_read" for p in policies.get(name, []))
    )
    assert missing_system_policies == [], "system_tenant_read policy missing on: " + ", ".join(missing_system_policies)

    problems: list[str] = []
    for name in sorted(EXPECTED_TENANT_TABLES):
        tenant_policies = [p for p in policies.get(name, []) if p["policyname"] == "tenant_isolation"]
        for policy in tenant_policies:
            if "app.tenant_id" not in (policy["qual"] or ""):
                problems.append(f"{name}: tenant_isolation USING does not reference app.tenant_id")
            if "app.tenant_id" not in (policy["with_check"] or ""):
                problems.append(f"{name}: tenant_isolation WITH CHECK does not reference app.tenant_id")
        system_policies = [p for p in policies.get(name, []) if p["policyname"] == "system_tenant_read"]
        if system_policies and any(p["cmd"] != "SELECT" for p in system_policies):
            problems.append(f"{name}: system_tenant_read must be SELECT-only")
        if system_policies and SYSTEM_TENANT_ID not in (system_policies[0]["qual"] or ""):
            problems.append(f"{name}: system_tenant_read does not pin the SYSTEM tenant id")
    assert problems == [], problems


async def test_user_identity_is_global_auth_only_exception(db_session) -> None:
    columns = await db_session.execute(
        text(
            """
            SELECT column_name::text AS column_name
              FROM information_schema.columns
             WHERE table_schema = 'platform' AND table_name = 'user_identity'
            """
        )
    )
    names = {row["column_name"] for row in columns.mappings()}
    assert "tenant_id" not in names, "user_identity must be global (no tenant_id)"
    assert {"email", "is_platform_admin", "password_hash", "status"} <= names

    relations = await _relation_rows(db_session)
    assert "user_identity" in relations
    assert relations["user_identity"]["rls"] is False, "user_identity must not be RLS-scoped"

    rls_tables_without_tenant_id = sorted(
        name
        for name, row in relations.items()
        if row["rls"] and name not in EXPECTED_TENANT_TABLES and row["kind"] == "p"
    )
    assert rls_tables_without_tenant_id == []


async def _probe_role_privileges(session) -> bool:
    result = await session.execute(
        text(
            """
            SELECT r.rolsuper OR r.rolcreaterole AS can_create_role
              FROM pg_roles r
             WHERE r.rolname = current_user
            """
        )
    )
    row = result.mappings().first()
    return bool(row and row["can_create_role"])


async def test_cross_tenant_probe_is_denied_by_policy(db_session) -> None:
    """Non-owner probe role: GUC tenant sees only its own rows; cross-tenant insert is rejected.

    Requires CREATEROLE (or superuser) so the test can create a temporary, non-owner login role inside
    the rollback transaction. Where the environment cannot provide that, the probe skips with a reason;
    the catalog checks above still prove the policies exist.
    """
    can_create_role = await _probe_role_privileges(db_session)
    if not can_create_role:
        pytest.skip("cross-tenant probe requires CREATEROLE/superuser to create a non-owner probe role")

    tenant_a, tenant_b = uuid4(), uuid4()
    code_a, code_b = f"rls-a-{tenant_a.hex[:8]}", f"rls-b-{tenant_b.hex[:8]}"
    role = f"rls_probe_{uuid4().hex[:8]}"

    await db_session.execute(
        text(
            """
            INSERT INTO platform.tenant (id, tenant_id, code, name, timezone, status, version)
            VALUES (:a, :a, :code_a, 'RLS Tenant A', 'UTC', 'ACTIVE', 1),
                   (:b, :b, :code_b, 'RLS Tenant B', 'UTC', 'ACTIVE', 1)
            """
        ),
        {"a": tenant_a, "b": tenant_b, "code_a": code_a, "code_b": code_b},
    )

    # The table owner (or a superuser) bypasses RLS, which is why the shipped policies are the
    # defence-in-depth backstop for the non-owner application role.
    owner_codes = await db_session.execute(
        text("SELECT code FROM platform.tenant WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
    )
    assert {row["code"] for row in owner_codes.mappings()} == {code_a, code_b}

    await db_session.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    await db_session.execute(text(f'GRANT USAGE ON SCHEMA platform TO "{role}"'))
    await db_session.execute(text(f'GRANT SELECT, INSERT ON platform.tenant TO "{role}"'))

    await db_session.execute(text(f'SET LOCAL ROLE "{role}"'))
    await db_session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_a)})
    visible = await db_session.execute(text("SELECT code FROM platform.tenant ORDER BY code"))
    visible_codes = {row["code"] for row in visible.mappings()}
    assert code_b not in visible_codes, "probe role must not see another tenant's row"
    assert code_a in visible_codes, "probe role must see its own tenant row"
    assert visible_codes <= {code_a, "SYSTEM"}, f"unexpected visible tenants: {sorted(visible_codes)}"

    # WITH CHECK: inserting a row for tenant B while the GUC says tenant A must be rejected.
    with pytest.raises(DBAPIError, match="row-level security|violates row-level security policy"):
        cross_tenant = uuid4()
        await db_session.execute(
            text(
                """
                INSERT INTO platform.tenant (id, tenant_id, code, name, timezone, status, version)
                VALUES (:id, :id, :code, 'Cross Tenant', 'UTC', 'ACTIVE', 1)
                """
            ),
            {"id": cross_tenant, "code": f"rls-x-{cross_tenant.hex[:8]}"},
        )
