"""0003_platform_events — transactional outbox (partitioned), consumer checkpoints, job catalogue/runs,
idempotency keys (FS-00, DATA-MODEL §21.1 M00 row)."""

from __future__ import annotations

import uuid

from alembic import op

from app.core.models import platform as models
from app.core.models.schema_ddl import (
    SYSTEM_TENANT_ID,
    apply_tenant_rls,
    create_partitioned,
    create_tables,
    disable_tenant_rls,
    drop_tables,
    grant_app_role_privileges,
    install_append_only_trigger,
    install_standard_triggers,
    tenant_table_names,
)

revision = "0003_platform_events"
down_revision = "0002_platform_security"
branch_labels = None
depends_on = None

EVENT_TABLES = models.REVISION_0003_TABLES
RLS_TABLES = tenant_table_names(EVENT_TABLES)

JOB_SEEDS: tuple[tuple[str, str, str, str, int], ...] = (
    ("platform.outbox.relay", "outbox", "platform.outbox.relay", "* * * * *", 900),
    ("platform.jobs.scheduler", "default", "platform.jobs.scheduler", "*/30 * * * * *", 60),
    ("platform.retention.purge", "default", "platform.retention.purge", "0 2 * * *", 3600),
    ("platform.sso.health.monitor", "default", "platform.sso.health.monitor", "*/15 * * * *", 300),
)


def upgrade() -> None:
    create_partitioned(op, models.outbox_event, months_before=1, months_after=2)
    install_append_only_trigger(op, "outbox_event")
    create_tables(op, tuple(t for t in EVENT_TABLES if t.name != "outbox_event"))
    install_standard_triggers(op, EVENT_TABLES)
    apply_tenant_rls(op, RLS_TABLES)
    grant_app_role_privileges(op, RLS_TABLES)
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts, singleton, is_enabled,
                 created_at, updated_at, version)
            VALUES
                ('{job_id}', '{SYSTEM_TENANT_ID}', '{name}', '{queue}', '{handler}', '{cron}', {timeout}, 3, true, true,
                 now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def downgrade() -> None:
    disable_tenant_rls(op, RLS_TABLES)
    drop_tables(op, EVENT_TABLES)
