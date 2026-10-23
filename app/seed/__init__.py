"""S0-07 seed/reference bootstrap package.

Data-driven, idempotent, tenant-scoped seed loader. At S0 the physical SQLAlchemy models do not
exist yet (FS-00 builds them), so the loader is repository-agnostic: the shipped implementation
targets an in-memory repository, and :class:`app.seed.repository.SqlAlchemySeedRepository` is the
documented integration hook for FS-00/migrations once the platform tables land.

Entry points:
    python -m app.seed.cli --tenant <uuid> [--dry-run] [--json]

Sources of truth (read-only): ``design/DATA-MODEL.md`` §21.2 seed table,
``design/permissions.json`` (authoritative RBAC catalogue), ``design/FEATURE-SETS.md`` manifest flags
and ``design/ARCHITECTURE.md`` §2.3 posting matrix. Seed payloads live in ``app/seed/data/*.json``.
"""

from app.seed.constants import SEED_SCHEMA_VERSION, SYSTEM_TENANT_ID
from app.seed.loader import SeedLoader
from app.seed.permissions import PermissionCatalogue, load_permissions
from app.seed.report import SeedReport
from app.seed.repository import MemoryRepository, SeedRepository, build_repository

__all__ = [
    "SEED_SCHEMA_VERSION",
    "SYSTEM_TENANT_ID",
    "MemoryRepository",
    "PermissionCatalogue",
    "SeedLoader",
    "SeedReport",
    "SeedRepository",
    "build_repository",
    "load_permissions",
]
