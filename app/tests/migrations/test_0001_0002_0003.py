"""FS-00 migration tests: empty-DB upgrade/downgrade round-trip + schema/RLS/seed checks.

The live test creates a throwaway database (``inv_mig_<uuid>``) so it never mutates the shared dev
database; when Postgres is unavailable (or the role cannot CREATE DATABASE) the tests skip
gracefully. ``chain_guard`` and metadata checks run everywhere.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:5432/inventory"
EXPECTED_TABLES = (
    "api_key", "app_user", "approval_action", "approval_definition", "approval_request",
    "attachment_metadata", "audit_event", "currency", "custom_field_def", "custom_field_value",
    "document_link", "event_consumer_checkpoint", "exchange_rate", "feature_flag",
    "finance_journal_intent", "fiscal_period", "idempotency_key", "job_definition", "job_run",
    "number_sequence", "outbox_event", "permission_scope", "role", "role_permission",
    "sso_group_role_map", "tenant", "tenant_invitation", "tenant_setting", "tenant_sso_connection",
    "user_data_scope", "user_identity", "user_identity_link", "user_role", "user_session",
)


def _raw_url() -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL") or DEFAULT_URL
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _admin_url() -> str:
    base = _raw_url()
    prefix, _, _db = base.rpartition("/")
    return f"{prefix}/postgres"


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["ALEMBIC_DATABASE_URL"] = url
    env.pop("DATABASE_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(url)


def test_chain_guard_clean() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.migrations.chain_guard", "--json"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0001_platform_core" in result.stdout and "0003_platform_events" in result.stdout


def test_metadata_groups_and_table_count() -> None:
    from app.core.models.platform import (
        ALL_TABLES,
        REVISION_0001_TABLES,
        REVISION_0002_TABLES,
        REVISION_0003_TABLES,
        metadata,
    )

    assert len(ALL_TABLES) == 34
    assert len(metadata.tables) == 34
    assert (len(REVISION_0001_TABLES), len(REVISION_0002_TABLES), len(REVISION_0003_TABLES)) == (21, 8, 5)
    assert {table.name for table in ALL_TABLES} == set(EXPECTED_TABLES)
    # Global exceptions follow DATA-MODEL §1.2: currency has no id, user_identity has no tenant_id.
    currency = metadata.tables["platform.currency"]
    assert "id" not in currency.c and "tenant_id" in currency.c
    identity = metadata.tables["platform.user_identity"]
    assert "tenant_id" not in identity.c


def test_upgrade_downgrade_roundtrip_on_empty_database() -> None:
    import asyncpg

    admin = _admin_url()
    db_name = f"inv_mig_{uuid.uuid4().hex[:12]}"
    temp_url = _raw_url().rsplit("/", 1)[0] + f"/{db_name}"

    async def _setup() -> bool:
        try:
            conn = await asyncpg.connect(admin)
        except (OSError, asyncpg.PostgresError):
            return False
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            return True
        except asyncpg.PostgresError:
            return False
        finally:
            await conn.close()

    try:
        created = asyncio.run(_setup())
    except Exception:  # pragma: no cover - defensive
        created = False
    if not created:
        pytest.skip("Postgres unavailable or role cannot CREATE DATABASE; live migration test skipped")

    async def _drop() -> None:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        finally:
            await conn.close()

    async def _inspect() -> dict:
        conn = await _connect(temp_url)
        try:
            head = await conn.fetchval("select version_num from public.alembic_version")
            tables = {
                row["tablename"]
                for row in await conn.fetch(
                    "select tablename from pg_tables where schemaname='platform' "
                    "and tablename not like '%\\_20%'"
                )
            }
            partitioned = {
                row["relname"]
                for row in await conn.fetch(
                    "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                    "where n.nspname='platform' and c.relkind='p'"
                )
            }
            rls = await conn.fetchval(
                "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='platform' and c.relrowsecurity"
            )
            policies = await conn.fetchval("select count(*) from pg_policies where schemaname='platform'")
            seeds = {
                "tenants": await conn.fetchval("select count(*) from platform.tenant"),
                "currencies": await conn.fetchval("select count(*) from platform.currency"),
                "jobs": await conn.fetchval("select count(*) from platform.job_definition"),
            }
            columns = {
                row["column_name"]
                for row in await conn.fetch(
                    "select column_name from information_schema.columns "
                    "where table_schema='platform' and table_name='app_user'"
                )
            }
            return {"head": head, "tables": tables, "partitioned": partitioned, "rls": rls, "policies": policies, "seeds": seeds, "columns": columns}
        finally:
            await conn.close()

    try:
        upgrade = _run_alembic(temp_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
        state = asyncio.run(_inspect())
        assert state["head"] in {"0003_platform_events", "0004_item_master", "0005_item_master_uom", "0006_pricing", "0007_traceability_registry", "0008_ledger_core", "0009_ledger_commitment_guards", "0010_ledger_balances", "0011_costing", "0012_costing_standard", "0013_numbered_inventory", "0014_counts", "0015_procurement_vendors", "0016_procurement_ordering", "0017_procurement_billing", "0018_manufacturing_master", "0019_manufacturing_routing", "0020_manufacturing_execution", "0021_planning", "0022_planning_atp", "0023_wms_setup", "0024_wms_inbound", "0025_wms_execution", "0026_quality", "0027_consigned", "0028_extensions", "0029_reporting_views", "0030_rls_grants_verify", "0031_count_post_txn_idx", "0032_currency_global_writes"}
        assert set(EXPECTED_TABLES) <= state["tables"]
        assert state["partitioned"] == {"audit_event", "outbox_event"}
        assert state["rls"] >= 32
        assert state["policies"] >= 64
        assert state["seeds"]["tenants"] == 1 and state["seeds"]["currencies"] == 10 and state["seeds"]["jobs"] >= 4
        assert {"identity_id", "membership_status", "token_version"} <= state["columns"]

        downgrade = _run_alembic(temp_url, "downgrade", "base")
        assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
        reupgrade = _run_alembic(temp_url, "upgrade", "head")
        assert reupgrade.returncode == 0, reupgrade.stdout + reupgrade.stderr
        final = asyncio.run(_inspect())
        assert final["head"] in {"0003_platform_events", "0004_item_master", "0005_item_master_uom", "0006_pricing", "0007_traceability_registry", "0008_ledger_core", "0009_ledger_commitment_guards", "0010_ledger_balances", "0011_costing", "0012_costing_standard", "0013_numbered_inventory", "0014_counts", "0015_procurement_vendors", "0016_procurement_ordering", "0017_procurement_billing", "0018_manufacturing_master", "0019_manufacturing_routing", "0020_manufacturing_execution", "0021_planning", "0022_planning_atp", "0023_wms_setup", "0024_wms_inbound", "0025_wms_execution", "0026_quality", "0027_consigned", "0028_extensions", "0029_reporting_views", "0030_rls_grants_verify", "0031_count_post_txn_idx", "0032_currency_global_writes"} and set(EXPECTED_TABLES) <= final["tables"]
    finally:
        try:
            asyncio.run(_drop())
        except Exception:  # pragma: no cover - defensive cleanup
            pass
