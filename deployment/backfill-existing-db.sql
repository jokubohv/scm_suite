-- Idempotent backfills for databases created before the latest migrations.
DROP POLICY IF EXISTS global_currency_read ON platform.currency;
CREATE POLICY global_currency_read ON platform.currency FOR SELECT USING (tenant_id IS NULL);

INSERT INTO platform.feature_flag (id, tenant_id, flag_key, is_enabled, source, created_at, updated_at, version)
SELECT gen_random_uuid(), t.id, 'traceability.registry.enabled', false, 'BACKFILL', now(), now(), 1
FROM platform.tenant t
WHERE t.id <> '00000000-0000-0000-0000-000000000000'::uuid
  AND NOT EXISTS (
      SELECT 1 FROM platform.feature_flag f
      WHERE f.tenant_id = t.id AND f.flag_key = 'traceability.registry.enabled'
  );
