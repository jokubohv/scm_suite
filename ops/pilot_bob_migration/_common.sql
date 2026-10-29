-- =============================================================================
-- SCM pilot tenant reset: shared discovery / safety layer.
--
-- Included by preflight.sql, reset.sql and verify.sql with:   \ir _common.sql
-- Requires the whole directory to be copied together; always invoke the scripts
-- as files (-f), never piped through stdin.
--
-- This file builds session-local TEMPORARY objects only. It never writes to an
-- application table. All object discovery comes from the PostgreSQL catalogs so
-- the scripts do not depend on the exact Alembic revision (local 0031 vs
-- production 0032_currency_global_writes only adds platform.currency policies).
--
-- What it establishes, for the tenant resolved from :tenant_code:
--   _mig_params             psql inputs (tenant code, target currency, expected id,
--                           protected-count baseline)
--   _mig_ctx                resolved tenant + runtime/schema facts (fails if missing/SYSTEM)
--   _mig_blocker_table      exact mirror of app/composition/tenant_configuration.py _USAGE
--                           (40 tables + predicates + CurrencyChangeBlocker code)
--   _mig_keep               protected master/reference tables (never deleted)
--   _mig_wipe               tables whose tenant rows are reset: module roots plus the
--                           transitive FK child closure, verified against _mig_keep
--   _mig_cost_null          the two item_master cost fields the guard counts that are
--                           cleared instead of deleting the master rows
--   _mig_fk                 normalized FK graph (partition parents resolved to root)
--   _mig_protected_report   master tables reported before/after for preservation
--   pg_temp.mig_*           helpers: guard blockers, wipe counts, protected counts
--
-- Any safety violation raises an exception: with ON_ERROR_STOP=1 psql exits
-- non-zero and (inside reset.sql's transaction) nothing is applied.
-- =============================================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off
\pset footer off

-- Hide catalog DROP ... IF EXISTS notices from the evidence log; warnings/errors stay visible.
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- psql inputs and defaults
-- ---------------------------------------------------------------------------
\if :{?tenant_code}
\else
  \set tenant_code intelranch_pilot
\endif
\if :{?target_currency}
\else
  \set target_currency BOB
\endif

-- Re-runnable inside one psql session (e.g. preflight then verify): drop and
-- recreate the session-local objects so definitions can never go stale.
DROP TABLE IF EXISTS _mig_fk, _mig_blocker_table, _mig_keep, _mig_wipe,
  _mig_cost_null, _mig_protected_report, _mig_ctx, _mig_params CASCADE;
DROP FUNCTION IF EXISTS pg_temp.mig_guard_blockers(uuid);
DROP FUNCTION IF EXISTS pg_temp.mig_blocker_codes(uuid);
DROP FUNCTION IF EXISTS pg_temp.mig_wipe_counts(uuid);
DROP FUNCTION IF EXISTS pg_temp.mig_protected_counts(uuid);

CREATE TEMP TABLE _mig_params (
  tenant_code            text NOT NULL,
  target_currency        text NOT NULL,
  expected_tenant_id     text,
  baseline_protected     text
);

INSERT INTO _mig_params (tenant_code, target_currency)
VALUES (:'tenant_code', :'target_currency');

\if :{?expected_tenant_id}
UPDATE _mig_params SET expected_tenant_id = :'expected_tenant_id';
\endif
\if :{?baseline_protected_counts}
UPDATE _mig_params SET baseline_protected = :'baseline_protected_counts';
\endif

CREATE TEMP TABLE _mig_ctx AS
SELECT t.id                 AS tenant_id,
       t.tenant_id          AS tenant_row_tenant_id,
       t.code               AS tenant_code,
       t.name               AS tenant_name,
       t.base_currency_code,
       t.status             AS tenant_status,
       t.version            AS tenant_version,
       current_database()   AS database_name,
       current_user         AS current_role,
       current_setting('is_superuser')::boolean AS is_superuser,
       current_setting('server_version') AS server_version,
       CASE WHEN to_regclass('public.alembic_version') IS NULL THEN 'absent'
            ELSE (SELECT version_num::text FROM public.alembic_version) END AS alembic_version
FROM platform.tenant t
JOIN _mig_params p ON p.tenant_code = t.code;

-- ---------------------------------------------------------------------------
-- Resolve and validate the target tenant. Fails (non-zero psql exit) when the
-- tenant code is missing, ambiguous, the reserved SYSTEM tenant, inconsistent,
-- or does not match the required expected_tenant_id.
-- ---------------------------------------------------------------------------
DO $mig$
DECLARE
  c    _mig_ctx%ROWTYPE;
  p    _mig_params%ROWTYPE;
  n    integer;
  want uuid;
BEGIN
  SELECT * INTO p FROM _mig_params;
  SELECT count(*) INTO n FROM _mig_ctx;
  IF n = 0 THEN
    RAISE EXCEPTION 'SCM pilot reset refused: no platform.tenant row with code %', p.tenant_code;
  END IF;
  IF n > 1 THEN
    RAISE EXCEPTION 'SCM pilot reset refused: tenant code % is ambiguous (% rows)', p.tenant_code, n;
  END IF;
  SELECT * INTO STRICT c FROM _mig_ctx;

  IF c.tenant_code = 'SYSTEM' OR c.tenant_id = '00000000-0000-0000-0000-000000000000'::uuid THEN
    RAISE EXCEPTION 'SCM pilot reset refused: % is the reserved SYSTEM tenant', c.tenant_code;
  END IF;
  IF c.tenant_id IS DISTINCT FROM c.tenant_row_tenant_id THEN
    RAISE EXCEPTION 'SCM pilot reset refused: tenant % has id % but tenant_id % (inconsistent row)',
      c.tenant_code, c.tenant_id, c.tenant_row_tenant_id;
  END IF;
  IF p.expected_tenant_id IS NOT NULL THEN
    BEGIN
      want := p.expected_tenant_id::uuid;
    EXCEPTION WHEN others THEN
      RAISE EXCEPTION 'SCM pilot reset refused: expected_tenant_id % is not a UUID', p.expected_tenant_id;
    END;
    IF c.tenant_id <> want THEN
      RAISE EXCEPTION 'SCM pilot reset refused: tenant % resolves to id %, expected %',
        c.tenant_code, c.tenant_id, want;
    END IF;
  END IF;
  IF c.tenant_status <> 'ACTIVE' THEN
    RAISE WARNING 'SCM pilot tenant % has status % (continuing read-only/proof, review before apply)',
      c.tenant_code, c.tenant_status;
  END IF;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Exact mirror of the currency-change blocker tables and predicates from
-- app/composition/tenant_configuration.py::_USAGE and ::__blocker. Kept as data
-- so the guard re-check is generated from one place. A local consistency check
-- (verification/pilot-bob-migration/check_guard_mirror.py) diffs this table
-- against the Python source.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _mig_blocker_table (
  schema_name  text NOT NULL,
  table_name   text NOT NULL,
  predicate    text NOT NULL,
  blocker_code text NOT NULL,
  PRIMARY KEY (schema_name, table_name)
);

INSERT INTO _mig_blocker_table (schema_name, table_name, predicate, blocker_code) VALUES
  ('consigned', 'consignment_agreement_item', 'true', 'consignment_records'),
  ('consigned', 'consignment_consumption', 'true', 'consignment_records'),
  ('consigned', 'consignment_ownership_transfer', 'true', 'consignment_records'),
  ('costing', 'cogs_adjustment', 'true', 'cost_history'),
  ('costing', 'cost_layer', 'true', 'cost_history'),
  ('costing', 'cost_ledger_entry', 'true', 'cost_history'),
  ('costing', 'cost_revaluation', 'true', 'cost_history'),
  ('costing', 'cost_rollup_line', 'true', 'cost_history'),
  ('costing', 'cost_template_line', 'fixed_amount IS NOT NULL', 'cost_configuration'),
  ('costing', 'cost_variance', 'true', 'cost_history'),
  ('costing', 'group_average_balance', 'true', 'cost_history'),
  ('costing', 'item_cost', 'true', 'cost_history'),
  ('costing', 'landed_cost', 'true', 'cost_history'),
  ('costing', 'planned_standard_cost', 'true', 'cost_configuration'),
  ('costing', 'standard_cost', 'true', 'cost_configuration'),
  ('counts', 'inventory_count_snapshot', 'true', 'count_snapshots'),
  ('inventory_ledger', 'inventory_adjustment_line', 'true', 'inventory_documents'),
  ('inventory_ledger', 'inventory_balance', 'true', 'inventory_balances'),
  ('inventory_ledger', 'inventory_distribution_line', 'true', 'inventory_documents'),
  ('inventory_ledger', 'inventory_in_transit', 'true', 'inventory_documents'),
  ('inventory_ledger', 'inventory_transaction', 'true', 'inventory_history'),
  ('inventory_ledger', 'inventory_worksheet_line', 'true', 'inventory_documents'),
  ('inventory_ledger', 'transfer_order', 'transfer_price IS NOT NULL', 'inventory_documents'),
  ('item_master', 'item_inventory', 'initial_cost IS NOT NULL', 'item_cost_configuration'),
  ('item_master', 'item_purchasing', 'purchase_price IS NOT NULL AND purchase_price_currency IS NULL', 'item_cost_configuration'),
  ('manufacturing', 'assembly_build', 'true', 'manufacturing_history'),
  ('manufacturing', 'assembly_item', 'build_cost <> 0', 'manufacturing_cost_configuration'),
  ('manufacturing', 'assembly_unbuild', 'true', 'manufacturing_history'),
  ('manufacturing', 'manufacturing_cost_template', 'true', 'manufacturing_cost_configuration'),
  ('manufacturing', 'manufacturing_cost_template_line', 'true', 'manufacturing_cost_configuration'),
  ('manufacturing', 'routing_step_charge', 'true', 'manufacturing_cost_configuration'),
  ('manufacturing', 'wip_transaction', 'true', 'manufacturing_history'),
  ('manufacturing', 'work_center', 'cost_per_hour <> 0', 'manufacturing_cost_configuration'),
  ('manufacturing', 'work_order_wip_balance', 'true', 'manufacturing_history'),
  ('platform', 'finance_journal_intent', 'true', 'finance_journals'),
  ('procurement', 'item_receipt', 'true', 'procurement_documents'),
  ('procurement', 'purchase_order', 'true', 'procurement_documents'),
  ('procurement', 'vendor_bill', 'true', 'procurement_documents'),
  ('procurement', 'vendor_credit', 'true', 'procurement_documents'),
  ('procurement', 'vendor_return_authorization', 'true', 'procurement_documents');

-- ---------------------------------------------------------------------------
-- Protected master/reference tables: never deleted. Everything here is needed
-- by the Serebo tenant link (items/vendors/locations/lots/users/API keys) or by
-- SCM operation after the reset (number sequences, statuses, reason codes,
-- categories/UoMs/item settings, currency, auth/RBAC/platform reference data).
-- Deliberately excludes platform.finance_journal_intent (a blocker table).
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _mig_keep (
  schema_name text NOT NULL,
  table_name  text NOT NULL,
  reason      text NOT NULL,
  PRIMARY KEY (schema_name, table_name)
);

INSERT INTO _mig_keep (schema_name, table_name, reason)
SELECT n.nspname, c.relname, 'platform tenant/auth/RBAC/reference data'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'platform'
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relname <> 'finance_journal_intent';

INSERT INTO _mig_keep (schema_name, table_name, reason)
SELECT n.nspname, c.relname, 'item master / item settings / categories / UoMs'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'item_master' AND c.relkind IN ('r', 'p') AND NOT c.relispartition;

INSERT INTO _mig_keep (schema_name, table_name, reason)
SELECT n.nspname, c.relname, 'traceability locations/bins/statuses/zones/lots/serials'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'traceability' AND c.relkind IN ('r', 'p') AND NOT c.relispartition;

INSERT INTO _mig_keep (schema_name, table_name, reason)
SELECT n.nspname, c.relname, 'procurement vendor master / item-vendor config'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'procurement' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relname = ANY (ARRAY['vendor', 'vendor_category', 'item_vendor', 'item_vendor_price',
                             'vendor_contract', 'vendor_contract_item']);

INSERT INTO _mig_keep (schema_name, table_name, reason)
SELECT n.nspname, c.relname, 'inventory reason codes'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'inventory_ledger' AND c.relname = 'inventory_reason_code'
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition;

-- Two item_master rows keep their identity but lose exactly the value the guard
-- counts. Explicitly denominated prices (purchase_price_currency IS NOT NULL)
-- are not blockers and are left untouched.
CREATE TEMP TABLE _mig_cost_null (
  schema_name text NOT NULL,
  table_name  text NOT NULL,
  set_clause  text NOT NULL,
  predicate   text NOT NULL,
  blocker_code text NOT NULL,
  PRIMARY KEY (schema_name, table_name)
);

INSERT INTO _mig_cost_null (schema_name, table_name, set_clause, predicate, blocker_code) VALUES
  ('item_master', 'item_inventory',
   'initial_cost = NULL, updated_at = now(), version = version + 1',
   'initial_cost IS NOT NULL', 'item_cost_configuration'),
  ('item_master', 'item_purchasing',
   'purchase_price = NULL, updated_at = now(), version = version + 1',
   'purchase_price IS NOT NULL AND purchase_price_currency IS NULL', 'item_cost_configuration');

-- ---------------------------------------------------------------------------
-- Normalized FK graph: one row per foreign key whose child is a real table.
-- Partition parents are resolved to the top-level partitioned table so the reset
-- can never miss a relationship through a partition.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _mig_fk AS
WITH RECURSIVE parts AS (
  SELECT c.oid AS reloid, c.oid AS rootoid
  FROM pg_class c
  WHERE NOT c.relispartition
  UNION ALL
  SELECT i.inhrelid, p.rootoid
  FROM pg_inherits i
  JOIN parts p ON p.reloid = i.inhparent
)
SELECT con.oid                AS constraint_oid,
       con.conname            AS constraint_name,
       cn.nspname             AS child_schema,
       cc.relname             AS child_table,
       pn.nspname             AS parent_schema,
       pc.relname             AS parent_table,
       pcn.nspname            AS raw_parent_schema,
       pcc.relname            AS raw_parent_table,
       con.conrelid           AS child_oid,
       con.confrelid          AS parent_oid,
       con.conkey             AS child_cols,
       con.confkey            AS parent_cols,
       con.confdeltype::text  AS on_delete
FROM pg_constraint con
JOIN pg_class cc      ON cc.oid = con.conrelid
JOIN pg_namespace cn  ON cn.oid = cc.relnamespace
JOIN parts pr         ON pr.reloid = con.confrelid
JOIN pg_class pc      ON pc.oid = pr.rootoid
JOIN pg_namespace pn  ON pn.oid = pc.relnamespace
JOIN pg_class pcc     ON pcc.oid = con.confrelid
JOIN pg_namespace pcn ON pcn.oid = pcc.relnamespace
WHERE con.contype = 'f'
  AND cc.relkind IN ('r', 'p') AND NOT cc.relispartition
  AND cn.nspname NOT IN ('pg_catalog', 'information_schema')
  AND pn.nspname NOT IN ('pg_catalog', 'information_schema');

-- ---------------------------------------------------------------------------
-- Wipe set.  Roots:
--   * every table of the transactional module schemas inventory_ledger,
--     costing, manufacturing, consigned and counts, except the explicitly kept
--     inventory_ledger.inventory_reason_code reference table
--   * the procurement documents named by the migration plan (orders, receipts,
--     bills, credits, returns and their lines/details)
--   * platform.finance_journal_intent
-- Then the transitive FK child closure is added (module-internal children are
-- already roots; cross-module children such as wms.receiving_order are added).
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _mig_wipe (
  schema_name text NOT NULL,
  table_name  text NOT NULL,
  origin      text NOT NULL,
  reason      text NOT NULL,
  PRIMARY KEY (schema_name, table_name)
);

INSERT INTO _mig_wipe (schema_name, table_name, origin, reason)
SELECT n.nspname, c.relname, 'module_schema',
       'tenant demo data reset: transactional module ' || n.nspname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname = ANY (ARRAY['inventory_ledger', 'costing', 'manufacturing', 'consigned', 'counts'])
  AND NOT EXISTS (SELECT 1 FROM _mig_keep k
                  WHERE k.schema_name = n.nspname AND k.table_name = c.relname);

INSERT INTO _mig_wipe (schema_name, table_name, origin, reason)
SELECT n.nspname, c.relname, 'procurement_document',
       'tenant demo data reset: procurement documents'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname = 'procurement'
  AND c.relname = ANY (ARRAY[
        'purchase_order', 'purchase_order_line', 'purchase_order_release',
        'item_receipt', 'item_receipt_detail', 'item_receipt_line',
        'vendor_bill', 'vendor_bill_line', 'vendor_bill_match', 'vendor_bill_variance',
        'vendor_credit', 'vendor_credit_line',
        'vendor_return_authorization', 'vendor_return_authorization_line'])
  AND NOT EXISTS (SELECT 1 FROM _mig_keep k
                  WHERE k.schema_name = n.nspname AND k.table_name = c.relname);

INSERT INTO _mig_wipe (schema_name, table_name, origin, reason)
SELECT n.nspname, c.relname, 'finance_journal',
       'tenant demo data reset: finance journal intents (currency blocker)'
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND n.nspname = 'platform' AND c.relname = 'finance_journal_intent';

-- Transitive FK child closure, catalog-driven. Children in _mig_keep are also
-- inserted here so the protected-table check below can abort on them instead of
-- silently leaving FK orphans behind.
DO $mig$
DECLARE
  n integer;
BEGIN
  LOOP
    WITH candidates AS (
      SELECT fk.child_schema, fk.child_table,
             string_agg(DISTINCT fk.parent_schema || '.' || fk.parent_table, ', ' ORDER BY fk.parent_schema || '.' || fk.parent_table) AS parents
      FROM _mig_fk fk
      JOIN _mig_wipe w ON w.schema_name = fk.parent_schema AND w.table_name = fk.parent_table
      WHERE NOT EXISTS (SELECT 1 FROM _mig_wipe c
                        WHERE c.schema_name = fk.child_schema AND c.table_name = fk.child_table)
      GROUP BY fk.child_schema, fk.child_table
    )
    INSERT INTO _mig_wipe (schema_name, table_name, origin, reason)
    SELECT child_schema, child_table, 'fk_closure', 'FK child of reset table(s): ' || parents
    FROM candidates
    ON CONFLICT (schema_name, table_name) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    EXIT WHEN n = 0;
  END LOOP;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Safety checks: refuse to run unless every guard table is covered, no
-- protected table entered the wipe set, no unexpected cross-module dependency
-- appeared, and every wipe table is tenant scoped.
-- ---------------------------------------------------------------------------
DO $mig$
DECLARE
  bad text;
  n integer;
BEGIN
  SELECT string_agg(format('%s.%s', bt.schema_name, bt.table_name), ', ' ORDER BY bt.schema_name, bt.table_name)
  INTO bad
  FROM _mig_blocker_table bt
  WHERE NOT EXISTS (SELECT 1 FROM _mig_wipe w
                    WHERE w.schema_name = bt.schema_name AND w.table_name = bt.table_name)
    AND NOT EXISTS (SELECT 1 FROM _mig_cost_null c
                    WHERE c.schema_name = bt.schema_name AND c.table_name = bt.table_name);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: guard blocker tables are neither reset nor cost-cleared: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s', w.schema_name, w.table_name), ', ' ORDER BY w.schema_name, w.table_name)
  INTO bad
  FROM _mig_wipe w
  JOIN _mig_keep k ON k.schema_name = w.schema_name AND k.table_name = w.table_name;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: protected/master tables would be deleted: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s', w.schema_name, w.table_name), ', ' ORDER BY w.schema_name, w.table_name)
  INTO bad
  FROM _mig_wipe w
  WHERE w.origin = 'fk_closure'
    AND w.schema_name NOT IN ('inventory_ledger', 'costing', 'manufacturing', 'consigned',
                              'counts', 'procurement', 'wms');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: unexpected cross-module FK dependents need review: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s', w.schema_name, w.table_name), ', ' ORDER BY w.schema_name, w.table_name)
  INTO bad
  FROM _mig_wipe w
  WHERE NOT EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname = w.schema_name AND c.relname = w.table_name
  );
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: reset tables without tenant_id column: %', bad;
  END IF;

  SELECT string_agg(format('%s.%s', w.schema_name, w.table_name), ', ' ORDER BY w.schema_name, w.table_name)
  INTO bad
  FROM _mig_wipe w
  WHERE w.origin = 'fk_closure'
    AND NOT EXISTS (SELECT 1 FROM information_schema.tables t
                    WHERE t.table_schema = w.schema_name AND t.table_name = w.table_name);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: reset table not found in catalog: %', bad;
  END IF;

  SELECT count(*) INTO n FROM _mig_wipe;
  IF n = 0 THEN
    RAISE EXCEPTION 'SCM pilot reset refused: empty wipe set (schema discovery failed)';
  END IF;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Master table counts reported before/after as preservation evidence.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _mig_protected_report (
  schema_name text NOT NULL,
  table_name  text NOT NULL,
  category    text NOT NULL,
  global_scope boolean NOT NULL DEFAULT false,
  PRIMARY KEY (schema_name, table_name)
);

INSERT INTO _mig_protected_report (schema_name, table_name, category, global_scope) VALUES
  ('platform', 'tenant', 'tenant', false),
  ('platform', 'app_user', 'users', false),
  ('platform', 'user_role', 'memberships', false),
  ('platform', 'api_key', 'api_keys', false),
  ('platform', 'role', 'roles', false),
  ('platform', 'role_permission', 'role_permissions', false),
  ('platform', 'permission_scope', 'scopes', false),
  ('platform', 'number_sequence', 'number_sequences', false),
  ('platform', 'tenant_setting', 'settings', false),
  ('platform', 'currency', 'currencies', true),
  ('item_master', 'item', 'items', false),
  ('item_master', 'item_category', 'item_categories', false),
  ('item_master', 'item_type_definition', 'item_types', false),
  ('item_master', 'uom', 'uoms', false),
  ('item_master', 'uom_type', 'uom_types', false),
  ('item_master', 'item_uom', 'item_uoms', false),
  ('item_master', 'item_inventory', 'item_inventory', false),
  ('item_master', 'item_purchasing', 'item_purchasing', false),
  ('traceability', 'location', 'locations', false),
  ('traceability', 'bin', 'bins', false),
  ('traceability', 'lot', 'lots', false),
  ('traceability', 'zone', 'zones', false),
  ('traceability', 'inventory_status', 'inventory_statuses', false),
  ('traceability', 'serial_number', 'serial_numbers', false),
  ('procurement', 'vendor', 'vendors', false),
  ('procurement', 'vendor_category', 'vendor_categories', false),
  ('procurement', 'item_vendor', 'item_vendors', false),
  ('procurement', 'vendor_contract', 'vendor_contracts', false),
  ('procurement', 'vendor_contract_item', 'vendor_contract_items', false),
  ('inventory_ledger', 'inventory_reason_code', 'reason_codes', false);

-- Fail closed if a reported master table is absent from this schema revision.
DO $mig$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(format('%s.%s', r.schema_name, r.table_name), ', ' ORDER BY r.schema_name, r.table_name)
  INTO missing
  FROM _mig_protected_report r
  WHERE to_regclass(format('%I.%I', r.schema_name, r.table_name)) IS NULL;
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'SCM pilot reset refused: expected master table(s) not found: %', missing;
  END IF;
END
$mig$;

-- ---------------------------------------------------------------------------
-- Helpers (session-local).
-- ---------------------------------------------------------------------------

-- Exact guard blocker query semantics: one row per blocker table with matching
-- tenant rows, plus the CurrencyChangeBlocker code. Mirrors the UNION ALL query
-- built by SqlCurrencyUsage.blockers().
CREATE OR REPLACE FUNCTION pg_temp.mig_guard_blockers(p_tenant uuid)
RETURNS TABLE (blocker_code text, schema_name text, table_name text, row_count bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  q text;
BEGIN
  SELECT string_agg(
           format('SELECT %L::text AS blocker_code, %L::text AS schema_name, %L::text AS table_name, '
                  'count(*) AS row_count FROM %I.%I WHERE tenant_id = %L AND (%s)',
                  bt.blocker_code, bt.schema_name, bt.table_name,
                  bt.schema_name, bt.table_name, p_tenant, bt.predicate),
           ' UNION ALL ' ORDER BY bt.schema_name, bt.table_name)
  INTO q
  FROM pg_temp._mig_blocker_table bt;
  IF q IS NULL THEN
    RETURN;
  END IF;
  RETURN QUERY EXECUTE
    'SELECT blocker_code, schema_name, table_name, row_count FROM (' || q || ') s '
    'WHERE row_count > 0 ORDER BY blocker_code, schema_name, table_name';
END
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.mig_blocker_codes(p_tenant uuid)
RETURNS text[]
LANGUAGE plpgsql AS $fn$
DECLARE
  codes text[];
BEGIN
  SELECT array_agg(DISTINCT blocker_code ORDER BY blocker_code)
  INTO codes
  FROM pg_temp.mig_guard_blockers(p_tenant);
  RETURN codes;
END
$fn$;

-- Row counts (tenant scoped) for every table in the wipe set.
CREATE OR REPLACE FUNCTION pg_temp.mig_wipe_counts(p_tenant uuid)
RETURNS TABLE (schema_name text, table_name text, row_count bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  r record;
  n bigint;
BEGIN
  FOR r IN SELECT w.schema_name, w.table_name FROM pg_temp._mig_wipe w ORDER BY 1, 2 LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I WHERE tenant_id = %L', r.schema_name, r.table_name, p_tenant)
      INTO n;
    schema_name := r.schema_name;
    table_name := r.table_name;
    row_count := n;
    RETURN NEXT;
  END LOOP;
END
$fn$;

-- Counts of the protected master rows (tenant scoped unless global_scope).
CREATE OR REPLACE FUNCTION pg_temp.mig_protected_counts(p_tenant uuid)
RETURNS TABLE (schema_name text, table_name text, category text, row_count bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
  r record;
  n bigint;
  where_sql text;
BEGIN
  FOR r IN SELECT * FROM pg_temp._mig_protected_report ORDER BY 1, 2 LOOP
    where_sql := CASE WHEN r.global_scope THEN 'true'
                      ELSE format('tenant_id = %L', p_tenant) END;
    EXECUTE format('SELECT count(*) FROM %I.%I WHERE %s', r.schema_name, r.table_name, where_sql)
      INTO n;
    schema_name := r.schema_name;
    table_name := r.table_name;
    category := r.category;
    row_count := n;
    RETURN NEXT;
  END LOOP;
END
$fn$;
