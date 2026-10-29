-- =============================================================================
-- SCM pilot tenant reset: TRANSACTIONAL APPLY.
--
-- Resets the demo/transactional data of exactly one tenant (resolved by code,
-- pinned with expected_tenant_id) so the deployed tenant-configuration endpoint
-- can later change its base currency. Master/reference data is preserved:
-- items, categories, UoMs, item settings, locations/bins/statuses/zones/lots/
-- serials, vendors + item-vendor config, reason codes, users/roles/API keys,
-- number sequences, currencies and all other platform reference rows.
--
-- Behavior:
--   * every statement is scoped `WHERE tenant_id = <resolved tenant>`; other
--     tenants (incl. SYSTEM) and protected master tables are verified unchanged;
--   * the wipe set is the five transactional module schemas plus the named
--     procurement documents plus platform.finance_journal_intent plus the
--     catalog-derived transitive FK child closure (see _common.sql);
--   * deletes run in child-first FK order computed from the catalog;
--   * because several reset tables are append-only (REVOKE-style guard triggers
--     reject DELETE), the apply REQUIRES a superuser session (production
--     POSTGRES_USER is the compose image superuser) and uses
--     `SET LOCAL session_replication_role = replica` for this transaction only.
--     The closure is checked before deleting and an independent FK-orphan scan is
--     part of verify.sql, so disabling the FK triggers cannot hide a gap;
--   * after deleting, the exact tenant-configuration guard query is re-run in the
--     SAME transaction; any remaining blocker raises, aborts the transaction and
--     psql exits non-zero (nothing is applied);
--   * cost fields counted by the guard (item_inventory.initial_cost,
--     item_purchasing.purchase_price when the currency is NULL) are cleared on
--     the preserved master rows instead of deleting the rows;
--   * tenant row version/base currency and the platform.tenant row count are
--     asserted unchanged, so the subsequent API PATCH keeps its If-Match value.
--
-- Modes (psql variable `mode`):
--   mode=apply     default; commits after all checks pass
--   mode=dry-run   runs the complete delete/update path and then ROLLBACKs; emits
--                  the same evidence with outcome "dry-run-rolled-back"
--
-- Invocation (run the whole directory as files; do not pipe through stdin):
--   docker compose -f docker-compose.prod.yml exec -T postgres \
--     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--          -v mode=dry-run -v tenant_code=intelranch_pilot \
--          -v expected_tenant_id=7ee5cb96-619f-466e-9b34-9d2fef906f10 \
--          -f /tmp/pilot_bob_migration/reset.sql
-- =============================================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
\pset footer off

-- ---------------------------------------------------------------------------
-- Mode validation (apply | dry-run)
-- ---------------------------------------------------------------------------
\if :{?mode}
\else
  \set mode apply
\endif

DROP TABLE IF EXISTS _mig_mode;
CREATE TEMP TABLE _mig_mode (mode text NOT NULL);
INSERT INTO _mig_mode VALUES (:'mode');

DO $mig$
DECLARE
  m text;
BEGIN
  SELECT mode INTO m FROM _mig_mode;
  IF m NOT IN ('apply', 'dry-run') THEN
    RAISE EXCEPTION 'SCM pilot reset refused: mode must be apply or dry-run, got %', m;
  END IF;
END
$mig$;

SELECT (mode = 'dry-run') AS mig_dry_run FROM _mig_mode \gset

\ir _common.sql

SELECT is_superuser AS mig_is_superuser FROM _mig_ctx \gset
SELECT (SELECT expected_tenant_id IS NOT NULL FROM _mig_params) AS mig_expected_id_supplied \gset

-- apply must be pinned to the expected tenant id; dry-run only warns.
DO $mig$
DECLARE
  m text;
  expected text;
BEGIN
  SELECT mode INTO m FROM _mig_mode;
  SELECT expected_tenant_id INTO expected FROM _mig_params;
  IF m = 'apply' AND expected IS NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: apply mode requires -v expected_tenant_id=<tenant uuid>';
  END IF;
END
$mig$;

\echo '=== SCM pilot reset: mode=' :mode ' tenant=' :tenant_code ' ==='

-- ---------------------------------------------------------------------------
-- Session evidence tables. Created before the transaction so that the captured
-- psql variable survives a dry-run ROLLBACK (temp table *content* written inside
-- the transaction would be rolled back).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS _mig_run, _mig_deleted, _mig_cost_cleared,
  _mig_blockers_before, _mig_blockers_after, _mig_wipe_before, _mig_wipe_after,
  _mig_protected_before, _mig_protected_after, _mig_non_target_before,
  _mig_non_target_after, _mig_pending, _mig_delete_order, _mig_lock_sql;

CREATE TEMP TABLE _mig_run (key text PRIMARY KEY, value text);
CREATE TEMP TABLE _mig_deleted (
  schema_name text NOT NULL, table_name text NOT NULL, row_count bigint NOT NULL,
  PRIMARY KEY (schema_name, table_name));
CREATE TEMP TABLE _mig_cost_cleared (
  schema_name text NOT NULL, table_name text NOT NULL, row_count bigint NOT NULL,
  PRIMARY KEY (schema_name, table_name));
CREATE TEMP TABLE _mig_blockers_before (
  blocker_code text NOT NULL, schema_name text NOT NULL, table_name text NOT NULL,
  row_count bigint NOT NULL);
CREATE TEMP TABLE _mig_blockers_after (LIKE _mig_blockers_before);
CREATE TEMP TABLE _mig_wipe_before (
  schema_name text NOT NULL, table_name text NOT NULL, row_count bigint NOT NULL);
CREATE TEMP TABLE _mig_wipe_after (LIKE _mig_wipe_before);
CREATE TEMP TABLE _mig_protected_before (
  schema_name text NOT NULL, table_name text NOT NULL, category text NOT NULL,
  row_count bigint NOT NULL);
CREATE TEMP TABLE _mig_protected_after (LIKE _mig_protected_before);
CREATE TEMP TABLE _mig_non_target_before (
  schema_name text NOT NULL, table_name text NOT NULL, row_count bigint NOT NULL);
CREATE TEMP TABLE _mig_non_target_after (LIKE _mig_non_target_before);
CREATE TEMP TABLE _mig_pending (
  schema_name text NOT NULL, table_name text NOT NULL,
  PRIMARY KEY (schema_name, table_name));
CREATE TEMP TABLE _mig_delete_order (
  seq serial PRIMARY KEY, schema_name text NOT NULL, table_name text NOT NULL,
  UNIQUE (schema_name, table_name));
CREATE TEMP TABLE _mig_lock_sql (query text);

INSERT INTO _mig_lock_sql (query)
SELECT format('LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE NOWAIT',
              string_agg(format('%I.%I', schema_name, table_name), ', '
                         ORDER BY schema_name, table_name))
FROM (SELECT schema_name, table_name FROM _mig_wipe
      UNION
      SELECT schema_name, table_name FROM _mig_cost_null) t;

-- Row counts for every other tenant inside the wipe set (must not change).
CREATE OR REPLACE FUNCTION pg_temp.mig_nontarget_counts(p_tenant uuid)
RETURNS TABLE (schema_name text, table_name text, row_count bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  r record;
  n bigint;
BEGIN
  FOR r IN SELECT w.schema_name, w.table_name FROM pg_temp._mig_wipe w ORDER BY 1, 2 LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I WHERE tenant_id IS DISTINCT FROM %L',
                   r.schema_name, r.table_name, p_tenant)
      INTO n;
    schema_name := r.schema_name;
    table_name := r.table_name;
    row_count := n;
    RETURN NEXT;
  END LOOP;
END
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.mig_run_value(p_key text)
RETURNS text
LANGUAGE sql AS $fn$
  SELECT value FROM pg_temp._mig_run WHERE key = p_key
$fn$;

-- Evidence document builder: reads only session-local tables/psql state.
CREATE OR REPLACE FUNCTION pg_temp.mig_reset_evidence(p_outcome text)
RETURNS jsonb
LANGUAGE plpgsql AS $fn$
DECLARE
  result jsonb;
BEGIN
  SELECT jsonb_build_object(
    'script', 'reset',
    'outcome', p_outcome,
    'mode', (SELECT mode FROM pg_temp._mig_mode),
    'generated_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
    'database', (SELECT database_name FROM pg_temp._mig_ctx),
    'server_version', (SELECT server_version FROM pg_temp._mig_ctx),
    'alembic_version', (SELECT alembic_version FROM pg_temp._mig_ctx),
    'current_role', (SELECT current_role FROM pg_temp._mig_ctx),
    'is_superuser', (SELECT is_superuser FROM pg_temp._mig_ctx),
    'replica_used', COALESCE(pg_temp.mig_run_value('replica_used') = 't', false),
    'target_currency', (SELECT target_currency FROM pg_temp._mig_params),
    'tenant', (SELECT jsonb_build_object(
        'code', tenant_code,
        'id', tenant_id,
        'name', tenant_name,
        'base_currency_before', pg_temp.mig_run_value('base_currency_before'),
        'base_currency_after', pg_temp.mig_run_value('base_currency_after'),
        'version_before', pg_temp.mig_run_value('tenant_version_before'),
        'version_after', pg_temp.mig_run_value('tenant_version_after'),
        'base_currency_unchanged',
          pg_temp.mig_run_value('base_currency_before') IS NOT DISTINCT FROM pg_temp.mig_run_value('base_currency_after'),
        'version_unchanged',
          pg_temp.mig_run_value('tenant_version_before') IS NOT DISTINCT FROM pg_temp.mig_run_value('tenant_version_after')
      ) FROM pg_temp._mig_ctx),
    'locks', jsonb_build_object(
      'tenant_row', 'FOR UPDATE NOWAIT',
      'table_lock_mode', 'SHARE ROW EXCLUSIVE MODE NOWAIT',
      'tables_locked', COALESCE(pg_temp.mig_run_value('tables_locked')::integer, 0)
    ),
    'guard', jsonb_build_object(
      'guard_tables', (SELECT count(*) FROM pg_temp._mig_blocker_table),
      'reset_tables', (SELECT count(*) FROM pg_temp._mig_wipe),
      'fk_closure_added', (SELECT count(*) FROM pg_temp._mig_wipe WHERE origin = 'fk_closure'),
      'fk_closure_added_tables', COALESCE((
        SELECT jsonb_agg(w.schema_name || '.' || w.table_name ORDER BY w.schema_name, w.table_name)
        FROM pg_temp._mig_wipe w WHERE w.origin = 'fk_closure'), '[]'::jsonb)
    ),
    'blockers_before', jsonb_build_object(
      'codes', COALESCE((
        SELECT jsonb_agg(DISTINCT blocker_code ORDER BY blocker_code)
        FROM pg_temp._mig_blockers_before), '[]'::jsonb),
      'total_rows', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_blockers_before), 0),
      'tables', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                 'blocker_code', blocker_code,
                 'table', schema_name || '.' || table_name,
                 'rows', row_count)
               ORDER BY blocker_code, schema_name, table_name)
        FROM pg_temp._mig_blockers_before), '[]'::jsonb)
    ),
    'blockers_after', jsonb_build_object(
      'codes', COALESCE((
        SELECT jsonb_agg(DISTINCT blocker_code ORDER BY blocker_code)
        FROM pg_temp._mig_blockers_after), '[]'::jsonb),
      'total_rows', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_blockers_after), 0),
      'tables', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                 'blocker_code', blocker_code,
                 'table', schema_name || '.' || table_name,
                 'rows', row_count)
               ORDER BY blocker_code, schema_name, table_name)
        FROM pg_temp._mig_blockers_after), '[]'::jsonb)
    ),
    'deleted', jsonb_build_object(
      'wipe_table_count', (SELECT count(*) FROM pg_temp._mig_wipe),
      'tables_with_deleted_rows', (SELECT count(*) FROM pg_temp._mig_deleted WHERE row_count > 0),
      'total_rows', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_deleted), 0),
      'tables', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
                 'table', d.schema_name || '.' || d.table_name,
                 'rows', d.row_count,
                 'origin', w.origin)
               ORDER BY d.schema_name, d.table_name)
        FROM pg_temp._mig_deleted d
        JOIN pg_temp._mig_wipe w
          ON w.schema_name = d.schema_name AND w.table_name = d.table_name
        WHERE d.row_count > 0), '[]'::jsonb)
    ),
    'cost_fields_cleared', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
               'table', c.schema_name || '.' || c.table_name,
               'rows', cc.row_count,
               'set_clause', c.set_clause,
               'predicate', c.predicate)
             ORDER BY c.schema_name, c.table_name)
      FROM pg_temp._mig_cost_cleared cc
      JOIN pg_temp._mig_cost_null c
        ON c.schema_name = cc.schema_name AND c.table_name = cc.table_name), '[]'::jsonb),
    'wipe_rows_remaining', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_wipe_after), 0),
    'protected', jsonb_build_object(
      'counts_before', COALESCE((
        SELECT jsonb_object_agg(schema_name || '.' || table_name, row_count)
        FROM pg_temp._mig_protected_before), '{}'::jsonb),
      'counts_after', COALESCE((
        SELECT jsonb_object_agg(schema_name || '.' || table_name, row_count)
        FROM pg_temp._mig_protected_after), '{}'::jsonb),
      'decreased', COALESCE((
        SELECT jsonb_agg(b.schema_name || '.' || b.table_name ORDER BY b.schema_name, b.table_name)
        FROM pg_temp._mig_protected_before b
        JOIN pg_temp._mig_protected_after a
          ON a.schema_name = b.schema_name AND a.table_name = b.table_name
        WHERE a.row_count < b.row_count), '[]'::jsonb),
      'unchanged', NOT EXISTS (
        SELECT 1
        FROM pg_temp._mig_protected_before b
        JOIN pg_temp._mig_protected_after a
          ON a.schema_name = b.schema_name AND a.table_name = b.table_name
        WHERE a.row_count < b.row_count)
    ),
    'non_target_scope', jsonb_build_object(
      'rows_before', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_non_target_before), 0),
      'rows_after', COALESCE((SELECT sum(row_count) FROM pg_temp._mig_non_target_after), 0),
      'unchanged', NOT EXISTS (
        SELECT 1
        FROM pg_temp._mig_non_target_before b
        JOIN pg_temp._mig_non_target_after a
          ON a.schema_name = b.schema_name AND a.table_name = b.table_name
        WHERE a.row_count IS DISTINCT FROM b.row_count)
    ),
    'platform_tenant_rows', jsonb_build_object(
      'before', pg_temp.mig_run_value('tenant_count_before')::bigint,
      'after', pg_temp.mig_run_value('tenant_count_after')::bigint
    )
  ) INTO result;
  RETURN result;
END
$fn$;

-- ---------------------------------------------------------------------------
-- Apply transaction.
-- ---------------------------------------------------------------------------
BEGIN;

-- Serialize against tenant configuration changes with the same row lock the
-- configuration endpoint takes; then snapshot the tenant row values we promise
-- not to modify.
DO $mig$
DECLARE
  v_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_tenant FROM pg_temp._mig_ctx;
  BEGIN
    PERFORM 1 FROM platform.tenant WHERE id = v_tenant FOR UPDATE NOWAIT;
  EXCEPTION WHEN lock_not_available THEN
    RAISE EXCEPTION 'SCM pilot reset aborted: tenant row % is locked by another session (retry when idle)', v_tenant;
  END;
  IF NOT EXISTS (SELECT 1 FROM platform.tenant WHERE id = v_tenant) THEN
    RAISE EXCEPTION 'SCM pilot reset aborted: tenant row % disappeared before apply', v_tenant;
  END IF;
END
$mig$;

INSERT INTO _mig_run (key, value) VALUES
  ('replica_used', :'mig_is_superuser'),
  ('tables_locked', '0');

INSERT INTO _mig_run (key, value)
SELECT 'tenant_version_before', version::text FROM platform.tenant
WHERE id = (SELECT tenant_id FROM _mig_ctx);
INSERT INTO _mig_run (key, value)
SELECT 'base_currency_before', base_currency_code FROM platform.tenant
WHERE id = (SELECT tenant_id FROM _mig_ctx);
INSERT INTO _mig_run (key, value)
SELECT 'tenant_count_before', count(*)::text FROM platform.tenant;

-- Snapshot blockers/master counts/wipe counts/non-target rows before the reset.
INSERT INTO _mig_blockers_before
SELECT * FROM pg_temp.mig_guard_blockers((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_wipe_before
SELECT * FROM pg_temp.mig_wipe_counts((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_protected_before
SELECT * FROM pg_temp.mig_protected_counts((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_non_target_before
SELECT * FROM pg_temp.mig_nontarget_counts((SELECT tenant_id FROM _mig_ctx));

-- Lock every table that will be deleted from or updated so no concurrent posting
-- can insert a blocker between the post-delete guard re-check and COMMIT.
DO $mig$
DECLARE
  q text;
  n integer;
BEGIN
  SELECT query INTO q FROM _mig_lock_sql;
  BEGIN
    EXECUTE q;
  EXCEPTION WHEN lock_not_available THEN
    RAISE EXCEPTION 'SCM pilot reset aborted: another session holds a conflicting lock on a reset table (retry when idle)';
  END;
  SELECT (SELECT count(*) FROM _mig_wipe) + (SELECT count(*) FROM _mig_cost_null) INTO n;
  UPDATE _mig_run SET value = n::text WHERE key = 'tables_locked';
END
$mig$;

-- Guard triggers: append-only tables reject DELETE, and item_inventory rejects
-- changes to initial_cost. Both are ORIGIN triggers, so the transaction needs
-- `session_replication_role = replica` (superuser only). Refuse rather than
-- partially deleting when that prerequisite is not met.
\if :mig_is_superuser
SET LOCAL session_replication_role = replica;
\else
DO $mig$
DECLARE
  bad text;
BEGIN
  SELECT string_agg(format('%s.%s', w.schema_name, w.table_name), ', ' ORDER BY 1)
  INTO bad
  FROM _mig_wipe w
  WHERE EXISTS (
    SELECT 1 FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = w.schema_name AND c.relname = w.table_name
      AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A')
  );
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: running as non-superuser % but guard triggers block DELETE/UPDATE on: %; run as the compose POSTGRES_USER superuser', current_user, bad;
  END IF;
END
$mig$;
\endif

-- ---------------------------------------------------------------------------
-- Delete in catalog-derived child-first FK order. Every statement is tenant
-- scoped; counts are recorded per table for evidence.
-- ---------------------------------------------------------------------------
DO $mig$
DECLARE
  r record;
  n integer;
  remaining integer;
  cycles text;
  v_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_tenant FROM _mig_ctx;
  INSERT INTO _mig_pending (schema_name, table_name)
  SELECT schema_name, table_name FROM _mig_wipe;

  LOOP
    INSERT INTO _mig_delete_order (schema_name, table_name)
    SELECT p.schema_name, p.table_name
    FROM _mig_pending p
    WHERE NOT EXISTS (
      SELECT 1
      FROM _mig_fk f
      JOIN _mig_pending child
        ON child.schema_name = f.child_schema AND child.table_name = f.child_table
      WHERE f.parent_schema = p.schema_name AND f.parent_table = p.table_name
        AND NOT (f.child_schema = p.schema_name AND f.child_table = p.table_name)
    )
    ORDER BY p.schema_name, p.table_name;
    GET DIAGNOSTICS n = ROW_COUNT;
    DELETE FROM _mig_pending
    WHERE (schema_name, table_name) IN
          (SELECT schema_name, table_name FROM _mig_delete_order);
    EXIT WHEN n = 0;
  END LOOP;

  SELECT count(*) INTO remaining FROM _mig_pending;
  IF remaining > 0 THEN
    SELECT string_agg(schema_name || '.' || table_name, ', ' ORDER BY 1) INTO cycles FROM _mig_pending;
    RAISE EXCEPTION 'SCM pilot reset refused: FK delete-order cycle across % tables: %', remaining, cycles;
  END IF;

  FOR r IN SELECT schema_name, table_name FROM _mig_delete_order ORDER BY seq LOOP
    EXECUTE format('DELETE FROM %I.%I WHERE tenant_id = $1', r.schema_name, r.table_name)
      USING v_tenant;
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO _mig_deleted (schema_name, table_name, row_count)
    VALUES (r.schema_name, r.table_name, n);
  END LOOP;
END
$mig$;

-- Cost fields the guard counts are cleared on the preserved item master rows.
DO $mig$
DECLARE
  r record;
  n bigint;
  v_tenant uuid;
BEGIN
  SELECT tenant_id INTO v_tenant FROM _mig_ctx;
  FOR r IN SELECT schema_name, table_name, set_clause, predicate
           FROM _mig_cost_null ORDER BY schema_name, table_name LOOP
    EXECUTE format('UPDATE %I.%I SET %s WHERE tenant_id = $1 AND (%s)',
                   r.schema_name, r.table_name, r.set_clause, r.predicate)
      USING v_tenant;
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO _mig_cost_cleared (schema_name, table_name, row_count)
    VALUES (r.schema_name, r.table_name, n);
  END LOOP;
END
$mig$;

-- ---------------------------------------------------------------------------
-- In-transaction proof: exact guard query must be empty, all reset tables must
-- be empty for this tenant, master rows must not have decreased, and no other
-- tenant's rows may have changed.
-- ---------------------------------------------------------------------------
INSERT INTO _mig_blockers_after
SELECT * FROM pg_temp.mig_guard_blockers((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_wipe_after
SELECT * FROM pg_temp.mig_wipe_counts((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_protected_after
SELECT * FROM pg_temp.mig_protected_counts((SELECT tenant_id FROM _mig_ctx));
INSERT INTO _mig_non_target_after
SELECT * FROM pg_temp.mig_nontarget_counts((SELECT tenant_id FROM _mig_ctx));

INSERT INTO _mig_run (key, value)
SELECT 'tenant_version_after', version::text FROM platform.tenant
WHERE id = (SELECT tenant_id FROM _mig_ctx);
INSERT INTO _mig_run (key, value)
SELECT 'base_currency_after', base_currency_code FROM platform.tenant
WHERE id = (SELECT tenant_id FROM _mig_ctx);
INSERT INTO _mig_run (key, value)
SELECT 'tenant_count_after', count(*)::text FROM platform.tenant;

DO $mig$
DECLARE
  bad text;
BEGIN
  SELECT string_agg(format('%s.%s (%s rows)', schema_name, table_name, row_count), ', ' ORDER BY 1)
  INTO bad
  FROM _mig_blockers_after;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): currency blocker rows remain after reset: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s (%s rows)', schema_name, table_name, row_count), ', ' ORDER BY 1)
  INTO bad
  FROM _mig_wipe_after WHERE row_count > 0;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): reset tables still contain tenant rows: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s (before %s, after %s)', b.schema_name, b.table_name, b.row_count, a.row_count), ', ' ORDER BY 1)
  INTO bad
  FROM _mig_protected_before b
  JOIN _mig_protected_after a
    ON a.schema_name = b.schema_name AND a.table_name = b.table_name
  WHERE a.row_count < b.row_count;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): protected master rows decreased: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s (before %s, after %s)', b.schema_name, b.table_name, b.row_count, a.row_count), ', ' ORDER BY 1)
  INTO bad
  FROM _mig_non_target_before b
  JOIN _mig_non_target_after a
    ON a.schema_name = b.schema_name AND a.table_name = b.table_name
  WHERE a.row_count IS DISTINCT FROM b.row_count;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): rows outside the target tenant changed: %', bad;
  END IF;

  IF pg_temp.mig_run_value('base_currency_before')
     IS DISTINCT FROM pg_temp.mig_run_value('base_currency_after') THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): tenant base currency changed unexpectedly';
  END IF;
  IF pg_temp.mig_run_value('tenant_version_before')
     IS DISTINCT FROM pg_temp.mig_run_value('tenant_version_after') THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): tenant version changed unexpectedly';
  END IF;
  IF pg_temp.mig_run_value('tenant_count_before')
     IS DISTINCT FROM pg_temp.mig_run_value('tenant_count_after') THEN
    RAISE EXCEPTION 'SCM pilot reset aborted (ROLLBACK): platform.tenant row count changed';
  END IF;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Capture evidence, then commit (apply) or roll back (dry-run).
-- ---------------------------------------------------------------------------
\if :mig_dry_run
SELECT pg_temp.mig_reset_evidence('dry-run-rolled-back')::text AS mig_reset_evidence \gset
ROLLBACK;
\echo PILOT_RESET_OUTCOME dry-run-rolled-back
\else
COMMIT;
SELECT pg_temp.mig_reset_evidence('applied')::text AS mig_reset_evidence \gset
\echo PILOT_RESET_OUTCOME applied-committed
\endif

SELECT jsonb_pretty(:'mig_reset_evidence'::jsonb) AS reset_evidence;
\echo PILOT_RESET_EVIDENCE :mig_reset_evidence
