"""Shared SQLAlchemy metadata + naming conventions for the single Alembic chain.

Authority: ``design/DATA-MODEL.md`` §1.1 (constraint/index naming) and §21 (single additive chain).
Feature-set modules may export their own ``MetaData``/``Base``; :func:`discover_metadata` merges those
into :data:`target_metadata` so one Alembic environment sees the whole schema. No domain models live in
this package — FS-00 owns the first platform revisions (0001–0003).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from sqlalchemy import MetaData

# DATA-MODEL §1.1:
#   pk_<table>, uq_<table>_<cols>, fk_<table>_<ref>, ck_<table>_<rule>, ix_<table>_<cols>.
# Trigger names (trg_<table>_<action>) are raw DDL and are not represented in SQLAlchemy conventions.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Metadata shared by all migrations; autogenerate compares against this object.
target_metadata = MetaData(naming_convention=NAMING_CONVENTION)

#: Modules that feature sets may use to expose their declarative models/metadata.
_DISCOVERY_CANDIDATES: tuple[str, ...] = (
    "app.db.base",
    "app.db.models",
    "app.core.db.base",
    "app.core.db.models",
    "app.core.models",
)


def _merge_metadata(target: MetaData, source: MetaData) -> None:
    """Copy every table from ``source`` into ``target`` exactly once."""
    existing = {table.key for table in target.tables.values()}
    for table in source.tables.values():
        if table.key not in existing:
            table.to_metadata(target)


def discover_metadata(
    candidates: Iterable[str] = _DISCOVERY_CANDIDATES,
) -> MetaData:
    """Collect metadata exported by feature modules (best-effort; empty schema is valid in S0).

    A module participates if it defines a module-level ``metadata`` attribute that is a
    :class:`sqlalchemy.MetaData` (or a ``Base`` whose ``.metadata`` is one). Missing modules are
    ignored so the S0 baseline migration environment works before FS-00 lands its models.
    """
    seen_ids = {id(target_metadata)}
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        except Exception:  # noqa: BLE001 - discovery must never break migrations on import bugs
            continue
        metadata = getattr(module, "metadata", None)
        if metadata is None:
            base = getattr(module, "Base", None)
            metadata = getattr(base, "metadata", None)
        if isinstance(metadata, MetaData) and id(metadata) not in seen_ids:
            _merge_metadata(target_metadata, metadata)
            seen_ids.add(id(metadata))
    return target_metadata


__all__ = ["NAMING_CONVENTION", "discover_metadata", "target_metadata"]
