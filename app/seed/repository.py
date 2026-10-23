"""Repository abstraction for the seed loader (S0-07 / FS-00 task-48).

The seed loader never talks to the database directly. It writes through the small synchronous
:class:`SeedRepository` contract, which has two implementations:

* :class:`MemoryRepository` — deterministic in-memory implementation used by hermetic tests and the
  CLI ``--state`` file (fast load-twice-no-diff gate without a database; loads all 13 datasets).
* :class:`SqlAlchemySeedRepository` — **live Postgres implementation** over the
  ``app.core.models.platform`` Core tables (migrations 0001–0003). The async engine/session runs on a
  private background event loop; the loader-facing API stays synchronous so
  :class:`app.seed.loader.SeedLoader` and the report/idempotency contract are unchanged.

Live-DB dataset mapping (8 available datasets)
----------------------------------------------
``platform.tenant`` (SYSTEM zero-UUID row), ``currency`` (10 ISO), ``permission_scope`` (255),
``role`` (7 system templates), ``role_permission`` (template→scope; role_code/scope resolved to
role_id/scope_id), ``feature_flag`` (17, default OFF), ``number_sequence`` (27 templates),
``tenant_setting`` (8 platform-seeded settings).

Deferred datasets (5; owning tables do not exist yet)
-----------------------------------------------------
SoD pairs, UoM types/units, inventory statuses (incl. USED/REFURB), the T-01..T-31 transaction-type
catalogue and the RBAC alias mapping have no physical tables in migrations 0001–0003. The SQLAlchemy
repository deliberately **does not write them** (no JSON placeholders, no foreign tables) — their owning
FS migrations/seeds add them later. They are exposed as :attr:`SqlAlchemySeedRepository.deferred_datasets`
and :meth:`SqlAlchemySeedRepository.finalise_report` moves their loader counts to
``skipped_reference_only`` with an explicit warning, so the load report never claims they were persisted.

Upsert semantics are strictly non-destructive (insert/update by natural key, never delete). A single
async session is used per repository instance; :meth:`SqlAlchemySeedRepository.commit` /
:meth:`rollback` define the transaction envelope, and :meth:`state_digest` is an idempotency digest over
the tenant-scoped seed state.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.types import Date as SADate
from sqlalchemy.types import DateTime as SADateTime

from app.seed.constants import SYSTEM_TENANT_ID

UpsertOutcome = Literal["inserted", "updated", "unchanged"]
TableName = str
RowKey = tuple[str, ...]

DEFAULT_DATABASE_URL = "postgresql+asyncpg://inventory:inventory-dev-password@localhost:25432/inventory"

#: Baseline ISO-4217 currencies seeded by the platform migrations (migrations 0001 retain these 10 rows).
#: The seed payload carries a wider initial ISO set for future extension; FS-00 only maintains the
#: documented baseline so the live reference table stays in sync with the migration seed contract.
BASELINE_CURRENCY_CODES: frozenset[str] = frozenset(
    {"AUD", "CAD", "CNY", "EUR", "GBP", "INR", "JPY", "MXN", "SGD", "USD"}
)

#: Datasets whose physical tables do not exist in migrations 0001–0003 (table -> owning feature set).
DEFERRED_DATASETS: dict[str, str] = {
    "catalog.sod_pair": "FS-00",  # RBAC SoD policy table arrives with the RBAC migration set
    "catalog.transaction_type": "FS-04",  # ledger reference catalogue seed
    "item_master.uom_type": "FS-01",
    "item_master.uom": "FS-01",
    "traceability.inventory_status": "FS-03",
}


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


# ==============================================================================================
# SQLAlchemy live-DB repository (FS-00 Core metadata; no ORM cross-imports)
# ==============================================================================================
def _platform_models() -> Any:
    """Lazy import keeps memory-only environments importable without the platform metadata."""
    from app.core.models import platform as models

    return models


def default_database_url() -> str:
    """Resolve the seed database URL: SEED_DATABASE_URL > DATABASE_URL > TEST_DATABASE_URL > dev default."""
    import os

    for env_var in ("SEED_DATABASE_URL", "DATABASE_URL", "TEST_DATABASE_URL"):
        value = os.environ.get(env_var)
        if value:
            return value
    return DEFAULT_DATABASE_URL


def database_url_configured() -> bool:
    """True when an explicit seed/test DB URL is configured in the environment."""
    import os

    return any(os.environ.get(var) for var in ("SEED_DATABASE_URL", "DATABASE_URL", "TEST_DATABASE_URL"))


class _BackgroundLoop:
    """Dedicated event loop thread used to expose an async engine through a sync repository API."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="seed-db-loop", daemon=True)
        self._closed = False
        self._thread.start()

    def _run(self) -> None:  # pragma: no cover - thread entry point
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any, *, timeout: float = 120.0) -> Any:
        if self._closed:
            raise SeedRepositoryError("seed repository is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:  # pragma: no cover - defensive timeout
            future.cancel()
            raise SeedRepositoryError(f"database operation timed out after {timeout:.0f}s") from exc

    def close(self) -> None:  # pragma: no cover - thread shutdown
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


class SqlAlchemySeedRepository:
    """Live Postgres seed repository over the ``app.core.models.platform`` Core tables.

    Parameters
    ----------
    database_url:
        SQLAlchemy async URL; loaded lazily from :func:`default_database_url` when omitted.
    tenant_id:
        Target tenant for tenant-scoped rows and digest scoping. ``SYSTEM_TENANT_ID`` loads only the
        global datasets; real tenants must already exist (FKs) — see :meth:`check_tenant_exists`.
    session_factory:
        Optional ``async_sessionmaker``/callable for tests or FS-00 provisioning; wins over
        ``database_url``. The session's transaction is owned by this repository.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        tenant_id: UUID | str | None = None,
        session_factory: Callable[..., AsyncSession] | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        self.database_url = database_url
        self.tenant_id = UUID(str(tenant_id)) if tenant_id is not None else SYSTEM_TENANT_ID
        self._session_factory = session_factory
        self._connect_timeout = connect_timeout
        self._loop = _BackgroundLoop()
        self._engine: Any | None = None
        self._session: AsyncSession | None = None
        self._closed = False
        # Per-table upsert outcomes recorded during a run (used to make the loader report exact:
        # the loader classifies rows from get() before upsert(), while this repository knows the real
        # insert/update/unchanged outcome from the DB comparison).
        self._outcomes: dict[str, dict[RowKey, UpsertOutcome]] = {}
        # Rows deliberately not written because their dataset is partially deferred (currency baseline).
        self._skipped_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ lifecycle ------------
    def __enter__(self) -> SqlAlchemySeedRepository:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close(commit=exc_type is None)

    def _run(self, coro: Any) -> Any:
        return self._loop.run(coro)

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:  # pragma: no cover - exercised only without an injected factory
                url = self.database_url or default_database_url()
                self._engine = create_async_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
                factory = async_sessionmaker(self._engine, expire_on_commit=False)
                self._session = factory()
        return self._session

    async def _commit(self) -> None:
        if self._session is not None:
            await self._session.commit()

    async def _rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()

    async def _close(self, commit: bool) -> None:
        try:
            if self._session is not None:
                if commit:
                    await self._session.commit()
                else:
                    await self._session.rollback()
                await self._session.close()
                self._session = None
            if self._engine is not None:
                await self._engine.dispose()
                self._engine = None
        finally:
            self._closed = True

    def commit(self) -> None:
        """Commit the seed transaction."""
        self._run(self._commit())

    def rollback(self) -> None:
        """Roll back all seed writes since the last commit (transaction/rollback tests)."""
        self._run(self._rollback())

    def close(self, *, commit: bool = False) -> None:
        if self._closed:
            return
        try:
            self._run(self._close(commit))
        finally:
            self._loop.close()

    # ---------------------------------------------------------------- connectivity -----------
    def check_connection(self) -> None:
        """Verify the DB is reachable; raises :class:`SeedRepositoryError` with an actionable reason."""
        async def _ping() -> None:
            session = await self._ensure_session()
            from sqlalchemy import text

            try:
                await session.execute(text("SELECT 1"))
            except Exception as exc:  # noqa: BLE001 - environment boundary
                raise SeedRepositoryError(f"seed database unavailable: {exc}") from exc

        try:
            self._run(_ping())
        except SeedRepositoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - environment boundary
            raise SeedRepositoryError(f"seed database unavailable: {exc}") from exc

    def check_tenant_exists(self) -> None:
        """Ensure a real target tenant row exists (tenant-scoped FKs); SYSTEM is always implicit."""
        if self.tenant_id == SYSTEM_TENANT_ID:
            return

        async def _check() -> None:
            models = _platform_models()
            session = await self._ensure_session()
            result = await session.execute(select(models.tenant.c.id).where(models.tenant.c.id == self.tenant_id))
            if result.scalar_one_or_none() is None:
                raise SeedRepositoryError(
                    f"target tenant {self.tenant_id} does not exist in platform.tenant; "
                    "provision the tenant before seeding"
                )

        self._run(_check())

    # ---------------------------------------------------------- deferred datasets ------------
    @property
    def deferred_datasets(self) -> list[str]:
        """Datasets with no physical table in the current migrations (not written to the DB)."""
        return sorted(DEFERRED_DATASETS)

    def finalise_report(self, report: Any) -> Any:
        """Move deferred-dataset counts to ``skipped_reference_only`` and document the reason.

        The loader iterates all 13 datasets; for tables with no physical schema the repository returns
        ``None``/``unchanged`` without writing. This method makes the report honest: deferred rows are
        counted as skipped, a warning is appended, and the interface used by the CLI exposes the list.
        Mutates and returns the supplied :class:`app.seed.report.SeedReport` (kept in ``report.py``).
        """
        deferred_tables = set(DEFERRED_DATASETS)
        moved = 0
        for dataset_report in getattr(report, "datasets", []):
            table = dataset_report.table
            if table in deferred_tables:
                moved += dataset_report.total
                dataset_report.skipped_reference_only += dataset_report.total
                dataset_report.inserted = 0
                dataset_report.updated = 0
                dataset_report.unchanged = 0
                continue
            outcomes = self._outcomes.get(table)
            if outcomes:
                # Exact counts from real DB comparisons (non-dry-run runs only).
                dataset_report.inserted = sum(1 for outcome in outcomes.values() if outcome == "inserted")
                dataset_report.updated = sum(1 for outcome in outcomes.values() if outcome == "updated")
                dataset_report.unchanged = sum(1 for outcome in outcomes.values() if outcome == "unchanged")
                dataset_report.skipped_reference_only = self._skipped_counts.get(table, 0)
                if table == "platform.currency" and dataset_report.skipped_reference_only:
                    report.warnings.append(
                        "currency_baseline: only the 10 migration-seeded ISO currencies are maintained live "
                        f"({dataset_report.skipped_reference_only} wider-catalogue rows deferred)"
                    )
        if moved:
            owners = ", ".join(f"{table} ({owner})" for table, owner in sorted(DEFERRED_DATASETS.items()))
            report.warnings.append(
                f"deferred_datasets: {moved} seed rows not written — owning tables not migrated yet: {owners}"
            )
        return report

    # -------------------------------------------------------------- SeedRepository -----------
    def get(self, table: TableName, key: RowKey) -> dict[str, Any] | None:
        return self._run(self._get(table, tuple(key)))

    def upsert(self, table: TableName, key: RowKey, values: Mapping[str, Any]) -> UpsertOutcome:
        return self._run(self._upsert(table, tuple(key), dict(values)))

    def count(self, table: TableName) -> int:
        return self._run(self._count(table))

    def tables(self) -> Iterable[TableName]:
        return sorted(TABLE_MODELS)

    def state_digest(self) -> str:
        return self._run(self._digest())

    # ------------------------------------------------------------- internal async ops --------
    async def _get(self, table: TableName, key: RowKey) -> dict[str, Any] | None:
        if table in DEFERRED_DATASETS:
            return None
        if table not in TABLE_MODELS:
            raise SeedRepositoryError(f"unknown seed table {table!r} for the SQLAlchemy repository")
        models = _platform_models()
        session = await self._ensure_session()
        model = TABLE_MODELS[table]
        if table == "platform.role_permission":
            resolved = await self._resolve_role_permission_key(models, key)
            if resolved is None:
                return None
            conditions = [
                model.c.tenant_id == resolved["tenant_id"],
                model.c.role_id == resolved["role_id"],
                model.c.scope_id == resolved["scope_id"],
            ]
        else:
            conditions = self._natural_key_conditions(table, model, key)
        result = await session.execute(select(model).where(*conditions))
        mapping = result.mappings().first()
        if mapping is None:
            return None
        return _row_payload(mapping)

    async def _upsert(self, table: TableName, key: RowKey, values: dict[str, Any]) -> UpsertOutcome:
        if table in DEFERRED_DATASETS:
            # No physical table yet: never write placeholders; owning FS migration seeds it later.
            return "unchanged"
        if table == "platform.currency" and str(key[-1]).upper() not in BASELINE_CURRENCY_CODES:
            # Only the documented baseline ISO set is maintained live (migration seed contract);
            # the wider initial ISO catalogue stays available in the seed payload for later extension.
            self._skipped_counts[table] = self._skipped_counts.get(table, 0) + 1
            return "unchanged"
        if table not in TABLE_MODELS:
            raise SeedRepositoryError(f"unknown seed table {table!r} for the SQLAlchemy repository")
        session = await self._ensure_session()
        model = TABLE_MODELS[table]
        prepared = await self._prepare_values(table, values)
        if table == "platform.role_permission":
            conditions = [
                model.c.tenant_id == prepared["tenant_id"],
                model.c.role_id == prepared["role_id"],
                model.c.scope_id == prepared["scope_id"],
            ]
        else:
            conditions = self._natural_key_conditions(table, model, key)
        existing = await session.execute(select(model).where(*conditions))
        mapping = existing.mappings().first()
        if mapping is None:
            if "id" in model.c and "id" not in prepared:
                prepared["id"] = uuid4()
            await session.execute(insert(model).values(**prepared))
            await session.flush()
            self._record_outcome(table, key, "inserted")
            return "inserted"
        changed = {
            column: value
            for column, value in prepared.items()
            if column != "id" and mapping[column] != value
        }
        if not changed:
            self._record_outcome(table, key, "unchanged")
            return "unchanged"
        if "version" in model.c:
            changed["version"] = model.c.version + 1
        await session.execute(update(model).where(*conditions).values(**changed))
        await session.flush()
        self._record_outcome(table, key, "updated")
        return "updated"

    async def _count(self, table: TableName) -> int:
        if table in DEFERRED_DATASETS:
            return 0
        if table not in TABLE_MODELS:
            raise SeedRepositoryError(f"unknown seed table {table!r} for the SQLAlchemy repository")
        session = await self._ensure_session()
        model = TABLE_MODELS[table]
        statement = select(func.count()).select_from(model)
        statement = self._apply_scope(table, model, statement)
        result = await session.execute(statement)
        return int(result.scalar_one())

    async def _digest(self) -> str:
        session = await self._ensure_session()
        await session.flush()
        payload: dict[str, Any] = {
            "deferred_datasets": self.deferred_datasets,
        }
        for table in sorted(TABLE_MODELS):
            model = TABLE_MODELS[table]
            statement = self._apply_scope(table, model, select(model))
            result = await session.execute(statement)
            records = [_row_payload(mapping) for mapping in result.mappings().all()]
            payload[table] = sorted(records, key=lambda row: _sort_key(row, NATURAL_KEYS[table]))
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _record_outcome(self, table: TableName, key: RowKey, outcome: UpsertOutcome) -> None:
        self._outcomes.setdefault(table, {})[tuple(key)] = outcome

    async def _prepare_values(self, table: TableName, values: Mapping[str, Any]) -> dict[str, Any]:
        models = _platform_models()
        model = TABLE_MODELS[table]
        if table == "platform.role_permission":
            return await self._prepare_role_permission(models, values)
        prepared: dict[str, Any] = {}
        aliases = COLUMN_ALIASES.get(table, {})
        for raw_key, value in values.items():
            column_name = aliases.get(raw_key, raw_key)
            if column_name is None or column_name not in model.c:
                continue
            prepared[column_name] = _coerce_value(model.c[column_name], value)
        if table == "platform.tenant":
            prepared.setdefault("tenant_id", prepared.get("id"))
        if table == "platform.tenant_setting":
            prepared.setdefault("is_default", False)
            prepared.setdefault("value_type", "JSON")
        if table == "platform.role":
            prepared.setdefault("is_template", True)
            prepared.setdefault("is_system", True)
            prepared.setdefault("is_editable", False)
        return prepared

    async def _prepare_role_permission(self, models: Any, values: Mapping[str, Any]) -> dict[str, Any]:
        session = await self._ensure_session()
        tenant_id = UUID(str(values["tenant_id"]))
        role_code = str(values["role_code"])
        scope_name = str(values["scope"])
        role_row = await session.execute(
            select(models.role.c.id).where(
                models.role.c.tenant_id == tenant_id, models.role.c.code == role_code
            )
        )
        role_id = role_row.scalar_one_or_none()
        if role_id is None:
            raise SeedRepositoryError(
                f"role {role_code!r} missing for tenant {tenant_id}; seed the role templates first"
            )
        scope_row = await session.execute(
            select(models.permission_scope.c.id).where(
                models.permission_scope.c.tenant_id == tenant_id,
                models.permission_scope.c.scope == scope_name,
            )
        )
        scope_id = scope_row.scalar_one_or_none()
        if scope_id is None:
            raise SeedRepositoryError(
                f"permission scope {scope_name!r} missing for tenant {tenant_id}; seed the catalogue first"
            )
        return {"tenant_id": tenant_id, "role_id": role_id, "scope_id": scope_id}

    async def _resolve_role_permission_key(self, models: Any, key: RowKey) -> dict[str, Any] | None:
        """Resolve the loader's logical ``(tenant, role_code, scope)`` key to FK ids (None if absent)."""
        if len(key) != 3:  # pragma: no cover - loader contract
            raise SeedRepositoryError(f"role_permission key expects (tenant, role_code, scope), got {len(key)}")
        session = await self._ensure_session()
        tenant_id = UUID(str(key[0]))
        role_row = await session.execute(
            select(models.role.c.id).where(
                models.role.c.tenant_id == tenant_id, models.role.c.code == str(key[1])
            )
        )
        role_id = role_row.scalar_one_or_none()
        scope_row = await session.execute(
            select(models.permission_scope.c.id).where(
                models.permission_scope.c.tenant_id == tenant_id,
                models.permission_scope.c.scope == str(key[2]),
            )
        )
        scope_id = scope_row.scalar_one_or_none()
        if role_id is None or scope_id is None:
            return None
        return {"tenant_id": tenant_id, "role_id": role_id, "scope_id": scope_id}

    def _natural_key_conditions(self, table: TableName, model: Any, key: RowKey) -> list[Any]:
        columns = NATURAL_KEYS[table]
        normalised: list[Any] = list(key)
        # The loader's currency natural key carries a synthetic "SYSTEM" scope prefix for the global rows.
        if table == "platform.currency" and len(normalised) == 2 and str(normalised[0]) == "SYSTEM":
            normalised = normalised[1:]
        if len(normalised) != len(columns):  # pragma: no cover - contract error
            raise SeedRepositoryError(
                f"table {table!r} expects natural key {columns}, got {len(normalised)} parts"
            )
        if table == "platform.number_sequence" and normalised[3] == "":
            normalised[3] = None
        return [
            _coerce_value(model.c[column], normalised[index]) == model.c[column]
            for index, column in enumerate(columns)
        ]

    def _apply_scope(self, table: TableName, model: Any, statement: Any) -> Any:
        if table == "platform.currency":
            return statement
        if table == "platform.tenant":
            return statement.where(model.c.id == SYSTEM_TENANT_ID)
        return statement.where(model.c.tenant_id == self.tenant_id)


# ----------------------------------------------------------------------------------------------
# Table/model mapping (platform Core metadata only; no cross-module ORM imports)
# ----------------------------------------------------------------------------------------------
def _build_table_models() -> dict[str, Any]:
    models = _platform_models()
    return {
        "platform.tenant": models.tenant,
        "platform.currency": models.currency,
        "platform.permission_scope": models.permission_scope,
        "platform.role": models.role,
        "platform.role_permission": models.role_permission,
        "platform.tenant_setting": models.tenant_settings,
        "platform.feature_flag": models.feature_flag,
        "platform.number_sequence": models.number_sequence,
    }


TABLE_MODELS: dict[str, Any] = _build_table_models()

NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "platform.tenant": ("id",),
    "platform.currency": ("code",),
    "platform.permission_scope": ("tenant_id", "scope"),
    "platform.role": ("tenant_id", "code"),
    "platform.role_permission": ("tenant_id", "role_id", "scope_id"),
    "platform.tenant_setting": ("tenant_id", "namespace", "key"),
    "platform.feature_flag": ("tenant_id", "flag_key"),
    "platform.number_sequence": ("tenant_id", "doc_type", "scope_key", "period_key"),
}

COLUMN_ALIASES: dict[str, dict[str, str | None]] = {
    "platform.tenant": {"owner_feature_set": None},
    "platform.currency": {"tenant_scope": None, "owner_feature_set": None},
    "platform.permission_scope": {
        "platform_only": "is_platform_only",
        "assignable": "is_assignable",
        "service_only": None,
    },
    "platform.role": {
        "is_cloneable": None,
        "scope_count": None,
        "is_template": None,
        "inactive_at": None,
    },
    "platform.role_permission": {"role_code": None, "scope": None},
    "platform.tenant_setting": {"owner_feature_set": None},
    "platform.feature_flag": {"default_enabled": None, "description": None, "owner_feature_set": None},
    "platform.number_sequence": {"owner_feature_set": None},
}


# ----------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------
def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _row_payload(mapping: Any) -> dict[str, Any]:
    return {str(key): _canonical(_json_value(value)) for key, value in mapping.items()}


def _coerce_value(column: Any, value: Any) -> Any:
    if value is None:
        return None
    column_type = column.type
    if isinstance(column_type, PGUUID) and isinstance(value, str):
        return UUID(value)
    if isinstance(column_type, SADateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(column_type, SADate) and not isinstance(column_type, SADateTime) and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _sort_key(row: Mapping[str, Any], natural_key: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(column)) for column in natural_key)


# ----------------------------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------------------------
def build_repository(
    profile: str = "memory",
    *,
    database_url: str | None = None,
    tenant_id: UUID | str | None = None,
    session_factory: Callable[..., AsyncSession] | None = None,
) -> SeedRepository:
    """Build the repository for the requested profile (``memory`` or ``sqlalchemy``)."""
    if profile == "memory":
        return MemoryRepository()
    if profile == "sqlalchemy":
        return SqlAlchemySeedRepository(
            database_url=database_url,
            tenant_id=tenant_id,
            session_factory=session_factory,
        )
    raise SeedRepositoryError(f"unknown seed repository profile {profile!r} (expected memory|sqlalchemy)")


__all__ = [
    "COLUMN_ALIASES",
    "DEFAULT_DATABASE_URL",
    "DEFERRED_DATASETS",
    "MemoryRepository",
    "NATURAL_KEYS",
    "SeedRepository",
    "SeedRepositoryError",
    "SqlAlchemySeedRepository",
    "TABLE_MODELS",
    "build_repository",
    "canonical_row",
    "database_url_configured",
    "default_database_url",
]
