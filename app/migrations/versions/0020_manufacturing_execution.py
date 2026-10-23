"""0020_manufacturing_execution — FS-09 work orders/WIP/build/unbuild dispositions/outsourced/ECO/
scheduling/capacity + Δ-CFG compatibility engine + Δ-REUSE condition codes (27 tables).

Chain: ``0019_manufacturing_routing`` → ``0020_manufacturing_execution`` (FS-09 migrations stay 3;
Δ tables/columns live inside this existing revision — no renumbering, no new migration). The
``compat_configuration`` evidence table is append-only (UPDATE/DELETE rejected by trigger). Seeds:
Δ-REUSE condition codes per tenant (NEW/USED_A/USED_B/USED_C/REFURB/SCRAP).

Additive; downgrade drops only the tables created here.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.modules.manufacturing import tables as t

revision = "0020_manufacturing_execution"
down_revision = "0019_manufacturing_routing"
branch_labels = None
depends_on = None

SCHEMA = "manufacturing"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

CONDITION_SEEDS: tuple[tuple[str, str, bool, bool, bool, int], ...] = (
    ("NEW", "New", False, True, True, 10),
    ("USED_A", "Used - Grade A", True, False, False, 20),
    ("USED_B", "Used - Grade B", True, False, False, 30),
    ("USED_C", "Used - Grade C", True, False, False, 40),
    ("REFURB", "Refurbished", True, False, False, 50),
    ("SCRAP", "Scrap", True, False, False, 90),
)


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
    for name in t.APPEND_ONLY_TABLES:
        if any(table.name == name for table in tables):
            op.execute(
                f'CREATE TRIGGER trg_{name}_append_only BEFORE UPDATE OR DELETE ON {SCHEMA}."{name}" '
                f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_mutation()"
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
            f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)"
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


def _condition_seeds() -> None:
    rows = (
        op.get_bind()
        .execute(
            text("SELECT id::text FROM platform.tenant WHERE id <> :system ORDER BY created_at"),
            {"system": SYSTEM_TENANT_ID},
        )
        .all()
    )
    insert = text(
        """
        INSERT INTO manufacturing.condition_code
            (id, tenant_id, code, name, is_used, is_sellable_default, is_committable_default,
             sort_order, is_active, created_at, updated_at, version)
        VALUES (gen_random_uuid(), :tenant_id, :code, :name, :is_used, :sellable, :committable,
                :sort_order, true, now(), now(), 1)
        ON CONFLICT (tenant_id, code) DO NOTHING
        """
    )
    for row in rows:
        for code, name, is_used, sellable, committable, sort_order in CONDITION_SEEDS:
            op.get_bind().execute(
                insert,
                {
                    "tenant_id": str(row[0]),
                    "code": code,
                    "name": name,
                    "is_used": is_used,
                    "sellable": sellable,
                    "committable": committable,
                    "sort_order": sort_order,
                },
            )


def upgrade() -> None:
    deferred: list = []
    for table in t.EXECUTION_TABLES:
        _create_table(table, deferred)
    _add_fks(deferred)
    _triggers(t.EXECUTION_TABLES)
    _rls_and_grants(t.EXECUTION_TABLES)
    _condition_seeds()


def downgrade() -> None:
    for table in reversed(t.EXECUTION_TABLES):
        op.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table.name}" CASCADE')
