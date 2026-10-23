"""Load-twice-no-diff: the seed loader is idempotent and non-destructive."""

from __future__ import annotations

from uuid import UUID

from app.seed.constants import SYSTEM_TENANT_ID
from app.seed.loader import SeedLoader
from app.seed.repository import MemoryRepository

TENANT = UUID("11111111-1111-7111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-7222-8222-222222222222")


def _loader(repository: MemoryRepository, **kwargs) -> SeedLoader:
    return SeedLoader(repository, **kwargs)


def test_first_load_inserts_and_second_load_is_noop() -> None:
    repository = MemoryRepository()
    first = _loader(repository).load(TENANT)
    assert first.valid
    assert first.inserted > 0
    assert first.updated == 0
    assert first.unchanged == 0
    assert first.digest_unchanged is False
    rows_after_first = _total_rows(repository)

    second = _loader(repository).load(TENANT)
    assert second.valid
    assert second.inserted == 0
    assert second.updated == 0
    assert second.unchanged == first.total
    assert second.digest_before == second.digest_after == first.digest_after
    assert second.digest_unchanged is True
    assert _total_rows(repository) == rows_after_first
    assert repository.state_digest() == first.digest_after


def test_expected_dataset_sizes() -> None:
    report = _loader(MemoryRepository()).load(TENANT)
    assert report.dataset("system_tenant").total == 1
    assert report.dataset("permissions").total == 255
    assert report.dataset("role_templates").total == 7
    assert report.dataset("role_permissions").total == 591
    assert report.dataset("sod_policy").total == 14
    assert report.dataset("transaction_types").total == 31
    assert report.dataset("inventory_statuses").total == 8
    assert report.dataset("number_sequences").total == 27
    assert report.dataset("feature_flags").total == 17
    assert report.dataset("uom_types").total == 4
    assert report.dataset("uom_units").total == 11
    assert report.dataset("tenant_settings").total == 8


def test_tenant_rows_are_tenant_scoped_and_roles_are_per_tenant() -> None:
    repository = MemoryRepository()
    _loader(repository).load(TENANT)
    permission_rows = repository.rows("platform.permission_scope")
    assert len(permission_rows) == 255
    for key, row in permission_rows.items():
        assert key[0] == str(TENANT)
        assert row["tenant_id"] == str(TENANT)
    role_rows = repository.rows("platform.role")
    assert len(role_rows) == 7
    assert all(row["is_system"] is True for row in role_rows.values())
    # feature flags are per-tenant and default OFF.
    flags = repository.rows("platform.feature_flag")
    assert len(flags) == 17
    assert all(row["is_enabled"] is False and row["source"] == "DEFAULT" for row in flags.values())


def test_system_rows_are_global_and_currencies_have_no_tenant() -> None:
    repository = MemoryRepository()
    _loader(repository).load(TENANT)
    tenants = repository.rows("platform.tenant")
    assert len(tenants) == 1
    assert tenants[(str(SYSTEM_TENANT_ID),)]["code"] == "SYSTEM"
    currencies = repository.rows("platform.currency")
    assert len(currencies) == 102
    assert all(row["tenant_id"] is None for row in currencies.values())
    transaction_types = repository.rows("catalog.transaction_type")
    assert len(transaction_types) == 31
    assert all(row["tenant_id"] is None for row in transaction_types.values())


def test_second_tenant_gets_its_own_rows_without_touching_first_tenant() -> None:
    repository = MemoryRepository()
    loader = _loader(repository)
    loader.load(TENANT)
    digest_after_first = repository.state_digest()
    report = loader.load(OTHER_TENANT)
    assert report.valid
    assert report.inserted == 255 + 7 + 591 + 14 + 8 + 17 + 27 + 4 + 11 + 8  # tenant-scoped datasets
    assert repository.state_digest() != digest_after_first
    assert len(repository.rows("platform.permission_scope")) == 510
    # SYSTEM datasets are not duplicated by a second tenant load (upsert -> unchanged).
    assert report.dataset("currencies").unchanged == 102


def test_duplicate_load_with_same_repo_is_stable_across_many_runs() -> None:
    repository = MemoryRepository()
    loader = _loader(repository)
    digests = []
    for _ in range(4):
        report = loader.load(TENANT)
        assert report.valid
        digests.append(repository.state_digest())
    assert len(set(digests)) == 1


def test_system_only_load() -> None:
    repository = MemoryRepository()
    report = _loader(repository).load(SYSTEM_TENANT_ID)
    assert report.valid
    assert {dataset.name for dataset in report.datasets} == {
        "system_tenant",
        "currencies",
        "transaction_types",
    }
    assert repository.rows("platform.permission_scope") == {}


def _total_rows(repository: MemoryRepository) -> int:
    return sum(repository.count(table) for table in repository.tables())
