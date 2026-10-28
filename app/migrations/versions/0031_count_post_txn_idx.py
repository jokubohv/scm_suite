"""0031_inventory_count_posted_txn_partial_unique — allow multiple unposted counts per tenant.

The FS-07 ``uq_inventory_count_posted_txn`` constraint used ``NULLS NOT DISTINCT`` on
``(tenant_id, posted_transaction_id)``, so a tenant could hold only one count whose
``posted_transaction_id`` was NULL. Every additional DRAFT/OPEN count failed with an unhandled 500
(P1 runtime probe: count create after the first count). Replace it with a partial unique index that
still enforces one posted transaction per count while permitting many unposted counts.

Revision ID: 0031_count_post_txn_idx
Revises: 0030_rls_grants_verify
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_count_post_txn_idx"
down_revision = "0030_rls_grants_verify"
branch_labels = None
depends_on = None

SCHEMA = "counts"
TABLE = "inventory_count"
CONSTRAINT = "uq_inventory_count_posted_txn"


def upgrade() -> None:
    # 0014 compiles the *current* table metadata, so a fresh database built after this fix already
    # creates the partial index; an existing database has the old NULLS NOT DISTINCT constraint.
    # Handle both states idempotently.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{CONSTRAINT}' AND conrelid = '{SCHEMA}.{TABLE}'::regclass
            ) THEN
                ALTER TABLE {SCHEMA}.{TABLE} DROP CONSTRAINT {CONSTRAINT};
            END IF;
        END $$;
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {CONSTRAINT} ON {SCHEMA}.{TABLE}"
        " (tenant_id, posted_transaction_id) WHERE posted_transaction_id IS NOT NULL"
    )


def downgrade() -> None:
    # Restoring the old constraint fails if more than one unposted count exists (the bug this fixes);
    # that is the correct guard: data must be reconciled before a rollback rather than silently
    # deleting business documents.
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.{CONSTRAINT}")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{CONSTRAINT}' AND conrelid = '{SCHEMA}.{TABLE}'::regclass
            ) THEN
                ALTER TABLE {SCHEMA}.{TABLE} ADD CONSTRAINT {CONSTRAINT}
                    UNIQUE NULLS NOT DISTINCT (tenant_id, posted_transaction_id);
            END IF;
        END $$;
        """
    )
