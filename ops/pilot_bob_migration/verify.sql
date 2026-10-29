-- =============================================================================
-- SCM pilot tenant reset: POST-CHANGE VERIFICATION (read-only).
--
-- Run after reset.sql. Proves:
--   1. the exact tenant-configuration guard blocker query returns no codes
--      (same semantics as app/composition/tenant_configuration.py);
--   2. every table in the catalog-derived reset/wipe set has zero rows for the
--      tenant (nothing was missed, no new demo data appeared since the apply);
--   3. the protected master/reference row counts still match the preflight
--      baseline (`-v baseline_protected_counts=<json>` from preflight's
--      PILOT_PROTECTED_COUNTS_BASELINE marker line);
--   4. the catalog FK graph has no edge from a kept table into a reset table
--      (closure completeness) and an independent per-FK data scan finds zero
--      orphan rows referencing reset tables (this is the check that makes the
--      `session_replication_role=replica` delete safe);
--   5. the tenant base currency is the expected one when `-v expected_currency`
--      is supplied (run once before the PATCH with USD, once after with BOB), and
--      the target currency still exists as an active shared currency.
--
-- Read-only: only SELECTs + session-local TEMPORARY tables. Exits non-zero on
-- any failed check.
--
-- Invocation (run the whole directory as files; do not pipe through stdin):
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--          -v tenant_code=intelranch_pilot \
--          -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
--          -v expected_currency=BOB \
--          -v baseline_protected_counts="$BASELINE" \
--          -f /tmp/pilot_bob_migration/verify.sql
-- =============================================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
\pset footer off

\ir _common.sql

\echo '=== SCM pilot reset verification (read-only) ==='

-- ---------------------------------------------------------------------------
-- Snapshot inputs.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS _mig_wipe_rows, _mig_protected_rows, _mig_baseline,
  _mig_expected, _mig_currency_ready, _mig_checks, _mig_fk_orphans;

CREATE TEMP TABLE _mig_wipe_rows AS
SELECT * FROM pg_temp.mig_wipe_counts((SELECT tenant_id FROM _mig_ctx));

CREATE TEMP TABLE _mig_protected_rows AS
SELECT * FROM pg_temp.mig_protected_counts((SELECT tenant_id FROM _mig_ctx));

CREATE TEMP TABLE _mig_baseline AS
SELECT (SELECT baseline_protected FROM _mig_params)::jsonb AS doc;

CREATE TEMP TABLE _mig_expected (currency text);
\if :{?expected_currency}
INSERT INTO _mig_expected VALUES (:'expected_currency');
\else
INSERT INTO _mig_expected VALUES (NULL);
\endif

CREATE TEMP TABLE _mig_currency_ready AS
SELECT EXISTS (
  SELECT 1 FROM platform.currency
  WHERE code = (SELECT target_currency FROM _mig_params)
    AND is_active
    AND tenant_id IS NULL
) AS ready;

CREATE TEMP TABLE _mig_checks (name text PRIMARY KEY, ok boolean NOT NULL, detail jsonb NOT NULL);
CREATE TEMP TABLE _mig_fk_orphans (
  child_table text NOT NULL, constraint_name text NOT NULL, orphan_rows bigint NOT NULL);

-- ---------------------------------------------------------------------------
-- Check 1: exact guard blocker query.
-- ---------------------------------------------------------------------------
INSERT INTO _mig_checks (name, ok, detail)
SELECT 'blocker_codes_empty',
       count(*) = 0,
       jsonb_build_object(
         'blockers', COALESCE(jsonb_agg(jsonb_build_object(
            'blocker_code', b.blocker_code,
            'table', b.schema_name || '.' || b.table_name,
            'rows', b.row_count)
          ORDER BY b.blocker_code, b.schema_name, b.table_name), '[]'::jsonb))
FROM pg_temp.mig_guard_blockers((SELECT tenant_id FROM _mig_ctx)) b;

-- ---------------------------------------------------------------------------
-- Check 2: every reset table is empty for the tenant.
-- ---------------------------------------------------------------------------
INSERT INTO _mig_checks (name, ok, detail)
SELECT 'wipe_tables_empty',
       count(*) FILTER (WHERE row_count > 0) = 0,
       jsonb_build_object(
         'reset_table_count', count(*),
         'nonempty_tables', COALESCE(jsonb_agg(
            schema_name || '.' || table_name || '=' || row_count
            ORDER BY schema_name, table_name)
           FILTER (WHERE row_count > 0), '[]'::jsonb))
FROM _mig_wipe_rows;

-- ---------------------------------------------------------------------------
-- Check 3: protected master/reference counts match the preflight baseline.
-- ---------------------------------------------------------------------------
DO $mig$
DECLARE
  doc jsonb;
  key text;
  expected bigint;
  actual bigint;
  mismatches jsonb := '[]'::jsonb;
  missing_baseline boolean := true;
BEGIN
  SELECT b.doc INTO doc FROM _mig_baseline b;
  IF doc IS NOT NULL AND doc ? 'counts' THEN
    missing_baseline := false;
    FOR key, expected IN SELECT k, v::bigint FROM jsonb_each_text(doc -> 'counts') AS e(k, v) LOOP
      SELECT row_count INTO actual
      FROM _mig_protected_rows
      WHERE schema_name = split_part(key, '.', 1)
        AND table_name = split_part(key, '.', 2);
      IF actual IS NULL THEN
        mismatches := mismatches || jsonb_build_object(
          'table', key, 'expected', expected, 'actual', NULL, 'reason', 'table not reported');
      ELSIF actual <> expected THEN
        mismatches := mismatches || jsonb_build_object(
          'table', key, 'expected', expected, 'actual', actual, 'reason', 'count changed');
      END IF;
    END LOOP;
  END IF;
  INSERT INTO _mig_checks (name, ok, detail)
  VALUES ('protected_counts_match_baseline',
          NOT missing_baseline AND mismatches = '[]'::jsonb,
          jsonb_build_object(
            'baseline_provided', NOT missing_baseline,
            'mismatches', mismatches,
            'current_counts', COALESCE((
              SELECT jsonb_object_agg(schema_name || '.' || table_name, row_count ORDER BY schema_name, table_name)
              FROM _mig_protected_rows), '{}'::jsonb)));
END
$mig$;

-- ---------------------------------------------------------------------------
-- Check 4a: FK closure completeness (catalog invariant).
-- ---------------------------------------------------------------------------
INSERT INTO _mig_checks (name, ok, detail)
SELECT 'fk_closure_complete',
       count(*) = 0,
       jsonb_build_object(
         'reset_table_count', (SELECT count(*) FROM _mig_wipe),
         'fk_edges_into_reset_tables', count(*),
         'unexpected_kept_children', COALESCE(jsonb_agg(
            child_table || '.' || constraint_name ORDER BY child_table, constraint_name), '[]'::jsonb))
FROM (
  SELECT f.child_schema || '.' || f.child_table AS child_table, f.constraint_name
  FROM _mig_fk f
  JOIN _mig_wipe parent
    ON parent.schema_name = f.parent_schema AND parent.table_name = f.parent_table
  WHERE NOT EXISTS (
    SELECT 1 FROM _mig_wipe child
    WHERE child.schema_name = f.child_schema AND child.table_name = f.child_table)
) gaps;

-- ---------------------------------------------------------------------------
-- Check 4b: independent per-FK orphan data scan. For every catalog FK whose
-- (normalized) parent is a reset table, no remaining row may reference a parent
-- row that no longer exists. Non-reset parents are also empty after the reset,
-- so any surviving reference is an orphan.
-- ---------------------------------------------------------------------------
DO $mig$
DECLARE
  r record;
  n bigint;
  v_tenant uuid;
  child_has_tenant boolean;
  pairs text;
  child_cols text;
  match_cond text;
  child_filter text;
  q text;
BEGIN
  SELECT tenant_id INTO v_tenant FROM _mig_ctx;
  FOR r IN
    SELECT DISTINCT f.constraint_name,
           f.child_schema, f.child_table, f.child_oid,
           f.raw_parent_schema, f.raw_parent_table, f.parent_oid,
           f.child_cols, f.parent_cols
    FROM _mig_fk f
    JOIN _mig_wipe parent
      ON parent.schema_name = f.parent_schema AND parent.table_name = f.parent_table
  LOOP
    SELECT EXISTS (
      SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = r.child_oid AND a.attname = 'tenant_id'
        AND a.attnum > 0 AND NOT a.attisdropped)
    INTO child_has_tenant;

    SELECT string_agg(format('ch.%I = p.%I', ca.attname, pa.attname), ' AND ' ORDER BY i),
           string_agg(format('ch.%I', ca.attname), ', ' ORDER BY i)
    INTO match_cond, child_cols
    FROM generate_subscripts(r.child_cols, 1) AS i
    JOIN pg_attribute ca ON ca.attrelid = r.child_oid AND ca.attnum = r.child_cols[i]
    JOIN pg_attribute pa ON pa.attrelid = r.parent_oid AND pa.attnum = r.parent_cols[i];

    child_filter := CASE WHEN child_has_tenant
                         THEN format('ch.tenant_id = %L AND ', v_tenant)
                         ELSE '' END;
    q := format(
      'SELECT count(*) FROM %I.%I ch WHERE %s(%s) IS NOT NULL '
      'AND NOT EXISTS (SELECT 1 FROM %I.%I p WHERE %s)',
      r.child_schema, r.child_table, child_filter, child_cols,
      r.raw_parent_schema, r.raw_parent_table, match_cond);
    EXECUTE q INTO n;
    IF n > 0 THEN
      INSERT INTO _mig_fk_orphans (child_table, constraint_name, orphan_rows)
      VALUES (r.child_schema || '.' || r.child_table, r.constraint_name, n);
    END IF;
  END LOOP;

  INSERT INTO _mig_checks (name, ok, detail)
  SELECT 'fk_orphan_rows_zero',
         COALESCE(sum(orphan_rows), 0) = 0,
         jsonb_build_object(
           'orphan_rows', COALESCE(sum(orphan_rows), 0),
           'orphans', COALESCE(jsonb_agg(jsonb_build_object(
              'child_table', child_table,
              'constraint', constraint_name,
              'rows', orphan_rows) ORDER BY child_table, constraint_name), '[]'::jsonb))
  FROM _mig_fk_orphans;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Check 5: currency state.
-- ---------------------------------------------------------------------------
INSERT INTO _mig_checks (name, ok, detail)
SELECT 'target_currency_ready',
       ready,
       jsonb_build_object(
         'target_currency', (SELECT target_currency FROM _mig_params),
         'active_shared_currency', ready)
FROM _mig_currency_ready;

INSERT INTO _mig_checks (name, ok, detail)
SELECT 'base_currency_matches_expected',
       CASE WHEN e.currency IS NULL THEN true
            ELSE c.base_currency_code = e.currency END,
       jsonb_build_object(
         'checked', e.currency IS NOT NULL,
         'expected_currency', e.currency,
         'actual_base_currency_code', c.base_currency_code)
FROM _mig_ctx c, _mig_expected e;

-- ---------------------------------------------------------------------------
-- Evidence document + failure gate.
-- ---------------------------------------------------------------------------
BEGIN TRANSACTION READ ONLY;

SELECT jsonb_build_object(
  'script', 'verify',
  'generated_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
  'database', (SELECT database_name FROM _mig_ctx),
  'server_version', (SELECT server_version FROM _mig_ctx),
  'alembic_version', (SELECT alembic_version FROM _mig_ctx),
  'current_role', (SELECT current_role FROM _mig_ctx),
  'is_superuser', (SELECT is_superuser FROM _mig_ctx),
  'tenant', (SELECT jsonb_build_object(
      'code', tenant_code,
      'id', tenant_id,
      'name', tenant_name,
      'base_currency_code', base_currency_code,
      'status', tenant_status,
      'version', tenant_version) FROM _mig_ctx),
  'expected_tenant_id', (SELECT expected_tenant_id FROM _mig_params),
  'expected_currency', (SELECT currency FROM _mig_expected),
  'target_currency', (SELECT target_currency FROM _mig_params),
  'reset_set', jsonb_build_object(
    'reset_table_count', (SELECT count(*) FROM _mig_wipe),
    'fk_closure_added_count', (SELECT count(*) FROM _mig_wipe WHERE origin = 'fk_closure'),
    'guard_table_count', (SELECT count(*) FROM _mig_blocker_table),
    'cost_null_tables', (SELECT count(*) FROM _mig_cost_null)
  ),
  'checks', (SELECT jsonb_object_agg(name, jsonb_build_object('ok', ok, 'detail', detail) ORDER BY name)
             FROM _mig_checks),
  'verification', CASE
      WHEN NOT EXISTS (SELECT 1 FROM _mig_checks WHERE NOT ok) THEN 'ok'
      ELSE 'failed'
    END
)::text AS mig_verify_evidence
FROM (SELECT 1) x
\gset

DO $mig$
DECLARE
  failed jsonb;
BEGIN
  SELECT jsonb_agg(jsonb_build_object('check', name, 'detail', detail) ORDER BY name)
  INTO failed
  FROM _mig_checks WHERE NOT ok;
  IF failed IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset verification FAILED: %', failed;
  END IF;
END
$mig$;

ROLLBACK;

SELECT jsonb_pretty(:'mig_verify_evidence'::jsonb) AS verify_evidence;
\echo PILOT_VERIFY_EVIDENCE :mig_verify_evidence
