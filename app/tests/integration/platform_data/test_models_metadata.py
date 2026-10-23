"""Static model/metadata checks for FS-00 (no database required)."""

from __future__ import annotations

from app.core.models.platform import ALL_TABLES, APPEND_ONLY_TABLES, PARTITIONED_TABLES, metadata


def test_all_34_tables_registered() -> None:
    assert len(metadata.tables) == 34
    assert {t.name for t in ALL_TABLES} == set(metadata.tables) or {t.name for t in ALL_TABLES} == {
        name.split(".", 1)[1] for name in metadata.tables
    }


def test_standard_columns_except_documented_globals() -> None:
    for table in ALL_TABLES:
        if table.name == "currency":
            assert "code" in table.c and "tenant_id" in table.c
            continue
        if table.name == "user_identity":
            assert "tenant_id" not in table.c
            continue
        assert "tenant_id" in table.c, table.name
        if table.name in {"audit_event", "outbox_event"}:
            # Append-only event stores use occurred_at; they have no updated_at/version by design.
            assert "occurred_at" in table.c
            continue
        assert "created_at" in table.c and "version" in table.c, table.name


def test_check_constraints_and_indexes_present() -> None:
    tenant = metadata.tables["platform.tenant"]
    assert any(type(c).__name__ == "CheckConstraint" for c in tenant.constraints)
    outbox = metadata.tables["platform.outbox_event"]
    assert outbox.indexes
    assert "postgresql_partition_by" in outbox.dialect_kwargs
    assert PARTITIONED_TABLES == ("audit_event", "outbox_event")
    assert set(APPEND_ONLY_TABLES) == {"audit_event", "outbox_event", "finance_journal_intent", "approval_action", "document_link"}


def test_identity_and_sso_final_shape() -> None:
    identity = metadata.tables["platform.user_identity"]
    assert {"email", "status", "is_platform_admin", "sso_subject"} <= set(identity.c.keys())
    app_user = metadata.tables["platform.app_user"]
    assert {"identity_id", "membership_status", "token_version", "joined_at"} <= set(app_user.c.keys())
    sso = metadata.tables["platform.tenant_sso_connection"]
    assert {"protocol", "status", "dex_issuer_url", "email_domains", "is_enforced"} <= set(sso.c.keys())
