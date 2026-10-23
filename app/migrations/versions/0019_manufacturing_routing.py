"""0019_manufacturing_routing — FS-09 routings/steps/charges, work centres/groups/calendars and
manufacturing cost templates (9 tables), plus the deferred ``assembly_item.default_routing_id → routing``
foreign key (routing is created here, after the 0018 master revision).

Chain: ``0018_manufacturing_master`` → ``0019_manufacturing_routing`` → ``0020_manufacturing_execution``.
Additive; downgrade drops only the tables/FK created here (0020 objects are dropped by their own
downgrade first).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.manufacturing import tables as t

revision = "0019_manufacturing_routing"
down_revision = "0018_manufacturing_master"
branch_labels = None
depends_on = None

SCHEMA = "manufacturing"
APP_ROLE = "platform_app"


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        deferred_fks.append(fk)


def _add_fks(deferred_fks: list) -> None:
    for fk in deferred_fks:
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
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
        op.execute(
            f'CREATE POLICY system_tenant_read ON {SCHEMA}."{name}" FOR SELECT '
            f"USING (tenant_id = '00000000-0000-0000-0000-000000000000'::uuid)"
        )
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
    for table in t.ROUTING_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    op.execute(
        f'ALTER TABLE {SCHEMA}.assembly_item ADD CONSTRAINT "fk_routing_default_routing_id" '
        f"FOREIGN KEY (tenant_id, default_routing_id) REFERENCES {SCHEMA}.routing (tenant_id, id)"
    )
    _triggers(t.ROUTING_TABLES)
    _rls_and_grants(t.ROUTING_TABLES)


def downgrade() -> None:
    op.execute(f'ALTER TABLE {SCHEMA}.assembly_item DROP CONSTRAINT IF EXISTS "fk_routing_default_routing_id"')
    for table in reversed(t.ROUTING_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
