"""0024_wms_inbound — FS-11 inbound processing (6 tables): ASN/inbound shipments, receiving orders
and directed putaway tasks/lines.

Chain position: after ``0023_wms_setup``. Additive; receipts themselves are posted through the frozen
``procurement.p2p`` contract (``item_receipt``) and putaway moves post T-29 through ``inventory.ledger``;
this revision only creates the WMS execution documents.
"""

from __future__ import annotations

from alembic import op

from app.modules.wms import migration_utils as u
from app.modules.wms import tables as t

revision = "0024_wms_inbound"
down_revision = "0023_wms_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    deferred: list = []
    for table in t.REVISION_0024_TABLES:
        u.create_table(table, deferred)
    u.add_fks(deferred)
    u.triggers(t.REVISION_0024_TABLES)
    u.rls_and_grants(t.REVISION_0024_TABLES)


def downgrade() -> None:
    for table in reversed(t.REVISION_0024_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {u.SCHEMA}."{table.name}" CASCADE')
