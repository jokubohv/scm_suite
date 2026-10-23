"""0025_wms_execution — FS-11 outbound + inventory-execution (17 tables): waves/schedules, pick tasks and
lines, pack tasks/cartons, shipments/manifests, replenishment plans/tasks, directed status-change tasks
and the backend cart/device-session containers (``EXT``; no mobile protocol, D8).

Chain position: after ``0024_wms_inbound``. Additive. Task completions post through the frozen
``inventory.ledger`` (T-29 bin transfer, T-30 status change, T-12 fulfilment), ``procurement.p2p``
(returns) and ``manufacturing.execution`` (work-order build) contracts; no ledger writes happen here.
"""

from __future__ import annotations

from alembic import op

from app.modules.wms import migration_utils as u
from app.modules.wms import tables as t

revision = "0025_wms_execution"
down_revision = "0024_wms_inbound"
branch_labels = None
depends_on = None


def upgrade() -> None:
    deferred: list = []
    for table in t.REVISION_0025_TABLES:
        u.create_table(table, deferred)
    u.add_fks(deferred)
    u.triggers(t.REVISION_0025_TABLES)
    u.rls_and_grants(t.REVISION_0025_TABLES)


def downgrade() -> None:
    for table in reversed(t.REVISION_0025_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {u.SCHEMA}."{table.name}" CASCADE')
