"""Allow authenticated reference maintenance to write global currency rows.

The currency API already requires platform.finance.sync and writes tenant_id NULL. The
original tenant_isolation policy only permits tenant-owned writes, so restricted app logins
could read global currencies but INSERT failed and UPDATE matched no rows. Add only the
missing INSERT/UPDATE policies; retain RLS, table grants and all tenant-owned policies.
Capability authorization remains at the API boundary, as for other platform writes.
"""

from __future__ import annotations

from alembic import op

revision = "0032_currency_global_writes"
down_revision = "0031_count_post_txn_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Policies grant no table privileges. A caller still needs the existing INSERT/UPDATE
    # grants and a request tenant context; no auth-role switch or BYPASSRLS is needed.
    predicate = "tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL"
    op.execute(
        f"CREATE POLICY global_currency_insert ON platform.currency FOR INSERT WITH CHECK ({predicate})"
    )
    op.execute(
        f"CREATE POLICY global_currency_update ON platform.currency FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    # Restore the prior write restrictions without deleting currencies or their audit history.
    op.execute("DROP POLICY IF EXISTS global_currency_update ON platform.currency")
    op.execute("DROP POLICY IF EXISTS global_currency_insert ON platform.currency")
