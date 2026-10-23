"""Read-only live schema checks for the FS-00 platform tables (graceful skip without DB)."""

from __future__ import annotations

import asyncio
import os

import pytest

DEFAULT_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory"


def _raw_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL") or DEFAULT_URL
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _try_connect():
    import asyncpg

    try:
        return await asyncpg.connect(_raw_url())
    except (OSError, asyncpg.PostgresError):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_platform_schema_present() -> None:
    async def check():
        conn = await _try_connect()
        if conn is None:
            pytest.skip("database unavailable")
        try:
            head = await conn.fetchval("select version_num from public.alembic_version")
            tables = await conn.fetchval(
                "select count(*) from pg_tables where schemaname='platform' and tablename not like '%\\_20%'"
            )
            assert tables == 34, tables
            assert head in {"0003_platform_events", "0004_item_master", "0005_item_master_uom", "0006_pricing", "0007_traceability_registry", "0008_ledger_core", "0009_ledger_commitment_guards", "0010_ledger_balances", "0011_costing", "0012_costing_standard", "0013_numbered_inventory", "0014_counts", "0015_procurement_vendors", "0016_procurement_ordering", "0017_procurement_billing", "0018_manufacturing_master", "0019_manufacturing_routing", "0020_manufacturing_execution", "0021_planning", "0022_planning_atp", "0023_wms_setup", "0024_wms_inbound", "0025_wms_execution", "0026_quality", "0027_consigned", "0028_extensions", "0029_reporting_views", "0030_rls_grants_verify"}
        finally:
            await conn.close()
    _run(check())


def test_rls_partition_and_trigger_guards() -> None:
    async def check():
        conn = await _try_connect()
        if conn is None:
            pytest.skip("database unavailable")
        try:
            rls = await conn.fetchval(
                "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='platform' and c.relrowsecurity"
            )
            policies = await conn.fetchval("select count(*) from pg_policies where schemaname='platform'")
            assert rls >= 32 and policies >= 64
            partitioned = {r["relname"] for r in await conn.fetch(
                "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='platform' and c.relkind='p'"
            )}
            assert {"audit_event", "outbox_event"} <= partitioned
            append_triggers = await conn.fetchval(
                "select count(*) from pg_trigger t join pg_class c on c.oid=t.tgrelid "
                "join pg_namespace n on n.oid=c.relnamespace where n.nspname='platform' "
                "and t.tgname like '%append_only%'"
            )
            assert append_triggers >= 5
        finally:
            await conn.close()
    _run(check())


def test_seed_rows_and_identity_columns() -> None:
    async def check():
        conn = await _try_connect()
        if conn is None:
            pytest.skip("database unavailable")
        try:
            assert await conn.fetchval("select count(*) from platform.tenant") >= 1
            assert await conn.fetchval("select count(*) from platform.currency") >= 10
            assert await conn.fetchval("select count(*) from platform.job_definition") >= 4
            cols = {r["column_name"] for r in await conn.fetch(
                "select column_name from information_schema.columns where table_schema='platform' and table_name='app_user'"
            )}
            assert {"identity_id", "membership_status", "token_version", "joined_at"} <= cols
            sso_tables = {r["tablename"] for r in await conn.fetch(
                "select tablename from pg_tables where schemaname='platform'"
            )}
            assert {"tenant_sso_connection", "user_identity_link", "sso_group_role_map", "user_session", "tenant_invitation"} <= sso_tables
        finally:
            await conn.close()
    _run(check())


def test_append_only_triggers_reject_mutation() -> None:
    async def check():
        conn = await _try_connect()
        if conn is None:
            pytest.skip("database unavailable")
        import asyncpg

        try:
            async with conn.transaction():
                system = "00000000-0000-0000-0000-000000000000"
                row_id = "11111111-1111-1111-1111-111111111111"
                await conn.execute(
                    "insert into platform.finance_journal_intent "
                    "(id, tenant_id, intent_line_no, source_doc_type, posting_rule_id, account_ref, "
                    " debit_amount_base, credit_amount_base, currency_code, status) "
                    "values ($1::uuid,$2::uuid,1,'INTEGRATION_TEST','RCPT','ACC',1,0,'USD','RECORDED')",
                    row_id, system,
                )
                with pytest.raises(asyncpg.PostgresError):
                    await conn.execute("update platform.finance_journal_intent set account_ref='X'")
                await conn.execute("rollback")
        finally:
            await conn.close()
    _run(check())
