"""0023_wms_setup — FS-11 WMS setup/configuration (15 tables) + per-tenant seeds.

Chain position: depends on FS-10 ``0022_planning_atp`` (slot M11 starts 0023). Additive; downgrade drops
the ``wms`` schema (0024/0025 objects are dropped by their own downgrades first).

Tables: warehouse extension of a location, item process families/groups/assignments, item stocking
rules, order types, UCC formats, carrier service levels, pick/putaway strategies + rules, system-rule
catalogue/overrides and bin blocks. Seeds: ``wms.execution.enabled`` (off), ``wms.*`` tenant settings and
the system-rule catalogue. No ledger/inventory writes here.
"""

from __future__ import annotations

from alembic import op

from app.modules.wms import migration_utils as u
from app.modules.wms import tables as t

revision = "0023_wms_setup"
down_revision = "0022_planning_atp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {u.SCHEMA}")
    u.schema_functions()
    deferred: list = []
    for table in t.REVISION_0023_TABLES:
        u.create_table(table, deferred)
    u.add_fks(deferred)
    u.triggers(t.REVISION_0023_TABLES)
    u.rls_and_grants(t.REVISION_0023_TABLES)
    u.seeds_0023()


def downgrade() -> None:
    u.drop_schema()
