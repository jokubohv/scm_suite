"""0022_planning_atp — FS-10 ATP projection + AIM replenishment calculations (3 tables).

Creates ``atp_projection`` (durable clamped projection with persisted shortage columns per
DATA-MODEL §13/DM-PLAN-05), ``replenishment_calculation`` and ``replenishment_calculation_line``;
adds updated-at triggers, tenant RLS/grants. Chain: ``0021_planning`` → ``0022_planning_atp``.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.replenishment import tables as t

revision = "0022_planning_atp"
down_revision = "0021_planning"
branch_labels = None
depends_on = None

SCHEMA = "replenishment"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"


def _dialect():
    return postgresql.dialect()


def _create_table(table, deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]]) -> None:
    op.execute(str(CreateTable(table, include_foreign_key_constraints=[]).compile(dialect=_dialect())).strip())
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=_dialect())).strip())
    for fk in table.foreign_key_constraints:
        local_columns = [column.name for column in fk.columns]
        targets = [element.target_fullname.split(".") for element in fk.elements]
        ref_schema, ref_table = targets[0][0], targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _triggers() -> None:
    for table in t.REVISION_0022_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_atp_projection() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.projected_available < 0 THEN
                RAISE EXCEPTION 'ATP projected_available must be clamped at zero (INV-22)';
            END IF;
            IF NEW.shortage_qty < 0 THEN
                RAISE EXCEPTION 'ATP shortage_qty cannot be negative';
            END IF;
            IF NEW.shortage_date IS NOT NULL AND NEW.shortage_qty <= 0 THEN
                RAISE EXCEPTION 'ATP shortage_date requires a positive shortage_qty';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_atp_projection_guard BEFORE INSERT OR UPDATE ON {SCHEMA}."atp_projection" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_atp_projection()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0022_TABLES:
        name = table.name
        op.execute(f'ALTER TABLE {SCHEMA}."{name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}."{name}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON {SCHEMA}."{name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0022_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {names} TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    deferred_fks: list[tuple[str, str, list[str], str, str, list[str]]] = []
    for table in t.REVISION_0022_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table_name}" ADD CONSTRAINT "{fk_name}" '
            f'FOREIGN KEY ({", ".join(local_columns)}) REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )
    _triggers()
    _rls_and_grants()


def downgrade() -> None:
    for table in reversed(t.REVISION_0022_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
