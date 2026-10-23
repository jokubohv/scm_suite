"""0012_costing_standard — FS-05 standard costing, rollups and T-24 revaluation documents (7 tables).

Creates the standard-costing tables from ``app.modules.costing.tables`` (revision-0012 group):
``cost_version``, ``planned_standard_cost``, ``standard_cost``, ``cost_rollup_run``,
``cost_rollup_line``, ``cost_revaluation``, ``cost_revaluation_line``; adds the effective-window
overlap guard on ``standard_cost`` (DM-COST-05), updated-at triggers, RLS/grants.

Chain position: depends on ``0011_costing``. Additive; downgrade drops the seven tables.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.costing import tables as t

revision = "0012_costing_standard"
down_revision = "0011_costing"
branch_labels = None
depends_on = None

SCHEMA = "costing"
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
        ref_schema = targets[0][0]
        ref_table = targets[0][1]
        ref_columns = [parts[2] for parts in targets]
        deferred_fks.append((fk.name, table.name, local_columns, ref_schema, ref_table, ref_columns))


def _triggers() -> None:
    for table in t.REVISION_0012_TABLES:
        op.execute(
            f'CREATE TRIGGER trg_{table.name}_touch BEFORE UPDATE ON {SCHEMA}."{table.name}" '
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_updated_at()"
        )
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_standard_cost_overlap() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.standard_cost sc
                 WHERE sc.tenant_id = NEW.tenant_id
                   AND sc.item_id = NEW.item_id
                   AND sc.cost_category_id = NEW.cost_category_id
                   AND sc.id <> NEW.id
                   AND daterange(sc.effective_from, COALESCE(sc.effective_to, 'infinity'::date), '[]')
                       && daterange(NEW.effective_from, COALESCE(NEW.effective_to, 'infinity'::date), '[]')
            ) THEN
                RAISE EXCEPTION 'standard_cost effective windows overlap (DM-COST-05)'
                    USING ERRCODE = 'unique_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_standard_cost_overlap BEFORE INSERT OR UPDATE ON {SCHEMA}."standard_cost" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_standard_cost_overlap()"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_cost_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'ACTIVE' AND OLD.status = 'SUPERSEDED' THEN
                RAISE EXCEPTION 'cost_version cannot move from SUPERSEDED to ACTIVE';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_cost_version_guard BEFORE UPDATE ON {SCHEMA}."cost_version" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_cost_version()"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_revaluation_posted() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'POSTED' AND NEW.status = 'POSTED' AND NEW.posted_transaction_id IS DISTINCT FROM OLD.posted_transaction_id THEN
                RAISE EXCEPTION 'posted cost_revaluation is immutable';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        f'CREATE TRIGGER trg_revaluation_posted_guard BEFORE UPDATE ON {SCHEMA}."cost_revaluation" '
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_revaluation_posted()"
    )


def _rls_and_grants() -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table in t.REVISION_0012_TABLES:
        name = table.name
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
    names = ", ".join(f'{SCHEMA}."{table.name}"' for table in t.REVISION_0012_TABLES)
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
    for table in t.REVISION_0012_TABLES:
        _create_table(table, deferred_fks)
    for fk_name, table_name, local_columns, ref_schema, ref_table, ref_columns in deferred_fks:
        op.execute(
            f'ALTER TABLE {SCHEMA}."{table_name}" ADD CONSTRAINT "{fk_name}" '
            f'FOREIGN KEY ({", ".join(local_columns)}) '
            f'REFERENCES {ref_schema}."{ref_table}" ({", ".join(ref_columns)})'
        )
    _triggers()
    _rls_and_grants()


def downgrade() -> None:
    for table in reversed(t.REVISION_0012_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
