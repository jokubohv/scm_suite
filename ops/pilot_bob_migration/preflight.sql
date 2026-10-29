-- =============================================================================
-- SCM pilot tenant reset: PREFLIGHT (read-only evidence).
--
-- Resolves the pilot tenant by code (default intelranch_pilot), prints the exact
-- currency-change blocker set using the same semantics as
-- app/composition/tenant_configuration.py, the catalog-derived wipe set with
-- per-table row counts, the protected master tables and their counts, and the
-- runtime prerequisites for reset.sql.
--
-- Read-only: this script only SELECTs from application tables and creates
-- session-local TEMPORARY objects. It never writes application data.
--
-- Invocation (run the whole directory as files; do not pipe through stdin):
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--          -v tenant_code=intelranch_pilot \
--          -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
--          -f /tmp/pilot_bob_migration/preflight.sql
--
-- Exits non-zero if the tenant is missing, is the SYSTEM tenant, is inconsistent,
-- does not match expected_tenant_id, or if the wipe set would touch protected
-- master data / leave a guard table uncovered.
-- =============================================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
\pset footer off

\echo '=== SCM pilot reset preflight (read-only) ==='
\ir _common.sql

-- Point-in-time snapshots used by the evidence document. Session-local only.
CREATE TEMP TABLE _mig_wipe_rows AS
SELECT * FROM pg_temp.mig_wipe_counts((SELECT tenant_id FROM _mig_ctx));

CREATE TEMP TABLE _mig_protected_rows AS
SELECT * FROM pg_temp.mig_protected_counts((SELECT tenant_id FROM _mig_ctx));

CREATE TEMP TABLE _mig_peers AS
SELECT id, code, base_currency_code, status
FROM platform.tenant
WHERE id <> (SELECT tenant_id FROM _mig_ctx)
ORDER BY code;

CREATE TEMP TABLE _mig_currency_ready AS
SELECT EXISTS (
  SELECT 1 FROM platform.currency
  WHERE code = (SELECT target_currency FROM _mig_params)
    AND is_active
    AND tenant_id IS NULL
) AS ready;

-- Rows belonging to any other tenant inside the wipe set: evidence that the
-- scoped reset cannot delete other tenants' data (re-checked by reset.sql).
CREATE TEMP TABLE _mig_non_target (schema_name text, table_name text, row_count bigint);
DO $mig$
DECLARE
  r record;
  n bigint;
  v_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_tenant FROM _mig_ctx;
  FOR r IN SELECT schema_name, table_name FROM _mig_wipe ORDER BY 1, 2 LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I WHERE tenant_id IS DISTINCT FROM %L',
                   r.schema_name, r.table_name, v_tenant)
      INTO n;
    INSERT INTO _mig_non_target VALUES (r.schema_name, r.table_name, n);
  END LOOP;
END
$mig$;

-- Append-only guard triggers on reset tables. When running as superuser,
-- reset.sql disables origin triggers with SET LOCAL session_replication_role=replica.
CREATE TEMP TABLE _mig_blocking_triggers AS
SELECT n.nspname || '.' || c.relname AS table_name, t.tgname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN _mig_wipe w ON w.schema_name = n.nspname AND w.table_name = c.relname
WHERE NOT t.tgisinternal
  AND (t.tgtype & 8) <> 0
  AND t.tgenabled IN ('O', 'A')
ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- Gather the evidence document (read-only transaction; only SELECTs run here).
-- ---------------------------------------------------------------------------
BEGIN TRANSACTION READ ONLY;

SELECT jsonb_build_object(
  'script', 'preflight',
  'generated_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
  'database', c.database_name,
  'server_version', c.server_version,
  'alembic_version', c.alembic_version,
  'current_role', c.current_role,
  'is_superuser', c.is_superuser,
  'tenant', jsonb_build_object(
    'code', c.tenant_code,
    'id', c.tenant_id,
    'name', c.tenant_name,
    'base_currency_code', c.base_currency_code,
    'status', c.tenant_status,
    'version', c.tenant_version
  ),
  'expected_tenant_id', p.expected_tenant_id,
  'target_currency', p.target_currency,
  'target_currency_ready', (SELECT ready FROM _mig_currency_ready),
  'blockers', jsonb_build_object(
    'codes', COALESCE(to_jsonb(pg_temp.mig_blocker_codes(c.tenant_id)), '[]'::jsonb),
    'tables', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'blocker_code', b.blocker_code,
               'table', b.schema_name || '.' || b.table_name,
               'rows', b.row_count)
             ORDER BY b.blocker_code, b.schema_name, b.table_name)
      FROM pg_temp.mig_guard_blockers(c.tenant_id) b), '[]'::jsonb),
    'total_rows', COALESCE((
      SELECT sum(b.row_count) FROM pg_temp.mig_guard_blockers(c.tenant_id) b), 0)
  ),
  'already_clean', COALESCE(pg_temp.mig_blocker_codes(c.tenant_id), '{}'::text[]) = '{}'::text[],
  'wipe', jsonb_build_object(
    'table_count', (SELECT count(*) FROM _mig_wipe),
    'fk_closure_added_count', (SELECT count(*) FROM _mig_wipe WHERE origin = 'fk_closure'),
    'nonempty_table_count', (SELECT count(*) FROM _mig_wipe_rows WHERE row_count > 0),
    'total_rows_to_delete', (SELECT COALESCE(sum(row_count), 0) FROM _mig_wipe_rows),
    'tables', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'table', w.schema_name || '.' || w.table_name,
               'origin', w.origin,
               'reason', w.reason,
               'rows', r.row_count)
             ORDER BY w.schema_name, w.table_name)
      FROM _mig_wipe w
      JOIN _mig_wipe_rows r
        ON r.schema_name = w.schema_name AND r.table_name = w.table_name), '[]'::jsonb)
  ),
  'fk_closure_added', COALESCE((
    SELECT jsonb_agg(w.schema_name || '.' || w.table_name ORDER BY w.schema_name, w.table_name)
    FROM _mig_wipe w WHERE w.origin = 'fk_closure'), '[]'::jsonb),
  'cost_null', COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
             'table', cn.schema_name || '.' || cn.table_name,
             'set_clause', cn.set_clause,
             'predicate', cn.predicate,
             'blocker_code', cn.blocker_code)
           ORDER BY cn.schema_name, cn.table_name)
    FROM _mig_cost_null cn), '[]'::jsonb),
  'protected', jsonb_build_object(
    'core_table_count', (SELECT count(*) FROM _mig_keep),
    'core_tables', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'table', k.schema_name || '.' || k.table_name,
               'reason', k.reason)
             ORDER BY k.schema_name, k.table_name)
      FROM _mig_keep k), '[]'::jsonb),
    'reported_tables', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'table', p.schema_name || '.' || p.table_name,
               'category', p.category,
               'rows', p.row_count)
             ORDER BY p.category, p.schema_name, p.table_name)
      FROM _mig_protected_rows p), '[]'::jsonb)
  ),
  'other_tenants', jsonb_build_object(
    'count', (SELECT count(*) FROM _mig_peers),
    'peers', COALESCE((
      SELECT jsonb_agg(jsonb_build_object('code', code, 'id', id,
               'base_currency_code', base_currency_code, 'status', status) ORDER BY code)
      FROM _mig_peers), '[]'::jsonb),
    'wipe_rows_non_target_total', (SELECT COALESCE(sum(row_count), 0) FROM _mig_non_target),
    'wipe_tables_with_non_target_rows', COALESCE((
      SELECT jsonb_agg(schema_name || '.' || table_name || '=' || row_count
                       ORDER BY schema_name, table_name)
      FROM _mig_non_target WHERE row_count > 0), '[]'::jsonb)
  ),
  'apply_prerequisites', jsonb_build_object(
    'superuser', c.is_superuser,
    'expected_tenant_id_supplied', p.expected_tenant_id IS NOT NULL,
    'append_only_trigger_tables', COALESCE((
      SELECT jsonb_agg(DISTINCT table_name ORDER BY table_name)
      FROM _mig_blocking_triggers), '[]'::jsonb),
    'append_only_trigger_count', (SELECT count(*) FROM _mig_blocking_triggers)
  ),
  'schema_notes', jsonb_build_object(
    'alembic_head', c.alembic_version,
    'note', CASE
      WHEN c.alembic_version = '0032_currency_global_writes'
        THEN 'production head 0032 (adds only platform.currency RLS policies); scripts are catalog-driven'
      ELSE 'alembic head ' || COALESCE(c.alembic_version, 'absent') ||
           '; scripts are catalog-driven and independent of the 0031/0032 difference'
    END
  )
)::text AS mig_preflight_evidence
FROM _mig_ctx c, _mig_params p
\gset

SELECT jsonb_build_object(
  'tenant_code', (SELECT tenant_code FROM _mig_ctx),
  'tenant_id', (SELECT tenant_id FROM _mig_ctx),
  'generated_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
  'counts', COALESCE((
    SELECT jsonb_object_agg(schema_name || '.' || table_name, row_count ORDER BY schema_name, table_name)
    FROM _mig_protected_rows), '{}'::jsonb)
)::text AS mig_baseline
\gset

ROLLBACK;

-- ---------------------------------------------------------------------------
-- Human-readable evidence + machine-readable marker lines.
-- ---------------------------------------------------------------------------
SELECT jsonb_pretty(:'mig_preflight_evidence'::jsonb) AS preflight_evidence;

\echo PILOT_PREFLIGHT_EVIDENCE :mig_preflight_evidence
\echo PILOT_PROTECTED_COUNTS_BASELINE :mig_baseline
