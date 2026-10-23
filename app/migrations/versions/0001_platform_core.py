"""0001_platform_core — platform schema bootstrap (FS-00, DATA-MODEL §21.1 M00).

Creates the ``platform`` schema, the 21 core tables (tenant lifecycle/config/reference, RBAC base,
approvals, custom fields, attachments, document links) and the pre-Δ-MT legacy ``app_user`` shape.
Identity/membership (Δ-MT), SSO (Δ-SSO), audit/partitions/finance intents, RLS and grants follow in
``0002_platform_security``; outbox/jobs in ``0003_platform_events``.

Revision ID: 0001_platform_core
Revises: None
"""

from __future__ import annotations

from alembic import op

from app.core.models import platform as models
from app.core.models.schema_ddl import (
    SYSTEM_TENANT_ID,
    create_legacy_app_user,
    create_tables,
    create_trigger_functions,
    drop_tables,
    install_standard_triggers,
)

revision = "0001_platform_core"
down_revision = None
branch_labels = None
depends_on = None

CORE_TABLES = tuple(table for table in models.REVISION_0001_TABLES if table.name != "app_user")

CURRENCIES: tuple[tuple[str, int, int], ...] = (
    ("USD", 840, 2), ("EUR", 978, 2), ("GBP", 826, 2), ("CAD", 124, 2), ("AUD", 36, 2),
    ("JPY", 392, 0), ("CNY", 156, 2), ("MXN", 484, 2), ("INR", 356, 2), ("SGD", 702, 2),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    create_trigger_functions(op)
    create_tables(op, CORE_TABLES)
    create_legacy_app_user(op)
    op.execute('CREATE INDEX IF NOT EXISTS ix_app_user_status ON platform.app_user (tenant_id, status)')
    install_standard_triggers(op, CORE_TABLES)
    # SYSTEM_TENANT reserved row (never customer-facing) + ISO-4217 reference rows (idempotent).
    op.execute(
        f"""
        INSERT INTO platform.tenant
            (id, tenant_id, code, name, base_currency_code, timezone, status,
             negative_inventory_policy, rounding_mode, retention_ledger_years, settings,
             created_at, updated_at, version)
        VALUES
            ('{SYSTEM_TENANT_ID}', '{SYSTEM_TENANT_ID}', 'SYSTEM', 'System', 'USD', 'UTC', 'ACTIVE',
             'BLOCK', 'PER_MOVEMENT', 7, '{{}}'::jsonb, now(), now(), 1)
        ON CONFLICT (code) DO NOTHING
        """
    )
    for code, numeric_code, minor_units in CURRENCIES:
        op.execute(
            f"""
            INSERT INTO platform.currency (code, tenant_id, numeric_code, minor_units, is_active, created_at, updated_at, version)
            VALUES ('{code}', NULL, {numeric_code}, {minor_units}, true, now(), now(), 1)
            ON CONFLICT (code) DO NOTHING
            """
        )


def downgrade() -> None:
    drop_tables(op, tuple(reversed(models.REVISION_0001_TABLES)))
    op.execute("DROP SCHEMA IF EXISTS platform CASCADE")
