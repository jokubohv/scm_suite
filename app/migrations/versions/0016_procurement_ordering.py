"""0016_procurement_ordering — FS-08 requisitions/purchase requests/RFQ and purchase orders with
blanket releases (11 tables). Chain position after ``0015_procurement_vendors``; additive."""

from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.procurement import tables as t

revision = "0016_procurement_ordering"
down_revision = "0015_procurement_vendors"
branch_labels = None
depends_on = None

SCHEMA = "procurement"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"


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


def _triggers(tables) -> None:
    for table in tables:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )


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


def upgrade() -> None:
    deferred: list = []
    for table in t.REVISION_0016_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _triggers(t.REVISION_0016_TABLES)
    _rls_and_grants(t.REVISION_0016_TABLES)


def downgrade() -> None:
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in reversed(t.REVISION_0016_TABLES))
    op.execute(f"DROP TABLE IF EXISTS {names} CASCADE")
