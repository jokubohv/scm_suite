"""SQLAlchemy 2.0 Core metadata for the 34 `platform` tables (FS-00).

Authority: `design/DATA-MODEL.md` §3 + §21.1 M00 row, `feature-sets/FS-00.md` §4.1–4.3 and the
Δ-MT/Δ-SSO/Δ-RBAC addenda. The Alembic revisions (0001–0003) own the explicit DDL; this metadata is
used for autogenerate checks and by FS-00 services/tests. No cross-module ORM imports.

Standard columns follow DATA-MODEL §1.2: UUIDv7 `id` (application-generated), `tenant_id`, audit
timestamps/actors, optimistic `version`, and `unique (tenant_id, id)` enabling composite tenant FKs.
Global exceptions: `currency` (code PK, system rows tenant_id NULL) and `user_identity` (no tenant_id,
auth-only).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase

from app.migrations.base import NAMING_CONVENTION

metadata = MetaData(schema="platform", naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


UUID_T = UUID(as_uuid=True)
MONEY = Numeric(38, 12)
QTY = Numeric(38, 12)
STAMP = DateTime(timezone=True)


def _audit(columns: list) -> None:
    columns.extend([
        Column("created_at", STAMP, nullable=False, server_default=text("now()")),
        Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
        Column("created_by", UUID_T, nullable=True),
        Column("updated_by", UUID_T, nullable=True),
        Column("version", Integer, nullable=False, server_default=text("1")),
    ])


def mk(name: str, columns: list, *, tenant: bool = True, timestamps: bool = True,
       tenant_fk: bool = True, id_pk: bool = True,
       extra_constraints: list | None = None) -> Table:
    cols: list = [Column("id", UUID_T, primary_key=True, nullable=False)] if id_pk else []
    if tenant:
        cols.append(Column("tenant_id", UUID_T, nullable=False))
    cols.extend(columns)
    if timestamps:
        _audit(cols)
    constraints: list = list(extra_constraints or [])
    if tenant:
        if id_pk:
            constraints.append(UniqueConstraint("tenant_id", "id", name=f"uq_{name}_tenant_id_id"))
        if tenant_fk:
            constraints.append(ForeignKeyConstraint(["tenant_id"], ["platform.tenant.id"], name=f"fk_{name}_tenant"))
    return Table(name, metadata, *cols, *constraints)

# --------------------------------------------------------------------------- 0001: platform core
currency = Table(
    "currency", metadata,
    Column("code", Text, primary_key=True, nullable=False),
    Column("tenant_id", UUID_T, nullable=True),
    Column("numeric_code", Integer, nullable=True),
    Column("minor_units", Integer, nullable=False, server_default=text("2")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    CheckConstraint("length(code) = 3", name="currency_code_len"),
)

tenant = mk("tenant", [
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("base_currency_code", Text, nullable=True),
    Column("timezone", Text, nullable=False, server_default=text("'UTC'")),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("negative_inventory_policy", Text, nullable=False, server_default=text("'BLOCK'")),
    Column("rounding_mode", Text, nullable=False, server_default=text("'PER_MOVEMENT'")),
    Column("retention_ledger_years", Integer, nullable=False, server_default=text("7")),
    Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
], tenant_fk=False, extra_constraints=[
    UniqueConstraint("code", name="uq_tenant_code"),
    CheckConstraint("status IN ('ACTIVE','SUSPENDED','OFFBOARDING')", name="tenant_status"),
])
tenant_settings = mk("tenant_setting", [
    Column("namespace", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("value", JSONB, nullable=False, server_default=text("'null'::jsonb")),
    Column("value_type", Text, nullable=False, server_default=text("'JSON'")),
    Column("is_default", Boolean, nullable=False, server_default=text("false")),
], extra_constraints=[
    UniqueConstraint("tenant_id", "namespace", "key", name="uq_tenant_setting_scope"),
    CheckConstraint("namespace IN ('inventory','manufacturing','wms','replenishment','costing','pricing','platform','accounting','rbac','auth','counts','traceability','procurement','quality','consigned','extensions','reporting')", name="tenant_setting_namespace"),
])
exchange_rate = mk("exchange_rate", [
    Column("base_currency", Text, nullable=False),
    Column("quote_currency", Text, nullable=False),
    Column("rate_date", Date, nullable=False),
    Column("rate", MONEY, nullable=False),
    Column("source", Text, nullable=False, server_default=text("'finance'")),
], extra_constraints=[
    UniqueConstraint("tenant_id", "base_currency", "quote_currency", "rate_date", "source", name="uq_exchange_rate_key"),
    CheckConstraint("rate > 0", name="exchange_rate_positive"),
])
fiscal_period = mk("fiscal_period", [
    Column("period_key", Text, nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'OPEN'")),
    Column("closed_at", STAMP, nullable=True),
    Column("source_version", Integer, nullable=False, server_default=text("1")),
], extra_constraints=[
    UniqueConstraint("tenant_id", "period_key", name="uq_fiscal_period_key"),
    CheckConstraint("start_date <= end_date", name="fiscal_period_dates"),
    CheckConstraint("status IN ('OPEN','CLOSED','LOCKED')", name="fiscal_period_status"),
])
number_sequence = mk("number_sequence", [
    Column("doc_type", Text, nullable=False),
    Column("scope_key", Text, nullable=False, server_default=text("''")),
    Column("prefix", Text, nullable=False, server_default=text("''")),
    Column("padding", Integer, nullable=False, server_default=text("6")),
    Column("next_value", BigInteger, nullable=False, server_default=text("1")),
    Column("reset_policy", Text, nullable=False, server_default=text("'NEVER'")),
    Column("gap_policy", Text, nullable=False, server_default=text("'NON_GAPLESS'")),
    Column("period_key", Text, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "doc_type", "scope_key", "period_key", name="uq_number_sequence_scope"),
    CheckConstraint("next_value > 0", name="number_sequence_next_positive"),
])
feature_flag = mk("feature_flag", [
    Column("flag_key", Text, nullable=False),
    Column("is_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("source", Text, nullable=False, server_default=text("'DEFAULT'")),
    Column("changed_by", UUID_T, nullable=True),
    Column("changed_at", STAMP, nullable=True),
    Column("notes", Text, nullable=True),
], extra_constraints=[UniqueConstraint("tenant_id", "flag_key", name="uq_feature_flag_key")])
custom_field_def = mk("custom_field_def", [
    Column("entity_type", Text, nullable=False),
    Column("field_code", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("data_type", Text, nullable=False),
    Column("options", JSONB, nullable=True),
    Column("is_required", Boolean, nullable=False, server_default=text("false")),
    Column("is_searchable", Boolean, nullable=False, server_default=text("false")),
    Column("validation", JSONB, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "entity_type", "field_code", name="uq_custom_field_def_scope"),
    CheckConstraint("data_type IN ('TEXT','NUMBER','DATE','BOOL','SELECT')", name="custom_field_def_type"),
])
custom_field_value = mk("custom_field_value", [
    Column("field_def_id", UUID_T, nullable=False),
    Column("entity_id", UUID_T, nullable=False),
    Column("value_text", Text, nullable=True),
    Column("value_number", MONEY, nullable=True),
    Column("value_date", Date, nullable=True),
    Column("value_bool", Boolean, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "field_def_id", "entity_id", name="uq_custom_field_value_scope"),
    CheckConstraint("(value_text IS NOT NULL)::int + (value_number IS NOT NULL)::int + (value_date IS NOT NULL)::int + (value_bool IS NOT NULL)::int = 1", name="custom_field_value_one"),
    ForeignKeyConstraint(["field_def_id"], ["platform.custom_field_def.id"], name="fk_custom_field_value_def"),
])
role = mk("role", [
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("is_system", Boolean, nullable=False, server_default=text("false")),
    Column("is_template", Boolean, nullable=False, server_default=text("false")),
    Column("is_editable", Boolean, nullable=False, server_default=text("true")),
    Column("inactive_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "code", name="uq_role_code"),
    CheckConstraint("NOT is_template OR (is_system AND NOT is_editable)", name="role_template_guard"),
])
permission_scope = mk("permission_scope", [
    Column("scope", Text, nullable=False),
    Column("domain", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("requires_approval", Boolean, nullable=False, server_default=text("false")),
    Column("is_platform_only", Boolean, nullable=False, server_default=text("false")),
    Column("is_assignable", Boolean, nullable=False, server_default=text("true")),
    Column("owner_feature_set", Text, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "scope", name="uq_permission_scope_scope"),
    CheckConstraint("NOT (is_platform_only AND is_assignable)", name="permission_scope_platform"),
])
role_permission = mk("role_permission", [
    Column("role_id", UUID_T, nullable=False),
    Column("scope_id", UUID_T, nullable=False),
], extra_constraints=[
    PrimaryKeyConstraint("tenant_id", "role_id", "scope_id", name="pk_role_permission"),
    ForeignKeyConstraint(["tenant_id", "role_id"], ["platform.role.tenant_id", "platform.role.id"], name="fk_role_permission_role"),
    ForeignKeyConstraint(["tenant_id", "scope_id"], ["platform.permission_scope.tenant_id", "platform.permission_scope.id"], name="fk_role_permission_scope"),
], id_pk=False)
user_role = mk("user_role", [
    Column("user_id", UUID_T, nullable=False),
    Column("role_id", UUID_T, nullable=False),
    Column("effective_from", STAMP, nullable=False, server_default=text("now()")),
    Column("effective_to", STAMP, nullable=True),
    Column("assigned_by", UUID_T, nullable=True),
], extra_constraints=[
    PrimaryKeyConstraint("tenant_id", "user_id", "role_id", "effective_from", name="pk_user_role"),
    CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="user_role_range"),
    ForeignKeyConstraint(["tenant_id", "role_id"], ["platform.role.tenant_id", "platform.role.id"], name="fk_user_role_role"),
], id_pk=False)
user_data_scope = mk("user_data_scope", [
    Column("user_id", UUID_T, nullable=True),
    Column("role_id", UUID_T, nullable=True),
    Column("scope_type", Text, nullable=False),
    Column("scope_value", Text, nullable=False),
    Column("include_descendants", Boolean, nullable=False, server_default=text("false")),
], extra_constraints=[
    CheckConstraint("user_id IS NOT NULL OR role_id IS NOT NULL", name="user_data_scope_subject"),
    UniqueConstraint("tenant_id", "user_id", "role_id", "scope_type", "scope_value", name="uq_user_data_scope_key"),
    CheckConstraint("scope_type IN ('LOCATION','CLASS','DEPARTMENT','SUBSIDIARY')", name="user_data_scope_type"),
    ForeignKeyConstraint(["tenant_id", "role_id"], ["platform.role.tenant_id", "platform.role.id"], name="fk_user_data_scope_role"),
])
api_key = mk("api_key", [
    Column("name", Text, nullable=False),
    Column("key_prefix", Text, nullable=False),
    Column("secret_hash", Text, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("expires_at", STAMP, nullable=True),
    Column("last_used_at", STAMP, nullable=True),
    Column("revoked_at", STAMP, nullable=True),
], extra_constraints=[UniqueConstraint("tenant_id", "key_prefix", name="uq_api_key_prefix")])
approval_definition = mk("approval_definition", [
    Column("document_type", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("condition", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("steps", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("is_enabled", Boolean, nullable=False, server_default=text("true")),
    Column("priority", Integer, nullable=False, server_default=text("100")),
], extra_constraints=[UniqueConstraint("tenant_id", "document_type", "name", name="uq_approval_definition_name")])
approval_request = mk("approval_request", [
    Column("document_type", Text, nullable=False),
    Column("document_id", UUID_T, nullable=False),
    Column("definition_id", UUID_T, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'PENDING'")),
    Column("current_step", Integer, nullable=False, server_default=text("0")),
    Column("amount_base", MONEY, nullable=True),
    Column("requested_by", UUID_T, nullable=True),
    Column("requested_at", STAMP, nullable=False, server_default=text("now()")),
    Column("decided_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "document_type", "document_id", name="uq_approval_request_document"),
    CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED')", name="approval_request_status"),
])
approval_action = mk("approval_action", [
    Column("approval_request_id", UUID_T, nullable=False),
    Column("step", Integer, nullable=False),
    Column("actor_id", UUID_T, nullable=True),
    Column("decision", Text, nullable=False),
    Column("comment", Text, nullable=True),
    Column("decided_at", STAMP, nullable=False, server_default=text("now()")),
    Column("delegated_to", UUID_T, nullable=True),
], extra_constraints=[
    CheckConstraint("decision IN ('APPROVE','REJECT','DELEGATE','RECALL')", name="approval_action_decision"),
    ForeignKeyConstraint(["approval_request_id"], ["platform.approval_request.id"], name="fk_approval_action_request"),
])
document_link = mk("document_link", [
    Column("link_type", Text, nullable=False),
    Column("from_type", Text, nullable=False),
    Column("from_id", UUID_T, nullable=False),
    Column("to_type", Text, nullable=False),
    Column("to_id", UUID_T, nullable=False),
    Column("qty", QTY, nullable=True),
    Column("amount_base", MONEY, nullable=True),
], extra_constraints=[UniqueConstraint("tenant_id", "link_type", "from_type", "from_id", "to_type", "to_id", name="uq_document_link_key")])
attachment_metadata = mk("attachment_metadata", [
    Column("entity_type", Text, nullable=False),
    Column("entity_id", UUID_T, nullable=False),
    Column("filename", Text, nullable=False),
    Column("content_type", Text, nullable=True),
    Column("size_bytes", BigInteger, nullable=False, server_default=text("0")),
    Column("storage_key", Text, nullable=False),
    Column("checksum", Text, nullable=True),
    Column("uploaded_by", UUID_T, nullable=True),
    Column("is_quality_image", Boolean, nullable=False, server_default=text("false")),
], extra_constraints=[UniqueConstraint("tenant_id", "storage_key", name="uq_attachment_metadata_key")])
app_user = mk("app_user", [
    Column("identity_id", UUID_T, nullable=False),
    Column("email", CITEXT, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("membership_status", Text, nullable=False, server_default=text("'INVITED'")),
    Column("joined_at", STAMP, nullable=True),
    Column("invited_by", UUID_T, nullable=True),
    Column("token_version", Integer, nullable=False, server_default=text("0")),
    Column("is_service_account", Boolean, nullable=False, server_default=text("false")),
    Column("last_login_at", STAMP, nullable=True),
    Column("locale", Text, nullable=True),
    Column("timezone", Text, nullable=True),
    Column("password_hash", Text, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "identity_id", name="uq_app_user_identity"),
    CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="app_user_status"),
    CheckConstraint("membership_status IN ('INVITED','ACTIVE','SUSPENDED')", name="app_user_membership_status"),
    CheckConstraint("token_version >= 0", name="app_user_token_version"),
    ForeignKeyConstraint(["identity_id"], ["platform.user_identity.id"], name="fk_app_user_identity"),
    ForeignKeyConstraint(["tenant_id", "invited_by"], ["platform.app_user.tenant_id", "platform.app_user.id"], name="fk_app_user_invited_by"),
])

# ------------------------------------------------------------------- 0002: security / identity / SSO
user_identity = Table(
    "user_identity", metadata,
    Column("id", UUID_T, primary_key=True, nullable=False),
    Column("email", CITEXT, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("password_hash", Text, nullable=True),
    Column("sso_subject", Text, nullable=True),
    Column("mfa_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("is_platform_admin", Boolean, nullable=False, server_default=text("false")),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("last_login_at", STAMP, nullable=True),
    Column("created_at", STAMP, nullable=False, server_default=text("now()")),
    Column("updated_at", STAMP, nullable=False, server_default=text("now()")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    UniqueConstraint("email", name="uq_user_identity_email"),
    CheckConstraint("status IN ('ACTIVE','DISABLED','LOCKED')", name="user_identity_status"),
)
tenant_invitation = mk("tenant_invitation", [
    Column("email", CITEXT, nullable=False),
    Column("intended_role_ids", ARRAY(UUID_T), nullable=False, server_default=text("'{}'::uuid[]")),
    Column("token_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'PENDING'")),
    Column("invited_by", UUID_T, nullable=False),
    Column("expires_at", STAMP, nullable=False),
    Column("accepted_at", STAMP, nullable=True),
    Column("accepted_app_user_id", UUID_T, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "token_hash", name="uq_tenant_invitation_token"),
    CheckConstraint("status IN ('PENDING','ACCEPTED','REVOKED','EXPIRED')", name="tenant_invitation_status"),
    CheckConstraint("expires_at > created_at", name="tenant_invitation_expiry"),
    ForeignKeyConstraint(["tenant_id", "invited_by"], ["platform.app_user.tenant_id", "platform.app_user.id"], name="fk_tenant_invitation_inviter"),
])
user_session = mk("user_session", [
    Column("app_user_id", UUID_T, nullable=False),
    Column("identity_id", UUID_T, nullable=False),
    Column("refresh_token_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("issued_at", STAMP, nullable=False, server_default=text("now()")),
    Column("expires_at", STAMP, nullable=False),
    Column("revoked_at", STAMP, nullable=True),
    Column("revoked_reason", Text, nullable=True),
    Column("ip", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("last_seen_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("refresh_token_hash", name="uq_user_session_refresh_hash"),
    CheckConstraint("status IN ('ACTIVE','REVOKED','EXPIRED')", name="user_session_status"),
    CheckConstraint("expires_at > issued_at", name="user_session_expiry"),
    ForeignKeyConstraint(["tenant_id", "app_user_id"], ["platform.app_user.tenant_id", "platform.app_user.id"], name="fk_user_session_app_user"),
])
tenant_sso_connection = mk("tenant_sso_connection", [
    Column("name", Text, nullable=False),
    Column("protocol", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
    Column("dex_issuer_url", Text, nullable=True),
    Column("dex_instance_ref", Text, nullable=True),
    Column("client_id", Text, nullable=True),
    Column("client_secret_ref", Text, nullable=True),
    Column("connector_config", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("email_domains", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("is_enforced", Boolean, nullable=False, server_default=text("false")),
    Column("jit_provisioning", Boolean, nullable=False, server_default=text("false")),
    Column("default_role_ids", ARRAY(UUID_T), nullable=False, server_default=text("'{}'::uuid[]")),
    Column("group_claim_name", Text, nullable=False, server_default=text("'groups'")),
    Column("last_tested_at", STAMP, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("inactive_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "name", name="uq_tenant_sso_connection_name"),
    UniqueConstraint("tenant_id", "dex_issuer_url", name="uq_tenant_sso_connection_issuer"),
    CheckConstraint("protocol IN ('SAML','OIDC','LDAP_AD')", name="tenant_sso_connection_protocol"),
    CheckConstraint("status IN ('DRAFT','TESTING','ACTIVE','DISABLED','ERROR')", name="tenant_sso_connection_status"),
])
user_identity_link = mk("user_identity_link", [
    Column("identity_id", UUID_T, nullable=False),
    Column("connection_id", UUID_T, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("email_at_idp", CITEXT, nullable=True),
    Column("email_verified", Boolean, nullable=False, server_default=text("false")),
    Column("linked_at", STAMP, nullable=False, server_default=text("now()")),
    Column("last_login_at", STAMP, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("revoked_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "connection_id", "issuer", "subject", name="uq_user_identity_link_subject"),
    UniqueConstraint("tenant_id", "connection_id", "identity_id", name="uq_user_identity_link_identity"),
    CheckConstraint("status IN ('ACTIVE','REVOKED')", name="user_identity_link_status"),
    ForeignKeyConstraint(["identity_id"], ["platform.user_identity.id"], name="fk_user_identity_link_identity"),
    ForeignKeyConstraint(["tenant_id", "connection_id"], ["platform.tenant_sso_connection.tenant_id", "platform.tenant_sso_connection.id"], name="fk_user_identity_link_connection"),
])
sso_group_role_map = mk("sso_group_role_map", [
    Column("connection_id", UUID_T, nullable=False),
    Column("match_type", Text, nullable=False),
    Column("group_value", Text, nullable=False),
    Column("role_id", UUID_T, nullable=False),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
], extra_constraints=[
    UniqueConstraint("tenant_id", "connection_id", "group_value", "role_id", name="uq_sso_group_role_map_key"),
    CheckConstraint("match_type IN ('EXACT','REGEX')", name="sso_group_role_map_match_type"),
    CheckConstraint("priority >= 0", name="sso_group_role_map_priority"),
    ForeignKeyConstraint(["tenant_id", "connection_id"], ["platform.tenant_sso_connection.tenant_id", "platform.tenant_sso_connection.id"], name="fk_sso_group_role_map_connection"),
    ForeignKeyConstraint(["tenant_id", "role_id"], ["platform.role.tenant_id", "platform.role.id"], name="fk_sso_group_role_map_role"),
])
# audit_event: append-only, monthly partitions (created as partitioned in migration 0002).
audit_event = Table(
    "audit_event", metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("event_id", UUID_T, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", UUID_T, nullable=True),
    Column("action", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=True),
    Column("diff", JSONB, nullable=True),
    Column("request_id", Text, nullable=True),
    Column("trace_id", Text, nullable=True),
    Column("ip", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("source", Text, nullable=False, server_default=text("'API'")),
    Column("occurred_at", STAMP, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("id", "occurred_at", name="pk_audit_event"),
    CheckConstraint("actor_type IN ('USER','SERVICE','SYSTEM')", name="audit_event_actor_type"),
    CheckConstraint("source IN ('API','JOB','IMPORT','INTEGRATION','SYSTEM')", name="audit_event_source"),
    Index("ix_audit_event_resource", "tenant_id", "resource_type", "resource_id", "occurred_at"),
    Index("ix_audit_event_actor", "tenant_id", "actor_id", "occurred_at"),
    postgresql_partition_by="RANGE (occurred_at)",
)
finance_journal_intent = mk("finance_journal_intent", [
    Column("intent_line_no", Integer, nullable=False),
    Column("inventory_transaction_id", UUID_T, nullable=True),
    Column("ledger_sequence", BigInteger, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Column("source_doc_type", Text, nullable=False),
    Column("source_doc_id", UUID_T, nullable=True),
    Column("posting_rule_id", Text, nullable=False),
    Column("account_ref", Text, nullable=False),
    Column("debit_amount_base", MONEY, nullable=False, server_default=text("0")),
    Column("credit_amount_base", MONEY, nullable=False, server_default=text("0")),
    Column("amount_txn", MONEY, nullable=True),
    Column("currency_code", Text, nullable=False),
    Column("exchange_rate", MONEY, nullable=False, server_default=text("1")),
    Column("qty", QTY, nullable=True),
    Column("uom_id", UUID_T, nullable=True),
    Column("segments", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("intent_payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'RECORDED'")),
    Column("recorded_at", STAMP, nullable=False, server_default=text("now()")),
    Column("acknowledged_at", STAMP, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "inventory_transaction_id", "posting_rule_id", "intent_line_no", name="uq_finance_journal_intent_line"),
    CheckConstraint("((debit_amount_base > 0)::int + (credit_amount_base > 0)::int) = 1", name="finance_journal_intent_side"),
    CheckConstraint("account_ref <> ''", name="finance_journal_intent_account"),
    CheckConstraint("status IN ('RECORDED','ACKNOWLEDGED','REJECTED','SUPERSEDED')", name="finance_journal_intent_status"),
])

# ------------------------------------------------------------------------- 0003: events / jobs
# outbox_event: append-only, monthly partitions (created as partitioned in migration 0003).
outbox_event = Table(
    "outbox_event", metadata,
    Column("id", UUID_T, nullable=False),
    Column("tenant_id", UUID_T, nullable=False),
    Column("event_id", UUID_T, nullable=False),
    Column("aggregate_type", Text, nullable=False),
    Column("aggregate_id", UUID_T, nullable=False),
    Column("aggregate_version", BigInteger, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("event_version", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("occurred_at", STAMP, nullable=False, server_default=text("now()")),
    Column("correlation_id", UUID_T, nullable=True),
    Column("causation_id", UUID_T, nullable=True),
    Column("published_at", STAMP, nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text, nullable=True),
    PrimaryKeyConstraint("id", "occurred_at", name="pk_outbox_event"),
    UniqueConstraint("tenant_id", "event_id", "occurred_at", name="uq_outbox_event_event"),
    UniqueConstraint("tenant_id", "aggregate_type", "aggregate_id", "aggregate_version", "occurred_at", name="uq_outbox_event_aggregate"),
    CheckConstraint("attempts >= 0", name="outbox_event_attempts"),
    Index("ix_outbox_event_unpublished", "occurred_at", postgresql_where=text("published_at IS NULL")),
    postgresql_partition_by="RANGE (occurred_at)",
)
event_consumer_checkpoint = mk("event_consumer_checkpoint", [
    Column("consumer_id", Text, nullable=False),
    Column("stream", Text, nullable=False),
    Column("aggregate_id", UUID_T, nullable=True),
    Column("last_event_id", UUID_T, nullable=True),
    Column("last_seq", BigInteger, nullable=False, server_default=text("0")),
], extra_constraints=[UniqueConstraint("tenant_id", "consumer_id", "stream", "aggregate_id", name="uq_event_consumer_checkpoint_key")])
job_definition = mk("job_definition", [
    Column("name", Text, nullable=False),
    Column("queue", Text, nullable=False, server_default=text("'default'")),
    Column("handler", Text, nullable=False),
    Column("cron_expr", Text, nullable=True),
    Column("timeout_seconds", Integer, nullable=False, server_default=text("300")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("singleton", Boolean, nullable=False, server_default=text("true")),
    Column("payload_schema", JSONB, nullable=True),
    Column("is_enabled", Boolean, nullable=False, server_default=text("true")),
], extra_constraints=[
    UniqueConstraint("tenant_id", "name", name="uq_job_definition_name"),
    CheckConstraint("queue IN ('default','costing','planning','heavy','outbox')", name="job_definition_queue"),
])
job_run = mk("job_run", [
    Column("job_definition_id", UUID_T, nullable=False),
    Column("run_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'QUEUED'")),
    Column("attempt", Integer, nullable=False, server_default=text("1")),
    Column("started_at", STAMP, nullable=True),
    Column("finished_at", STAMP, nullable=True),
    Column("cursor", JSONB, nullable=True),
    Column("result_summary", JSONB, nullable=True),
    Column("error", Text, nullable=True),
    Column("correlation_id", UUID_T, nullable=True),
], extra_constraints=[
    UniqueConstraint("tenant_id", "run_key", name="uq_job_run_run_key"),
    CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="job_run_status"),
    ForeignKeyConstraint(["tenant_id", "job_definition_id"], ["platform.job_definition.tenant_id", "platform.job_definition.id"], name="fk_job_run_definition"),
])
idempotency_key = mk("idempotency_key", [
    Column("endpoint_id", Text, nullable=False),
    Column("key_value", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'IN_PROGRESS'")),
    Column("response_status", Integer, nullable=True),
    Column("response_body", JSONB, nullable=True),
    Column("resource_id", UUID_T, nullable=True),
    Column("expires_at", STAMP, nullable=False),
], extra_constraints=[
    UniqueConstraint("tenant_id", "endpoint_id", "key_value", name="uq_idempotency_key_key"),
    CheckConstraint("status IN ('IN_PROGRESS','COMPLETED')", name="idempotency_key_status"),
])

#: Ordered table groups per Alembic revision (DATA-MODEL §21.1 M00 row / FS-00 §4.2).
REVISION_0001_TABLES: tuple[Table, ...] = (
    currency, tenant, tenant_settings, exchange_rate, fiscal_period, number_sequence, feature_flag,
    custom_field_def, custom_field_value, role, permission_scope, role_permission, user_role,
    user_data_scope, api_key, approval_definition, approval_request, approval_action, document_link,
    attachment_metadata, app_user,
)
REVISION_0002_TABLES: tuple[Table, ...] = (
    user_identity, tenant_invitation, user_session, tenant_sso_connection, user_identity_link,
    sso_group_role_map, audit_event, finance_journal_intent,
)
REVISION_0003_TABLES: tuple[Table, ...] = (
    outbox_event, event_consumer_checkpoint, job_definition, job_run, idempotency_key,
)
ALL_TABLES: tuple[Table, ...] = REVISION_0001_TABLES + REVISION_0002_TABLES + REVISION_0003_TABLES

#: Append-only tables (UPDATE/DELETE rejected by trigger).
APPEND_ONLY_TABLES: tuple[str, ...] = ("audit_event", "outbox_event", "finance_journal_intent", "approval_action", "document_link")

#: Partitioned tables (created as partitioned parents in their revision).
PARTITIONED_TABLES: tuple[str, ...] = ("audit_event", "outbox_event")
