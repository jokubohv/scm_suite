"""DDL helpers shared by the FS-00 migrations (0001–0003).

The migrations stay small and consistent by compiling the reviewed SQLAlchemy Core metadata from
:mod:`app.core.models.platform` into explicit PostgreSQL DDL (``CreateTable``/``CreateIndex``),
then applying triggers/RLS/partition/seeds as reviewed raw SQL. Kept dependency-free (SQLAlchemy
only) and safe for Alembic offline rendering (compiled strings, no live connection needed).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.models.platform import (
    ALL_TABLES,
    APPEND_ONLY_TABLES,
    PARTITIONED_TABLES,
)

SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"
AUTH_ROLE = "platform_auth"
FINANCE_ROLE = "finance_adapter"

_TABLE_BY_NAME = {table.name: table for table in ALL_TABLES}


def _dialect():  # pragma: no cover - trivial
    return postgresql.dialect()


def table_ddl(table: sa.Table) -> str:
    return str(CreateTable(table).compile(dialect=_dialect())).strip()


def index_ddls(table: sa.Table) -> list[str]:
    return [str(CreateIndex(index).compile(dialect=_dialect())).strip() for index in table.indexes]


def create_table(op_, table: sa.Table) -> None:
    """Create one non-partitioned table plus its indexes (partitioned parents use create_partitioned)."""
    op_.execute(table_ddl(table))
    for ddl in index_ddls(table):
        op_.execute(ddl)


def drop_table(op_, table: sa.Table) -> None:
    op_.execute(f'DROP TABLE IF EXISTS platform."{table.name}" CASCADE')


def create_tables(op_, tables) -> None:
    for table in tables:
        if table.name in PARTITIONED_TABLES:
            continue
        create_table(op_, table)


def drop_tables(op_, tables) -> None:
    for table in reversed(list(tables)):
        drop_table(op_, table)


def create_partitioned(op_, table: sa.Table, *, months_before: int = 1, months_after: int = 2) -> None:
    """Create a monthly RANGE-partitioned parent and its near-term partitions."""
    op_.execute(table_ddl(table))
    for ddl in index_ddls(table):
        op_.execute(ddl)
    ensure_month_partitions(op_, table.name, months_before=months_before, months_after=months_after)


def _month_start(anchor: date, offset: int) -> date:
    total = anchor.year * 12 + (anchor.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


def _next_month(month_start: date) -> date:
    return _month_start(month_start, 1)


def ensure_month_partitions(op_, table_name: str, *, months_before: int = 1, months_after: int = 2) -> list[str]:
    current = datetime.now(UTC).date().replace(day=1)
    created: list[str] = []
    for offset in range(-months_before, months_after + 1):
        start = _month_start(current, offset)
        end = _next_month(start)
        partition = f"{table_name}_{start.strftime('%Y%m')}"
        op_.execute(
            f'CREATE TABLE IF NOT EXISTS platform."{partition}" PARTITION OF platform."{table_name}" '
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
        created.append(partition)
    return created


def install_updated_at_trigger(op_, table_name: str) -> None:
    op_.execute(f'DROP TRIGGER IF EXISTS trg_{table_name}_touch ON platform."{table_name}"')
    op_.execute(
        f'CREATE TRIGGER trg_{table_name}_touch BEFORE UPDATE ON platform."{table_name}" '
        f"FOR EACH ROW EXECUTE FUNCTION platform.set_updated_at()"
    )


def install_append_only_trigger(op_, table_name: str) -> None:
    op_.execute(f'DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON platform."{table_name}"')
    op_.execute(
        f'CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON platform."{table_name}" '
        f"FOR EACH ROW EXECUTE FUNCTION platform.reject_mutation()"
    )


def create_trigger_functions(op_) -> None:
    op_.execute(
        """
        CREATE OR REPLACE FUNCTION platform.set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
        """
    )
    op_.execute(
        """
        CREATE OR REPLACE FUNCTION platform.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END $$;
        """
    )


def install_standard_triggers(op_, tables) -> None:
    for table in tables:
        if "updated_at" in table.c:
            install_updated_at_trigger(op_, table.name)
        if table.name in APPEND_ONLY_TABLES:
            install_append_only_trigger(op_, table.name)


def tenant_table_names(tables) -> list[str]:
    """Tenant-owned tables that require RLS (excludes global currency and user_identity)."""
    names: list[str] = []
    for table in tables:
        if "tenant_id" not in table.c:
            continue
        names.append(table.name)
    return names


def apply_tenant_rls(op_, table_names) -> None:
    setting = "current_setting('app.tenant_id', true)"
    for table_name in table_names:
        op_.execute(f'ALTER TABLE platform."{table_name}" ENABLE ROW LEVEL SECURITY')
        op_.execute(f'DROP POLICY IF EXISTS tenant_isolation ON platform."{table_name}"')
        op_.execute(
            f'CREATE POLICY tenant_isolation ON platform."{table_name}" '
            f"USING (tenant_id = {setting}::uuid) WITH CHECK (tenant_id = {setting}::uuid)"
        )
        op_.execute(f'DROP POLICY IF EXISTS system_tenant_read ON platform."{table_name}"')
        op_.execute(
            f'CREATE POLICY system_tenant_read ON platform."{table_name}" FOR SELECT '
            f"USING (tenant_id = '{SYSTEM_TENANT_ID}'::uuid)"
        )


def disable_tenant_rls(op_, table_names) -> None:
    for table_name in reversed(list(table_names)):
        op_.execute(f'DROP POLICY IF EXISTS tenant_isolation ON platform."{table_name}"')
        op_.execute(f'DROP POLICY IF EXISTS system_tenant_read ON platform."{table_name}"')
        op_.execute(f'ALTER TABLE platform."{table_name}" DISABLE ROW LEVEL SECURITY')


def grant_app_role_privileges(op_, table_names, *, append_only: tuple[str, ...] = APPEND_ONLY_TABLES) -> None:
    """Best-effort grants; environment bootstrap (S0-06) creates the DB roles.

    If the roles do not exist the DO block is a no-op, so migrations work on a plain dev DB.
    """
    select_tables = ", ".join(f'platform."{name}"' for name in table_names)
    append_tables = ", ".join(f'platform."{name}"' for name in append_only if name in table_names)
    op_.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA platform TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON {select_tables} TO {APP_ROLE}';
                IF '{append_tables}' <> '' THEN
                    EXECUTE 'REVOKE UPDATE ON {append_tables} FROM {APP_ROLE}';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{AUTH_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA platform TO {AUTH_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON platform.user_identity, platform.user_session, platform.tenant_invitation, platform.user_identity_link TO {AUTH_ROLE}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{FINANCE_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA platform TO {FINANCE_ROLE}';
                EXECUTE 'GRANT SELECT, INSERT ON platform.finance_journal_intent TO {FINANCE_ROLE}';
                EXECUTE 'GRANT UPDATE (status, acknowledged_at) ON platform.finance_journal_intent TO {FINANCE_ROLE}';
            END IF;
        END $$;
        """
    )


def create_legacy_app_user(op_) -> None:
    """0001 legacy app_user shape (pre-Δ-MT); 0002 performs the identity/membership expand-contract."""
    op_.create_table(
        "app_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("is_service_account", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name="fk_app_user_tenant"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_email"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_app_user_tenant_id_id"),
        schema="platform",
    )
