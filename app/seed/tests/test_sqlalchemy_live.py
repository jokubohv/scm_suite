"""Live-DB tests for the SQLAlchemy seed repository (task-48): 8 available datasets, deferred report,
idempotency, tenant scoping and rollback. Skipped gracefully when Postgres is unavailable."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.seed.loader import SeedLoader
from app.seed.repository import (
    DEFERRED_DATASETS,
    TABLE_MODELS,
    SeedRepositoryError,
    SqlAlchemySeedRepository,
    build_repository,
)

pytestmark = pytest.mark.integration

# Expected row counts for the available (physical) datasets.
EXPECTED = {
    "permissions": 255,
    "role_templates": 7,
    "role_permissions": 591,
    "feature_flags": 17,
    "number_sequences": 27,
    "tenant_settings": 8,
    "system_tenant": 1,
}


def _load(tenant: uuid.UUID, database_url: str) -> tuple[SqlAlchemySeedRepository, Any]:
    repository = build_repository("sqlalchemy", database_url=database_url, tenant_id=tenant)
    assert isinstance(repository, SqlAlchemySeedRepository)
    repository.check_connection()
    repository.check_tenant_exists()
    report = SeedLoader(repository).load(tenant)
    repository.finalise_report(report)
    return repository, report


async def test_first_load_seeds_available_datasets_and_reports_deferred(
    database_url: str, live_tenant: dict[str, object], db_counter: Any
) -> None:
    tenant = live_tenant["tenant_id"]
    assert isinstance(tenant, uuid.UUID)
    repository, report = _load(tenant, database_url)
    try:
        assert report.valid, report.render()
        repository.commit()
    finally:
        repository.close()

    for dataset, expected in EXPECTED.items():
        assert report.dataset(dataset).total == expected, dataset
    assert report.dataset("permissions").inserted == 255
    assert report.dataset("role_templates").inserted == 7
    assert report.dataset("role_permissions").inserted == 591
    assert report.dataset("feature_flags").inserted == 17
    assert report.dataset("number_sequences").inserted == 27
    assert report.dataset("tenant_settings").inserted == 8
    assert report.dataset("system_tenant").total == 1  # canonical SYSTEM row is upserted
    assert report.dataset("currencies").total == 102  # 10 baseline live + 92 reported deferred
    assert report.dataset("currencies").skipped_reference_only == 92
    assert report.inserted > 0

    # All five deferred datasets are reported as skipped, never inserted/updated.
    for dataset in ("sod_policy", "transaction_types", "uom_types", "uom_units", "inventory_statuses"):
        deferred = report.dataset(dataset)
        assert deferred.inserted == 0 and deferred.updated == 0
        assert deferred.skipped_reference_only == deferred.total > 0
    assert report.warnings

    # Real table counts, tenant-scoped.
    assert await db_counter("platform.permission_scope", tenant) == 255
    assert await db_counter("platform.role", tenant) == 7
    assert await db_counter("platform.role_permission", tenant) == 591
    assert await db_counter("platform.feature_flag", tenant) == 17
    assert await db_counter("platform.number_sequence", tenant) == 27
    assert await db_counter("platform.tenant_setting", tenant) == 8
    assert await db_counter("platform.currency") == 10  # migration-seeded baseline only
    # No deferred placeholder rows in the settings namespace.
    assert not await _setting_keys_start_with_seed_ref(database_url, tenant)


async def _setting_keys_start_with_seed_ref(database_url: str, tenant: uuid.UUID) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM platform.tenant_setting "
                    "WHERE tenant_id = :tenant AND key LIKE 'seed.ref.%'"
                ),
                {"tenant": tenant},
            )
            return int(result.scalar_one()) > 0
    finally:
        await engine.dispose()


async def test_load_twice_no_diff(database_url: str, live_tenant: dict[str, object]) -> None:
    tenant = live_tenant["tenant_id"]
    assert isinstance(tenant, uuid.UUID)

    first_repo, first = _load(tenant, database_url)
    try:
        assert first.valid
        first_repo.commit()
        digest_after_first = first_repo.state_digest()
    finally:
        first_repo.close()

    second_repo, second = _load(tenant, database_url)
    try:
        assert second.valid
        assert second.inserted == 0
        assert second.updated == 0
        second_repo.commit()
        assert second_repo.state_digest() == digest_after_first
    finally:
        second_repo.close()

    for dataset, expected in EXPECTED.items():
        assert second.dataset(dataset).inserted == 0, dataset
        assert second.dataset(dataset).total == expected, dataset


async def test_rollback_leaves_no_tenant_rows(
    database_url: str, live_tenant: dict[str, object], db_counter: Any
) -> None:
    tenant = live_tenant["tenant_id"]
    assert isinstance(tenant, uuid.UUID)
    repository, report = _load(tenant, database_url)
    try:
        assert report.valid
        repository.rollback()
    finally:
        repository.close()
    for table in (
        "platform.permission_scope",
        "platform.role",
        "platform.role_permission",
        "platform.feature_flag",
        "platform.number_sequence",
        "platform.tenant_setting",
    ):
        assert await db_counter(table, tenant) == 0, table


async def test_tenant_scoping(database_url: str, live_tenant: dict[str, object], db_counter: Any) -> None:
    # A second tenant is created inline so both loads can be compared; both are cleaned by the fixture
    # only for the primary tenant, so we clean the sibling explicitly below.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    primary = live_tenant["tenant_id"]
    assert isinstance(primary, uuid.UUID)
    secondary = uuid.uuid4()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO platform.tenant (id, tenant_id, code, name, timezone, status) "
                "VALUES (:id, :id, :code, 'Secondary Seed Tenant', 'UTC', 'ACTIVE')"
            ),
            {"id": secondary, "code": "SEC" + uuid.uuid4().hex[:8].upper()},
        )
    try:
        primary_repo, _ = _load(primary, database_url)
        try:
            primary_repo.commit()
            primary_digest = primary_repo.state_digest()
        finally:
            primary_repo.close()

        assert await db_counter("platform.permission_scope", primary) == 255
        assert await db_counter("platform.permission_scope", secondary) == 0
        assert await db_counter("platform.feature_flag", primary) == 17
        assert await db_counter("platform.feature_flag", secondary) == 0

        secondary_repo, _ = _load(secondary, database_url)
        try:
            secondary_repo.commit()
            assert secondary_repo.state_digest() != primary_digest
        finally:
            secondary_repo.close()
        assert await db_counter("platform.permission_scope", secondary) == 255
        assert await db_counter("platform.permission_scope", primary) == 255
    finally:
        async with engine.begin() as connection:
            for table in (
                "platform.tenant_setting",
                "platform.feature_flag",
                "platform.number_sequence",
                "platform.role_permission",
                "platform.permission_scope",
                "platform.role",
            ):
                await connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tenant"), {"tenant": secondary}
                )
            await connection.execute(text("DELETE FROM platform.tenant WHERE id = :tenant"), {"tenant": secondary})
        await engine.dispose()


def test_repository_contract_without_database() -> None:
    """Static repo contract: 8 physical tables, 5 deferred datasets, no writes for deferred tables."""
    repository = build_repository(
        "sqlalchemy",
        database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:9/unreachable",
        tenant_id=uuid.uuid4(),
    )
    assert isinstance(repository, SqlAlchemySeedRepository)
    try:
        assert set(repository.tables()) == set(TABLE_MODELS)
        assert repository.deferred_datasets == sorted(DEFERRED_DATASETS)
        assert repository.get("catalog.sod_pair", ("t", "a", "b")) is None
        assert repository.upsert("catalog.sod_pair", ("t", "a", "b"), {"a": "x", "b": "y"}) == "unchanged"
        with pytest.raises(SeedRepositoryError):
            repository.get("unknown.table", ("x",))
    finally:
        repository.close()
