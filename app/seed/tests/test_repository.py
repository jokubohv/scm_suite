"""In-memory seed repository semantics (non-destructive upsert, deterministic digest)."""

from __future__ import annotations

import pytest

from app.seed.repository import (
    DEFERRED_DATASETS,
    TABLE_MODELS,
    MemoryRepository,
    SeedRepositoryError,
    SqlAlchemySeedRepository,
    build_repository,
    canonical_row,
)


def test_upsert_insert_update_unchanged() -> None:
    repo = MemoryRepository()
    key = ("T1", "PO")
    assert repo.get("platform.number_sequence", key) is None
    assert repo.upsert("platform.number_sequence", key, {"doc_type": "PO", "next_value": 1}) == "inserted"
    assert repo.upsert("platform.number_sequence", key, {"doc_type": "PO", "next_value": 1}) == "unchanged"
    assert repo.upsert("platform.number_sequence", key, {"doc_type": "PO", "next_value": 2}) == "updated"
    updated = repo.get("platform.number_sequence", key)
    assert updated is not None and updated["next_value"] == 2


def test_get_returns_copy_not_reference() -> None:
    repo = MemoryRepository()
    repo.upsert("t", ("k",), {"a": 1})
    fetched = repo.get("t", ("k",))
    assert fetched is not None
    fetched["a"] = 99
    stored = repo.get("t", ("k",))
    assert stored is not None and stored["a"] == 1


def test_no_delete_api_and_count_never_decreases() -> None:
    repo = MemoryRepository()
    assert not hasattr(repo, "delete")
    for index in range(3):
        repo.upsert("t", (str(index),), {"n": index})
        assert repo.count("t") == index + 1


def test_state_digest_is_order_independent_and_key_sensitive() -> None:
    first = MemoryRepository()
    first.upsert("t", ("a",), {"v": 1})
    first.upsert("t", ("b",), {"v": 2})
    second = MemoryRepository()
    second.upsert("t", ("b",), {"v": 2})
    second.upsert("t", ("a",), {"v": 1})
    assert first.state_digest() == second.state_digest()

    third = MemoryRepository()
    third.upsert("t", ("a",), {"v": 1})
    third.upsert("t", ("b",), {"v": 3})
    assert third.state_digest() != first.state_digest()

    fourth = MemoryRepository()
    fourth.upsert("u", ("a",), {"v": 1})
    fourth.upsert("u", ("b",), {"v": 2})
    assert fourth.state_digest() != first.state_digest()


def test_state_round_trip_restores_identical_repository() -> None:
    repo = MemoryRepository()
    repo.upsert("t", ("a", "x"), {"v": 1})
    repo.upsert("u", ("b",), {"v": [1, 2, 3]})
    restored = MemoryRepository.from_state(repo.to_state())
    assert restored.state_digest() == repo.state_digest()
    assert restored.rows("u") == repo.rows("u")
    clone = repo.clone()
    assert clone.state_digest() == repo.state_digest()


def test_canonical_row_is_json_safe() -> None:
    row = canonical_row({"b": [1, {"z": 2, "a": 1}], "a": "x"})
    assert list(row.keys()) == ["a", "b"]


def test_build_repository_profiles() -> None:
    assert isinstance(build_repository("memory"), MemoryRepository)
    sqlalchemy_repo = build_repository("sqlalchemy")
    assert isinstance(sqlalchemy_repo, SqlAlchemySeedRepository)
    try:
        # 8 physical platform tables are writable; 5 datasets are deferred until their tables exist.
        assert set(sqlalchemy_repo.tables()) == set(TABLE_MODELS)
        assert sqlalchemy_repo.deferred_datasets == sorted(DEFERRED_DATASETS)
        assert sqlalchemy_repo.get("catalog.sod_pair", ("t", "a", "b")) is None
        assert sqlalchemy_repo.upsert("catalog.sod_pair", ("t", "a", "b"), {"a": "x"}) == "unchanged"
        with pytest.raises(SeedRepositoryError):
            sqlalchemy_repo.get("unknown.table", ("x",))
    finally:
        sqlalchemy_repo.close()
    with pytest.raises(SeedRepositoryError):
        build_repository("unknown")
    with pytest.raises(SeedRepositoryError):
        SqlAlchemySeedRepository(
            "postgresql+asyncpg://nobody:nobody@127.0.0.1:9/unreachable"
        ).get("unknown.table", ("x",))
