"""0009_ledger_commitment_guards — FS-04 commitment/transfer guard constraints and triggers."""

from __future__ import annotations

from alembic import op

revision = "0009_ledger_commitment_guards"
down_revision = "0008_ledger_core"
branch_labels = None
depends_on = None

SCHEMA = "inventory_ledger"


def upgrade() -> None:
    # One ACTIVE commitment per logical source line (terminal states remain for audit).
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_commitment_active_source "
        f"ON {SCHEMA}.inventory_commitment (tenant_id, source_type, source_line_ref) WHERE status = 'ACTIVE'"
    )
    # A serial can be reserved by at most one live allocation.
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_alloc_serial "
        f"ON {SCHEMA}.inventory_commitment_allocation (tenant_id, serial_id) "
        f"WHERE serial_id IS NOT NULL AND released_at IS NULL"
    )
    # qty_in_transit = qty_fulfilled - qty_received (INV-05) enforced by trigger.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.check_transfer_order_line_in_transit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.qty_in_transit IS DISTINCT FROM (NEW.qty_fulfilled - NEW.qty_received) THEN
                RAISE EXCEPTION 'qty_in_transit must equal qty_fulfilled - qty_received'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(f'DROP TRIGGER IF EXISTS trg_to_line_in_transit ON {SCHEMA}.transfer_order_line')
    op.execute(
        f'CREATE TRIGGER trg_to_line_in_transit BEFORE INSERT OR UPDATE ON {SCHEMA}.transfer_order_line '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.check_transfer_order_line_in_transit()"
    )
    # Refuse tracking of negative on-hand positions under BLOCK policy; service still owns policy detail.
    op.execute(f'ALTER TABLE {SCHEMA}.inventory_commitment ADD CONSTRAINT ck_commitment_released_qty CHECK (released_qty >= 0)')


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.inventory_commitment DROP CONSTRAINT IF EXISTS ck_commitment_released_qty")
    op.execute(f'DROP TRIGGER IF EXISTS trg_to_line_in_transit ON {SCHEMA}.transfer_order_line')
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.check_transfer_order_line_in_transit()")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.uq_alloc_serial")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.uq_commitment_active_source")
