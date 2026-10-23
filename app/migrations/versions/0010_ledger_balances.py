"""0010_ledger_balances — FS-04 materialised balance/bin/lot projections + availability generated column."""

from __future__ import annotations

from alembic import op

revision = "0010_ledger_balances"
down_revision = "0009_ledger_commitment_guards"
branch_labels = None
depends_on = None

SCHEMA = "inventory_ledger"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

BALANCE_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.inventory_balance (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES platform.tenant(id),
    item_id uuid NOT NULL,
    location_id uuid NOT NULL,
    inventory_status_id uuid NOT NULL,
    ownership text NOT NULL DEFAULT 'OWNED',
    qty_class text NOT NULL DEFAULT 'ON_HAND',
    lot_id uuid,
    serial_id uuid,
    qty_on_hand numeric(38,12) NOT NULL DEFAULT 0,
    qty_committed numeric(38,12) NOT NULL DEFAULT 0,
    qty_on_order numeric(38,12) NOT NULL DEFAULT 0,
    qty_backordered numeric(38,12) NOT NULL DEFAULT 0,
    value_base numeric(38,12) NOT NULL DEFAULT 0,
    last_ledger_sequence integer,
    last_posting_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    version integer NOT NULL DEFAULT 1,
    qty_available numeric(38,12) GENERATED ALWAYS AS (GREATEST(qty_on_hand - qty_committed, 0)) STORED,
    CONSTRAINT uq_inventory_balance_key UNIQUE NULLS NOT DISTINCT
        (tenant_id, item_id, location_id, inventory_status_id, ownership, qty_class, lot_id, serial_id),
    CONSTRAINT uq_inventory_balance_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT ck_balance_committed CHECK (qty_committed >= 0),
    CONSTRAINT ck_balance_serial CHECK (serial_id IS NULL OR qty_on_hand <= 1)
);
CREATE INDEX IF NOT EXISTS ix_balance_position ON {SCHEMA}.inventory_balance (tenant_id, item_id, location_id, inventory_status_id);
CREATE INDEX IF NOT EXISTS ix_balance_location ON {SCHEMA}.inventory_balance (tenant_id, location_id, item_id);
"""

BIN_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.inventory_balance_bin (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES platform.tenant(id),
    item_id uuid NOT NULL,
    location_id uuid NOT NULL,
    bin_id uuid NOT NULL,
    inventory_status_id uuid NOT NULL,
    ownership text NOT NULL DEFAULT 'OWNED',
    lot_id uuid,
    serial_id uuid,
    qty_on_hand numeric(38,12) NOT NULL DEFAULT 0,
    last_ledger_sequence integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    version integer NOT NULL DEFAULT 1,
    CONSTRAINT uq_inventory_balance_bin_key UNIQUE NULLS NOT DISTINCT
        (tenant_id, item_id, location_id, bin_id, inventory_status_id, ownership, lot_id, serial_id),
    CONSTRAINT uq_inventory_balance_bin_tenant_id_id UNIQUE (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS ix_balance_bin_bin ON {SCHEMA}.inventory_balance_bin (tenant_id, bin_id);
CREATE INDEX IF NOT EXISTS ix_balance_bin_item ON {SCHEMA}.inventory_balance_bin (tenant_id, item_id, location_id);
"""

LOT_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.inventory_balance_lot (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES platform.tenant(id),
    item_id uuid NOT NULL,
    location_id uuid NOT NULL,
    lot_id uuid NOT NULL,
    inventory_status_id uuid NOT NULL,
    ownership text NOT NULL DEFAULT 'OWNED',
    qty_on_hand numeric(38,12) NOT NULL DEFAULT 0,
    qty_committed numeric(38,12) NOT NULL DEFAULT 0,
    expiry_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    version integer NOT NULL DEFAULT 1,
    CONSTRAINT uq_inventory_balance_lot_key UNIQUE
        (tenant_id, item_id, location_id, lot_id, inventory_status_id, ownership),
    CONSTRAINT uq_inventory_balance_lot_tenant_id_id UNIQUE (tenant_id, id),
    CONSTRAINT ck_balance_lot_committed CHECK (qty_committed >= 0)
);
CREATE INDEX IF NOT EXISTS ix_balance_lot_lot ON {SCHEMA}.inventory_balance_lot (tenant_id, lot_id);
CREATE INDEX IF NOT EXISTS ix_balance_lot_expiry ON {SCHEMA}.inventory_balance_lot (tenant_id, item_id, expiry_date);
"""


def _rls() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for name in ("inventory_balance", "inventory_balance_bin", "inventory_balance_lot"):
        op.execute(f'ALTER TABLE {SCHEMA}."{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
        op.execute(f'DROP POLICY IF EXISTS system_tenant_read ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE POLICY system_tenant_read ON {SCHEMA}."{name}" FOR SELECT '
            f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)"
        )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.inventory_balance, {SCHEMA}.inventory_balance_bin, {SCHEMA}.inventory_balance_lot TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _execute_script(script: str) -> None:
    for statement in script.split(";\n"):
        if statement.strip():
            op.execute(statement.strip())


def upgrade() -> None:
    _execute_script(BALANCE_DDL)
    _execute_script(BIN_DDL)
    _execute_script(LOT_DDL)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
        """
    )
    for name in ("inventory_balance", "inventory_balance_bin", "inventory_balance_lot"):
        op.execute(f'DROP TRIGGER IF EXISTS trg_{name}_touch ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE TRIGGER trg_{name}_touch BEFORE UPDATE ON {SCHEMA}."{name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    _rls()


def downgrade() -> None:
    for name in ("inventory_balance_lot", "inventory_balance_bin", "inventory_balance"):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{name}" CASCADE')
