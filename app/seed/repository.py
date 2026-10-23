"""Repository abstraction for the seed loader.

The seed loader never talks to the database directly. It writes through this small protocol so
S0 can run without SQLAlchemy models (FS-00 owns them) while keeping a clean integration hook:

* :class:`MemoryRepository` — deterministic in-memory implementation used by tests and the CLI
  ``--state`` file (load-twice-no-diff gate).
* :class:`SqlAlchemySeedRepository` — documented hook for FS-00: pass an async session factory and a
  table registry; it will be completed when the platform tables exist (S0-03/FS-00 migration).

Upsert semantics are strictly non-destructive: rows are inserted or updated by natural key, never
deleted. Output of :meth:`SeedRepository.state_digest` is used to prove idempotency.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable

UpsertOutcome = Literal["inserted", "updated", "unchanged"]
TableName = str
RowKey = tuple[str, ...]


class SeedRepositoryError(RuntimeError):
    """Raised when a repository cannot satisfy the seed contract."""


@runtime_checkable
class SeedRepository(Protocol):
    """Minimal repository contract used by :class:`app.seed.loader.SeedLoader`."""

    def get(self, table: TableName, key: RowKey) -> dict[str, Any] | None: ...

    def upsert(self, table: TableName, key: RowKey, values: Mapping[str, Any]) -> UpsertOutcome: ...

    def count(self, table: TableName) -> int: ...

    def tables(self) -> Iterable[TableName]: ...

    def state_digest(self) -> str: ...


def _canonical(value: Any) -> Any:
    """Convert a seed value into a JSON-safe canonical form (deterministic)."""
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, int | float | str):
        return value
    return str(value)


def canonical_row(values: Mapping[str, Any]) -> dict[str, Any]:
    return _canonical(values)


class MemoryRepository:
    """In-memory seed repository with deterministic canonical digest.

    ``tenant_id`` is not stored separately from row values: the loader injects it into tenant-scoped
    rows and includes it in the natural key, so the same repository can hold several tenants.
    """

    def __init__(self) -> None:
        self._tables: dict[TableName, dict[RowKey, dict[str, Any]]] = {}

    # ---- SeedRepository -------------------------------------------------------
    def get(self, table: TableName, key: RowKey) -> dict[str, Any] | None:
        row = self._tables.get(table, {}).get(tuple(key))
        return copy.deepcopy(row) if row is not None else None

    def upsert(self, table: TableName, key: RowKey, values: Mapping[str, Any]) -> UpsertOutcome:
        key = tuple(key)
        canonical = canonical_row(values)
        rows = self._tables.setdefault(table, {})
        existing = rows.get(key)
        if existing is None:
            rows[key] = canonical
            return "inserted"
        if existing != canonical:
            # Non-destructive update: overwrite values for this natural key, never delete rows.
            rows[key] = canonical
            return "updated"
        return "unchanged"

    def count(self, table: TableName) -> int:
        return len(self._tables.get(table, {}))

    def tables(self) -> Iterable[TableName]:
        return sorted(self._tables)

    def state_digest(self) -> str:
        canonical = {
            table: {json.dumps(list(key), ensure_ascii=False): row for key, row in rows.items()}
            for table, rows in self._tables.items()
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---- helpers (tests/CLI) --------------------------------------------------
    def rows(self, table: TableName) -> dict[RowKey, dict[str, Any]]:
        """Deep copy of one table's rows (deterministic key order)."""
        rows = self._tables.get(table, {})
        return {key: copy.deepcopy(rows[key]) for key in sorted(rows)}

    def to_state(self) -> dict[str, Any]:
        """Serialise the whole repository for ``--state`` persistence."""
        return {
            "schema_version": "1.0.0",
            "digest": self.state_digest(),
            "tables": {
                table: {json.dumps(list(key)): row for key, row in sorted(rows.items())}
                for table, rows in sorted(self._tables.items())
            },
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> MemoryRepository:
        repo = cls()
        for table, rows in state.get("tables", {}).items():
            for raw_key, row in rows.items():
                key = tuple(json.loads(raw_key))
                repo._tables.setdefault(table, {})[key] = canonical_row(row)
        return repo

    def clone(self) -> MemoryRepository:
        return MemoryRepository.from_state(self.to_state())


class SqlAlchemySeedRepository:
    """FS-00 integration hook (not usable until the platform tables/models exist).

    Parameters
    ----------
    session_factory:
        Async SQLAlchemy ``async_sessionmaker`` (or any callable returning an async context
        manager); see ``app/db/session.py``.
    table_registry:
        Mapping ``table name -> mapped SQLAlchemy Table/ORM class`` owned by FS-00. The registry is
        supplied by the caller so this package never imports domain ORM models (import-linter rule).

    The implementation is intentionally deferred: S0 only needs the in-memory repository, and the
    seed data/manifest/loader/report layers are already final. FS-00 fills ``get/upsert/count`` once
    the models land (no interface change for callers).
    """

    def __init__(
        self,
        session_factory: Callable[..., Any] | None = None,
        table_registry: Mapping[str, Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tables = dict(table_registry or {})

    def get(self, table: TableName, key: RowKey) -> dict[str, Any] | None:
        self._require_ready(table)
        raise NotImplementedError("SqlAlchemySeedRepository.get is completed by FS-00 (models not yet present)")

    def upsert(self, table: TableName, key: RowKey, values: Mapping[str, Any]) -> UpsertOutcome:
        self._require_ready(table)
        raise NotImplementedError("SqlAlchemySeedRepository.upsert is completed by FS-00 (models not yet present)")

    def count(self, table: TableName) -> int:
        self._require_ready(table)
        raise NotImplementedError("SqlAlchemySeedRepository.count is completed by FS-00 (models not yet present)")

    def tables(self) -> Iterable[TableName]:
        return sorted(self._tables)

    def state_digest(self) -> str:
        raise NotImplementedError("SqlAlchemySeedRepository.state_digest is completed by FS-00")

    def _require_ready(self, table: TableName) -> None:
        if self._session_factory is None:
            raise SeedRepositoryError(
                "SqlAlchemy seed repository requires an async session factory; "
                "install models/migrations (S0-03 + FS-00) before using --profile sqlalchemy"
            )
        if table not in self._tables:
            raise SeedRepositoryError(
                f"table {table!r} is not in the SQLAlchemy table registry; "
                "FS-00 must supply the platform/inventory table mapping"
            )


def build_repository(
    profile: str = "memory",
    *,
    session_factory: Callable[..., Any] | None = None,
    table_registry: Mapping[str, Any] | None = None,
) -> SeedRepository:
    """Build the repository for the requested profile (``memory`` or ``sqlalchemy``)."""
    if profile == "memory":
        return MemoryRepository()
    if profile == "sqlalchemy":
        return SqlAlchemySeedRepository(session_factory=session_factory, table_registry=table_registry)
    raise SeedRepositoryError(f"unknown seed repository profile {profile!r} (expected memory|sqlalchemy)")
