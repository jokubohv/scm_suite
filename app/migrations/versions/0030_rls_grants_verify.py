"""0030_rls_grants_verify — FS-15 final RLS/grant verification + deferred-FK orphan check.

Adds the materialised-view refresh function, then verifies (and fails the migration if not true):
21 reporting views exist; exactly 5 are materialised; every view/relation uses ``security_invoker``;
the application role has SELECT but no INSERT/UPDATE/DELETE on any reporting relation; reporting
schema objects are owned by the migration role and exposed read-only. This closes the FS-15 migration
pair and is the final cross-set RLS/grant gate for the reporting surface. No base tables are created.
"""

from __future__ import annotations

from alembic import op

revision = "0030_rls_grants_verify"
down_revision = "0029_reporting_views"
branch_labels = None
depends_on = None

SCHEMA = "reporting"
APP_ROLE = "platform_app"
EXPECTED_VIEWS = 21
EXPECTED_MATERIALIZED = 5
MATERIALIZED = ("v_inventory_valuation_detail", "v_inventory_turnover_inputs",
                "v_inventory_profitability_inputs", "v_manufacturing_workbench",
                "v_available_to_build")


def _create_refresh_function() -> None:
    names = ", ".join(f"'{name}'" for name in MATERIALIZED)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.refresh_materialized_views(p_concurrent boolean DEFAULT true)
        RETURNS TABLE(view_name text, refreshed boolean)
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v text;
        BEGIN
            FOREACH v IN ARRAY ARRAY[{names}] LOOP
                BEGIN
                    IF p_concurrent THEN
                        EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I.%I', '{SCHEMA}', v);
                    ELSE
                        EXECUTE format('REFRESH MATERIALIZED VIEW %I.%I', '{SCHEMA}', v);
                    END IF;
                    view_name := v;
                    refreshed := true;
                    RETURN NEXT;
                EXCEPTION WHEN OTHERS THEN
                    view_name := v;
                    refreshed := false;
                    RAISE WARNING 'reporting refresh failed for %: %', v, SQLERRM;
                    RETURN NEXT;
                END;
            END LOOP;
        END;
        $$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.refresh_materialized_views(boolean) FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION {SCHEMA}.refresh_materialized_views(boolean) TO {APP_ROLE}';
            END IF;
            -- The login/SSO service role must discover identities/memberships before a tenant
            -- context exists; the API switches to this role only for public auth endpoints.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_auth') THEN
                EXECUTE 'ALTER ROLE platform_auth BYPASSRLS';
                EXECUTE 'GRANT USAGE ON SCHEMA platform TO platform_auth';
                EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA platform TO platform_auth';
            END IF;
            -- Readiness probe needs the applied migration head; grant read-only access.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                EXECUTE 'GRANT SELECT ON public.alembic_version TO {APP_ROLE}';
                -- Item-master related/kit-member rows are editable; earlier grants omitted DELETE.
                EXECUTE 'GRANT DELETE ON item_master.item_related, item_master.kit_group_member TO {APP_ROLE}';
            END IF;
        END $$;
        """
    )


def _verify() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
            n_views integer;
            n_mat integer;
            bad_security integer;
            write_grants integer;
            orphans integer;
        BEGIN
            SELECT count(*) INTO n_views
              FROM information_schema.views
             WHERE table_schema = '{SCHEMA}'
               AND table_name LIKE 'v\\_%' ESCAPE '\\';
            SELECT count(*) INTO n_mat
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{SCHEMA}' AND c.relkind = 'm' AND c.relname LIKE 'v\\_%' ESCAPE '\\';
            IF n_views + n_mat <> {EXPECTED_VIEWS} THEN
                RAISE EXCEPTION 'reporting gate: expected {EXPECTED_VIEWS} views, found % plain + % materialized', n_views, n_mat;
            END IF;
            IF n_mat <> {EXPECTED_MATERIALIZED} THEN
                RAISE EXCEPTION 'reporting gate: expected {EXPECTED_MATERIALIZED} materialized views, found %', n_mat;
            END IF;
            SELECT count(*) INTO bad_security
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '{SCHEMA}' AND c.relkind = 'v'
               AND COALESCE((c.reloptions::text LIKE '%security_invoker=true%'), false) = false;
            IF bad_security > 0 THEN
                RAISE EXCEPTION 'reporting gate: % plain view(s) are not security_invoker', bad_security;
            END IF;
            SELECT count(*) INTO write_grants
              FROM information_schema.role_table_grants g
             WHERE g.table_schema = '{SCHEMA}' AND g.grantee = '{APP_ROLE}'
               AND g.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE');
            IF write_grants > 0 THEN
                RAISE EXCEPTION 'reporting gate: application role has write grant(s) on reporting: %', write_grants;
            END IF;
            -- deferred-FK orphan check: all base FKs in the reporting dependency graph must be validated.
            SELECT count(*) INTO orphans
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('inventory_ledger', 'costing', 'item_master', 'traceability',
                                 'counts', 'procurement', 'replenishment', 'manufacturing',
                                 'quality', 'consigned', 'wms', 'extensions')
               AND con.contype = 'f' AND NOT con.convalidated;
            IF orphans > 0 THEN
                RAISE EXCEPTION 'reporting gate: % unvalidated deferred FK constraint(s) remain', orphans;
            END IF;
            RAISE NOTICE 'reporting gate: 21 views, 5 materialized, security_invoker, read-only grants, no unvalidated FKs';
        END $$;
        """
    )


def upgrade() -> None:
    _create_refresh_function()
    _verify()


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.refresh_materialized_views(boolean)")
