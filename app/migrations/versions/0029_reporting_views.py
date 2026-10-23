"""0029_reporting_views — FS-15 reporting read models.

Creates the ``reporting`` schema and exactly the 21 read-only views V-01..V-21 (16 plain, 5
materialised: V-04, V-11, V-12, V-16, V-21). No base tables are created (FS-15 owns 0 tables).
Views over later-wave contexts (V-13/V-14/V-16/V-19/V-20/V-21) are created from their real source
relations when present, with a typed empty fallback during partial-wave applies; the final wave-2
apply (after 0028) exercises the real definitions. Seeds the four reporting RBAC scopes, the
``reporting.readmodels.enabled`` flag and the two reporting jobs.

Chain position: 0029, after FS-13/14 ``0028_extensions``. Reversible: downgrade drops the reporting
schema and the seed rows.
"""

from __future__ import annotations

import uuid

from alembic import op

from app.modules.reporting.views import VIEWS, ViewSpec

revision = "0029_reporting_views"
down_revision = "0028_extensions"
branch_labels = None
depends_on = None

SCHEMA = "reporting"
SYSTEM_TENANT_ID = "00000000-0000-0000-0000-000000000000"
APP_ROLE = "platform_app"

# Catalogue authority (design/permissions.json) currently has 3 reporting scopes; FS-15 §3 also names
# ``reporting.admin`` but the catalogue is authoritative until it is added there (reported to the Lead).
REPORTING_SCOPES: tuple[tuple[str, str, str, str], ...] = (
    ("reporting.read", "reporting", "read", "Read reporting views and report endpoints"),
    ("reporting.cost.view", "reporting", "view", "View cost/valuation/margin reporting columns"),
    ("reporting.export", "reporting", "export", "Export report result sets"),
)

ROLE_REPORTING: dict[str, tuple[str, ...]] = {
    "admin": ("reporting.read", "reporting.cost.view", "reporting.export"),
    "planner": ("reporting.read", "reporting.cost.view"),
    "buyer": ("reporting.read", "reporting.cost.view"),
    "warehouse_operator": ("reporting.read", "reporting.cost.view"),
    "cost_accountant": ("reporting.read", "reporting.cost.view"),
    "quality_inspector": ("reporting.read", "reporting.cost.view"),
    "readonly": ("reporting.read", "reporting.cost.view"),
}

JOB_SEEDS: tuple[tuple[str, str, str, str, int], ...] = (
    ("reporting.refresh_views", "default", "reporting.refresh_views", "*/5 * * * *", 900),
    ("reporting.reconcile_readmodels", "default", "reporting.reconcile_readmodels", "0 3 * * *", 1800),
)

#: future-wave view -> required source relation
SOURCE_GUARDS: dict[str, str] = {
    "v_atp_current": "replenishment.atp_projection",
    "v_planning_workbench": "replenishment.supply_plan_suggestion",
    "v_manufacturing_workbench": "manufacturing.work_order",
    "v_consigned_balance": "consigned.consignment_agreement_item",
    "v_quality_inspection_status": "quality.inspection",
    "v_available_to_build": "platform.outbox_event",
}


def _create_view(spec: ViewSpec) -> None:
    kind = "MATERIALIZED VIEW" if spec.materialized else "VIEW"
    # PostgreSQL 16 supports security_invoker on plain views; materialized views cannot take the
    # option (they are refreshed by the definer job and protected by read-only grants).
    options = "" if spec.materialized else " WITH (security_invoker=true)"
    if spec.name not in SOURCE_GUARDS:
        op.execute(f"CREATE {kind} {SCHEMA}.{spec.name}{options} AS {spec.sql}")
        return
    rel = SOURCE_GUARDS[spec.name]
    real_sql = f"CREATE {kind} {SCHEMA}.{spec.name}{options} AS {spec.sql}"
    fallback_sql = f"CREATE {kind} {SCHEMA}.{spec.name}{options} AS {spec.fallback_sql}"
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('{rel}') IS NOT NULL THEN
                BEGIN
                    EXECUTE $ddl${real_sql}$ddl$;
                EXCEPTION WHEN OTHERS THEN
                    RAISE WARNING 'reporting: fallback for {spec.name} after source mismatch: %', SQLERRM;
                    EXECUTE $ddl${fallback_sql}$ddl$;
                END;
            ELSE
                RAISE WARNING 'reporting: source {rel} not present; creating typed fallback for {spec.name}';
                EXECUTE $ddl${fallback_sql}$ddl$;
            END IF;
        END $$;
        """
    )


def _create_indexes() -> None:
    unique: dict[str, str] = {
        "v_inventory_valuation_detail": "(tenant_id, layer_id)",
        "v_inventory_turnover_inputs": "(tenant_id, item_id, period_start)",
        "v_inventory_profitability_inputs": "(tenant_id, item_id, period_start)",
        "v_manufacturing_workbench": "(tenant_id, work_order_id)",
        "v_available_to_build": "(tenant_id, assembly_item, location_id, level_mode, basis, as_of)",
    }
    for name, keys in unique.items():
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_reporting_{name} ON {SCHEMA}.{name} {keys}")


def _seed_scopes() -> None:
    values = ", ".join(
        "('{}', '{}', '{}', '{}')".format(*entry) for entry in REPORTING_SCOPES
    )
    op.execute(
        f"""
        INSERT INTO platform.permission_scope
            (id, tenant_id, scope, domain, action, description, requires_approval,
             is_platform_only, is_assignable, owner_feature_set, created_at, updated_at, version)
        SELECT gen_random_uuid(), t.id, s.scope, s.domain, s.action, s.description, false,
               false, true, 'FS-15', now(), now(), 1
          FROM platform.tenant t
         CROSS JOIN (VALUES {values}) AS s(scope, domain, action, description)
        ON CONFLICT (tenant_id, scope) DO NOTHING
        """
    )


def _seed_role_links() -> None:
    for role_code, scopes in ROLE_REPORTING.items():
        scope_list = ", ".join(f"'{scope}'" for scope in scopes)
        op.execute(
            f"""
            INSERT INTO platform.role_permission
                (tenant_id, role_id, scope_id, created_at, updated_at, version)
            SELECT r.tenant_id, r.id, ps.id, now(), now(), 1
              FROM platform.role r
              JOIN platform.permission_scope ps
                ON ps.tenant_id = r.tenant_id AND ps.scope IN ({scope_list})
             WHERE r.code = '{role_code}'
            ON CONFLICT DO NOTHING
            """
        )


def _seed_flags() -> None:
    op.execute(
        """
        INSERT INTO platform.feature_flag
            (id, tenant_id, flag_key, is_enabled, source, notes, created_at, updated_at, version)
        SELECT gen_random_uuid(), t.id, 'reporting.readmodels.enabled', false, 'MIGRATION',
               'FS-15 reporting read models (deploy-dark)', now(), now(), 1
          FROM platform.tenant t
        ON CONFLICT (tenant_id, flag_key) DO NOTHING
        """
    )


def _seed_jobs() -> None:
    for name, queue, handler, cron, timeout in JOB_SEEDS:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platform.jobs/{name}"))
        op.execute(
            f"""
            INSERT INTO platform.job_definition
                (id, tenant_id, name, queue, handler, cron_expr, timeout_seconds, max_attempts,
                 singleton, is_enabled, created_at, updated_at, version)
            VALUES
                ('{job_id}', '{SYSTEM_TENANT_ID}'::uuid, '{name}', '{queue}', '{handler}', '{cron}',
                 {timeout}, 3, true, true, now(), now(), 1)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """
        )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for spec in VIEWS:
        _create_view(spec)
    _create_indexes()
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {APP_ROLE}';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )
    _seed_scopes()
    _seed_role_links()
    _seed_flags()
    _seed_jobs()


def downgrade() -> None:
    for name, _, _, _ in REPORTING_SCOPES:
        op.execute(f"DELETE FROM platform.permission_scope WHERE scope = '{name}' AND owner_feature_set = 'FS-15'")
    op.execute("DELETE FROM platform.feature_flag WHERE flag_key = 'reporting.readmodels.enabled'")
    for name, *_ in JOB_SEEDS:
        op.execute(f"DELETE FROM platform.job_definition WHERE name = '{name}' AND tenant_id = '{SYSTEM_TENANT_ID}'::uuid")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
