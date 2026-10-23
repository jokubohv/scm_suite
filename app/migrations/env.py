# ruff: noqa: E402, I001
"""Alembic environment — async SQLAlchemy 2.0 + asyncpg, single additive migration chain.

URL resolution order (first non-empty wins; no secrets are ever loaded from this file):
  1. ``ALEMBIC_DATABASE_URL`` (migration-only override, e.g. a shadow DB in CI);
  2. ``DATABASE_URL`` (standard runtime URL; ``postgresql://`` is upgraded to asyncpg);
  3. the application settings (``app.core.config.get_settings().database_url``);
  4. ``alembic.ini`` ``sqlalchemy.url`` (empty by default).

Used by both CLI (``alembic -c alembic.ini …``) and ``scripts/deploy_set.sh``. Offline mode
(``alembic upgrade head --sql``) renders SQL without connecting; it requires a URL to be configured.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

# Surface our package path even when invoked from a bare worktree without an installed dist.
# This must run before importing ``app.*`` modules (Alembic may be invoked from any cwd).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.migrations.base import discover_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = discover_metadata()


def _normalise_async_url(raw: str) -> str:
    """Force an asyncpg driver for SQLAlchemy's async engine; leave other async drivers alone."""
    url = raw.strip()
    if not url:
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    return url


def get_database_url() -> str:
    """Resolve the migration database URL without leaking credentials into logs/config files."""
    candidates = (
        os.environ.get("ALEMBIC_DATABASE_URL"),
        os.environ.get("DATABASE_URL"),
        config.get_main_option("sqlalchemy.url"),
    )
    for candidate in candidates:
        if candidate and candidate.strip():
            return _normalise_async_url(candidate)
    try:  # optional: reuse the application's settings (which may read .env)
        from app.core.config import get_settings

        return _normalise_async_url(get_settings().database_url)
    except Exception:  # noqa: BLE001 - migrations must give a clear error when unset
        return ""


_URL = get_database_url()
if not _URL:
    raise SystemExit(
        "DATABASE_URL is not set. Set DATABASE_URL (or ALEMBIC_DATABASE_URL) to a "
        "postgresql+asyncpg:// URL before running Alembic."
    )

config.set_main_option("sqlalchemy.url", _URL.replace("%", "%%"))


def _configure(connection: Connection | None, *, offline: bool = False) -> None:
    context.configure(
        connection=connection,
        url=None if connection is not None else _URL,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        literal_binds=offline,
        render_as_batch=False,
        # One version table for the single chain; schema set by the chain's first revision.
        version_table="alembic_version",
        # Fail loudly instead of silently rendering a branch point if a bad merge happens.
        transaction_per_migration=True,
        # Enforce 4-digit revision ids and single-head generation for every `alembic revision` call.
        process_revision_directives=process_revision_directives,
    )


def run_migrations_offline() -> None:
    """Render migration SQL to stdout (``alembic upgrade head --sql``) without a DB connection."""
    _configure(None, offline=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against the async database (single connection, no pooling)."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _URL
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=NullPool,
        future=True,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def _guard_revision_id(rev_id: str) -> None:
    """Reject autogenerated random ids; builders must pass the pre-allocated 4-digit revision id."""
    import re

    if not re.match(r"^\d{4}_[a-z0-9_]+$", rev_id or ""):
        raise SystemExit(
            f"Invalid revision id {rev_id!r}: use the pre-allocated 4-digit id, e.g. "
            "`alembic revision --rev-id 0004_item_master -m '…'` (see design/DATA-MODEL.md §21.1)."
        )


def process_revision_directives(context, revision, directives):
    """Enforce 4-digit revision ids and a single head at generation time (no autogenerate drift)."""
    if not directives:
        return
    script: ScriptDirectory = context.script
    for directive in directives:
        rev_id = getattr(directive, "rev_id", None)
        if rev_id:
            _guard_revision_id(rev_id)
        else:
            raise SystemExit(
                "Autogenerated revision ids are not allowed: pass --rev-id <NNNN_slug> "
                "(pre-allocated in design/DATA-MODEL.md §21.1)."
            )
    heads = script.get_heads()
    if len(heads) > 1:
        raise SystemExit(f"Multiple heads {heads!r}; merge/rebase the chain before generating.")


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
