"""Seed loader constants and default paths.

All values are deterministic so that two loads of the same seed revision produce byte-identical
repositories and reports (S0 exit check: ``seeds idempotent (load twice, no diff)``).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

SEED_SCHEMA_VERSION = "1.0.0"

#: Reserved tenant id for global reference rows (DATA-MODEL §1.2, DM-GLOBAL-10). UUIDv7-shaped:
#: version nibble 7, RFC 4122 variant. The zero payload marks it as the reserved system tenant.
SYSTEM_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")

#: Seed data directory shipped with the package.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"

#: Authoritative RBAC catalogue (read-only plan artifact). Overridable via ``--permissions-source``.
DEFAULT_PERMISSIONS_SOURCE = Path(__file__).resolve().parents[2] / "design" / "permissions.json"

#: Checked-in copy used when the plan package is not available (e.g. installed wheel/container).
FIXTURE_PERMISSIONS_SOURCE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "permissions.json"

#: Natural tenant-scope values used by the manifest.
TENANT_SCOPE_TENANT = "TENANT"
TENANT_SCOPE_SYSTEM = "SYSTEM"

#: RBAC policy expected by ``design/permissions.json`` v1.x (validated, never silently relaxed).
EXPECTED_CATALOGUE_COUNTS: dict[str, int] = {
    "scopes": 255,
    "assignable_scopes": 241,
    "platform_only": 1,
    "service_only": 13,
    "aliases": 1,
    "roles": 7,
    "sod_pairs": 14,
    "requires_approval": 18,
}

#: Documented role-template sizes (design/permissions.json + DATA-MODEL §21.2). Used as a warning
#: if the authoritative catalogue drifts; hard validation uses the catalogue's own ``counts`` block.
DOCUMENTED_ROLE_SIZES: dict[str, int] = {
    "admin": 241,
    "planner": 50,
    "buyer": 71,
    "warehouse_operator": 81,
    "cost_accountant": 57,
    "quality_inspector": 47,
    "readonly": 44,
}

#: Expected number of seed datasets in ``app/seed/data/manifest.json`` (kept explicit for the gate).
EXPECTED_DATASET_COUNT = 13

#: Names of every number sequence documented in DATA-MODEL §21.2 (order preserved for readability).
DOCUMENTED_NUMBER_SEQUENCES: tuple[str, ...] = (
    "PO",
    "IR",
    "VB",
    "VC",
    "VRA",
    "TO",
    "IT",
    "IA",
    "IW",
    "CNT",
    "AB",
    "AU",
    "WO",
    "WIP",
    "LC",
    "REV",
    "ECN",
    "CONS",
    "WAVE",
    "SHIP",
    "TASK",
    "IMPORT",
    "MFGSCH",
    "DP",
    "SP",
    "ALC",
    "AIM",
)

#: Documented posting transaction types (ARCHITECTURE §2.3 / DATA-MODEL §18).
DOCUMENTED_TRANSACTION_TYPES: tuple[str, ...] = tuple(f"T-{n:02d}" for n in range(1, 32))

#: Documented inventory statuses (DATA-MODEL §21.2 + Δ-REUSE additions).
DOCUMENTED_INVENTORY_STATUSES: tuple[str, ...] = (
    "AVAILABLE",
    "HOLD",
    "DAMAGED",
    "RETURNED",
    "CONSIGNED_GOOD",
    "IN_TRANSIT_OWNED",
    "USED",
    "REFURB",
)
