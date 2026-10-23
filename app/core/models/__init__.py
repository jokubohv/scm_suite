"""SQLAlchemy metadata for platform-core tables (FS-00 data layer).

The platform schema is owned by the FS-00 migrations ``0001_platform_core``,
``0002_platform_security`` and ``0003_platform_events``. This package exposes Core table
definitions (SQLAlchemy 2.0) so autogenerate/CI can compare code metadata against the migrated
database; the Alembic revisions remain the authoritative DDL (reviewed, explicit SQL).

Discovery note: ``app/migrations/env.py`` collects metadata from ``app.db.*`` / ``app.core.db.*``
(S0-03). The integrator wires this package into that discovery (see ``app/core/models/platform.py``
``metadata`` export); until then migrations do not depend on import-time metadata.
"""

from .platform import Base, metadata  # noqa: F401

__all__ = ["Base", "metadata"]
