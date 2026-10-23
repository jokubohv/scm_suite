"""0017_procurement_billing — FS-08 receipts/receipt details, vendor bills/lines/variances/3-way match,
vendor credits, vendor returns and drop-ship/special-order links (12 tables); closes the three deferred
FS-05 foreign keys and registers the four procurement jobs. Additive."""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.procurement import tables as t

revision = "0017_procurement_billing"
down_revision = "0016_procurement_ordering"
branch_labels = None
depends_on = None

SCHEMA = "procurement"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

JOB_SEEDS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("procurement.match.run", "default", "procurement.match.run", "*/15 * * * *", 900),
    ("procurement.po.close_overdue", "default", "procurement.po.close_overdue", "0 3 * * *", 1800),
    ("procurement.receipt.reconcile", "default", "procurement.receipt.reconcile", "15 2 * * *", 1800),
    ("procurement.partition.maintenance", "default", "procurement.partition.maintenance", "0 4 1 * *", 900),
)


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred: list) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    deferred.extend(table.foreign_key_constraints)


def _add_fks(deferred: list) -> None:
    for fk in deferred:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        op.execute(
            f'ALTER TABLE {SCHEMA}."{fk.table.name}" ADD CONSTRAINT "{fk.name}" '
            f"FOREIGN KEY ({', '.join(local_columns)}) "
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )


def _close_costing_fks() -> None:
    op.execute(
        "ALTER TABLE costing.landed_cost ADD CONSTRAINT fk_landed_cost_vendor "
        "FOREIGN KEY (tenant_id, vendor_id) REFERENCES procurement.vendor (tenant_id, id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE costing.landed_cost_allocation ADD CONSTRAINT fk_landed_alloc_receipt_line "
        "FOREIGN KEY (tenant_id, item_receipt_line_id) REFERENCES procurement.item_receipt_line (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE costing.cost_variance ADD CONSTRAINT fk_cost_variance_vendor_bill_line "
        "FOREIGN KEY (tenant_id, vendor_bill_line_id) REFERENCES procurement.vendor_bill_line (tenant_id, id)"
    )


def _triggers(tables) -> None:
    for table in tables:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    # No procurement table is DB append-only (see app/modules/procurement/tables.py): variance rows are
    # stamped with posted_transaction_id on T-06 posting; document immutability is enforced by services.


def _rls_and_grants(tables) -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in tables:
        name = table.name
        op.execute(f'ALTER TABLE {SCHEMA}."{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
                   f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)")
        op.execute(f'CREATE POLICY system_tenant_read ON {SCHEMA}."{name}" FOR SELECT '
                   f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)")
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in tables)
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


def _seed_jobs() -> None:
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        cron_sql = f"'{cron}'" if cron else "NULL"
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts,
                 singleton, is_enabled, created_at, updated_at, version)
            VALUES
                ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{handler}', {cron_sql},
                 {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    deferred: list = []
    for table in t.REVISION_0017_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _close_costing_fks()
    _triggers(t.REVISION_0017_TABLES)
    _rls_and_grants(t.REVISION_0017_TABLES)
    _seed_jobs()


def downgrade() -> None:
    op.execute("ALTER TABLE costing.cost_variance DROP CONSTRAINT IF EXISTS fk_cost_variance_vendor_bill_line")
    op.execute("ALTER TABLE costing.landed_cost_allocation DROP CONSTRAINT IF EXISTS fk_landed_alloc_receipt_line")
    op.execute("ALTER TABLE costing.landed_cost DROP CONSTRAINT IF EXISTS fk_landed_cost_vendor")
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in reversed(t.REVISION_0017_TABLES))
    op.execute(f"DROP TABLE IF EXISTS {names} CASCADE")
