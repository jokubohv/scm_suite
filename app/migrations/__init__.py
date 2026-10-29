"""Alembic migration environment package.

The platform keeps exactly **one** additive migration chain under ``app/migrations/versions/``.
Revision identifiers are 4-digit-prefixed (``0001_platform_core`` … ``0032_currency_global_writes``)
and are pre-allocated in ``design/DATA-MODEL.md`` §21.1 / ``design/feature-sets.json``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
