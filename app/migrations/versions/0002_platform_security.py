"""0002_platform_security — identity/membership (Δ-MT), enterprise SSO (Δ-SSO), RBAC guards,
audit trail, finance journal intents, RLS bootstrap and grants (FS-00, DATA-MODEL §21.1 M00).

Expand/contract inside this revision: legacy ``app_user`` rows are backfilled into
``user_identity`` + ACTIVE memberships before ``identity_id`` becomes NOT NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.models import platform as models
from app.core.models.schema_ddl import (
    apply_tenant_rls,
    create_partitioned,
    create_table,
    create_tables,
    disable_tenant_rls,
    drop_table,
    grant_app_role_privileges,
    install_append_only_trigger,
    install_standard_triggers,
    tenant_table_names,
)

revision = "0002_platform_security"
down_revision = "0001_platform_core"
branch_labels = None
depends_on = None

SECURITY_TABLES = models.REVISION_0002_TABLES
NON_IDENTITY_TABLES = ("user_identity",)
RLS_TABLES = tenant_table_names(models.REVISION_0001_TABLES + SECURITY_TABLES)


def upgrade() -> None:
    # 1. Global auth identity (Δ-MT, auth-only exception: no tenant_id / no RLS policy).
    create_table(op, models.user_identity)
    # 2. Expand legacy app_user -> tenant membership.
    op.add_column("app_user", sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=True), schema="platform")
    op.add_column("app_user", sa.Column("membership_status", sa.Text(), nullable=True), schema="platform")
    op.add_column("app_user", sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True), schema="platform")
    op.add_column("app_user", sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True), schema="platform")
    op.add_column("app_user", sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0")), schema="platform")
    op.execute(
        """
        INSERT INTO platform.user_identity (id, email, display_name, status, created_at, updated_at, version)
        SELECT gen_random_uuid(), lower(au.email), max(au.display_name), 'ACTIVE', now(), now(), 1
          FROM platform.app_user au
         WHERE au.email IS NOT NULL AND length(trim(au.email)) > 0
         GROUP BY lower(au.email)
        ON CONFLICT (email) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE platform.app_user au
           SET identity_id = ui.id,
               membership_status = CASE WHEN au.status = 'ACTIVE' THEN 'ACTIVE' ELSE 'SUSPENDED' END,
               joined_at = au.created_at,
               token_version = 0
          FROM platform.user_identity ui
         WHERE lower(au.email) = ui.email AND au.identity_id IS NULL
        """
    )
    op.execute("UPDATE platform.app_user SET identity_id = gen_random_uuid() WHERE identity_id IS NULL")
    op.alter_column("app_user", "identity_id", nullable=False, schema="platform")
    op.alter_column("app_user", "membership_status", nullable=False, server_default=sa.text("'INVITED'"), schema="platform")
    # DM-PLAT-12/13: membership/status/token-version integrity (model metadata declares these; the
    # expand-contract revision must add them to the legacy table too).
    op.create_check_constraint("app_user_status", "app_user", "status IN ('ACTIVE','INACTIVE')", schema="platform")
    op.create_check_constraint("app_user_membership_status", "app_user", "membership_status IN ('INVITED','ACTIVE','SUSPENDED')", schema="platform")
    op.create_check_constraint("app_user_token_version", "app_user", "token_version >= 0", schema="platform")
    op.create_foreign_key("fk_app_user_identity", "app_user", "user_identity", ["identity_id"], ["id"], source_schema="platform", referent_schema="platform")
    op.drop_constraint("uq_app_user_email", "app_user", type_="unique", schema="platform")
    op.create_unique_constraint("uq_app_user_identity", "app_user", ["tenant_id", "identity_id"], schema="platform")
    op.create_index("ix_app_user_membership_status", "app_user", ["tenant_id", "membership_status"], schema="platform")
    op.create_foreign_key("fk_app_user_invited_by", "app_user", "app_user", ["tenant_id", "invited_by"], ["tenant_id", "id"], source_schema="platform", referent_schema="platform")
    # 3. Invitations/sessions + Δ-SSO federation tables.
    create_tables(op, tuple(table for table in SECURITY_TABLES if table.name not in {"user_identity", "audit_event", "finance_journal_intent"}))
    # DM-PLAT-14/Δ-MT: at most one PENDING invitation per tenant+email.
    op.create_index(
        "uq_tenant_invitation_pending_email",
        "tenant_invitation",
        ["tenant_id", "email"],
        unique=True,
        schema="platform",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    # 4. Audit trail (append-only, monthly partitions) + finance journal intents (append-only).
    create_partitioned(op, models.audit_event)
    create_table(op, models.finance_journal_intent)
    install_append_only_trigger(op, "audit_event")
    install_append_only_trigger(op, "finance_journal_intent")
    install_standard_triggers(op, models.REVISION_0001_TABLES + tuple(t for t in SECURITY_TABLES if t.name in {"tenant_invitation", "user_session", "tenant_sso_connection", "user_identity_link", "sso_group_role_map", "finance_journal_intent"}))
    # 5. Defence in depth: tenant RLS on every tenant-owned platform table + SYSTEM_TENANT read policy.
    apply_tenant_rls(op, RLS_TABLES)
    grant_app_role_privileges(op, RLS_TABLES)


def downgrade() -> None:
    disable_tenant_rls(op, RLS_TABLES)
    op.execute('DROP TABLE IF EXISTS platform.audit_event CASCADE')
    drop_table(op, models.finance_journal_intent)
    op.drop_index("uq_tenant_invitation_pending_email", table_name="tenant_invitation", schema="platform")
    for check in ("app_user_token_version", "app_user_membership_status", "app_user_status"):
        op.drop_constraint(check, "app_user", type_="check", schema="platform")
    op.drop_constraint("fk_app_user_invited_by", "app_user", type_="foreignkey", schema="platform")
    op.drop_index("ix_app_user_membership_status", table_name="app_user", schema="platform")
    op.drop_constraint("uq_app_user_identity", "app_user", type_="unique", schema="platform")
    op.create_unique_constraint("uq_app_user_email", "app_user", ["tenant_id", "email"], schema="platform")
    op.drop_constraint("fk_app_user_identity", "app_user", type_="foreignkey", schema="platform")
    for column in ("token_version", "invited_by", "joined_at", "membership_status", "identity_id"):
        op.drop_column("app_user", column, schema="platform")
    for table in reversed([t for t in SECURITY_TABLES if t.name not in {"user_identity", "audit_event", "finance_journal_intent"}]):
        drop_table(op, table)
    drop_table(op, models.user_identity)
