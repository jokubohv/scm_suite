"""0005_item_master_uom — FS-01 UoM override/item assignments + base-UoM FK + job seeds.

Adds the ``uom_conversion`` and ``item_uom`` tables (DATA-MODEL §4), closes the internal
``uom_type.base_uom_id → uom`` FK (UOM-05), ensures the Δ-FS01-1 ``must_be_whole_base_unit`` column,
installs standard/RLS/grants and seeds the two FS-01 job definitions in the SYSTEM tenant.

Revision ID: 0005_item_master_uom
Revises: 0004_item_master
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.models.platform import tenant as _platform_tenant
from app.modules.item_master import tables as t

if "platform.tenant" not in t.metadata.tables:  # FK resolution for compiled DDL
    _platform_tenant.to_metadata(t.metadata)

revision = "0005_item_master_uom"
down_revision = "0004_item_master"
branch_labels = None
depends_on = None

SCHEMA = "item_master"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

JOB_SEEDS: tuple[tuple[str, str, str, str, int], ...] = (
    ("item.reference.validate", "default", "item_master.reference.validate", "0 4 * * *", 900),
    ("item.inventory.defaults.repair", "default", "item_master.inventory.defaults.repair", "30 4 * * *", 900),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table) -> None:
    op.execute(str(CreateTable(table).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())


def upgrade() -> None:
    # Δ-FS01-1: additive column (present in 0004 metadata for module parity; harmless if already there).
    op.execute(f"ALTER TABLE {SCHEMA}.item_inventory ADD COLUMN IF NOT EXISTS must_be_whole_base_unit boolean NOT NULL DEFAULT false")
    for table in t.REVISION_0005_TABLES:
        _create_table(table)
    op.execute(
        f"ALTER TABLE {SCHEMA}.uom_type DROP CONSTRAINT IF EXISTS fk_uom_type_base"
    )
    op.execute(
        f"ALTER TABLE {SCHEMA}.uom_type ADD CONSTRAINT fk_uom_type_base "
        f"FOREIGN KEY (tenant_id, base_uom_id) REFERENCES {SCHEMA}.uom (tenant_id, id)"
    )
    for table in t.REVISION_0005_TABLES:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{table.name}_touch ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    setting = "current_setting('app.tenant_id', true)"
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0005_TABLES)
    for table in t.REVISION_0005_TABLES:
        op.execute(f'ALTER TABLE {SCHEMA}."{table.name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{table.name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
        op.execute(f'DROP POLICY IF EXISTS system_tenant_read ON {SCHEMA}."{table.name}"')
        op.execute(
            f'CREATE POLICY system_tenant_read ON {SCHEMA}."{table.name}" FOR SELECT '
            f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)"
        )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {names} TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"item_master/jobs/{name}")
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts, singleton,
                 is_enabled, created_at, updated_at, version)
            VALUES ('{job_id}', '{SYSTEM_TENANT_ID}', '{name}', '{queue}', '{handler}', '{cron}',
                    {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def downgrade() -> None:
    for name, _queue, _handler, _cron, _timeout in JOB_SEEDS:
        op.execute(f"DELETE FROM platform.job_definition WHERE tenant_id = '{SYSTEM_TENANT_ID}' AND name = '{name}'")
    op.execute(f"ALTER TABLE {SCHEMA}.uom_type DROP CONSTRAINT IF EXISTS fk_uom_type_base")
    for table in reversed(t.REVISION_0005_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
