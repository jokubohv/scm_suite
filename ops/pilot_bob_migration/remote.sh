#!/usr/bin/env bash
# Scoped SCM pilot migration executor: backup, preflight, reset (dry-run/apply), verify.
# Runs on the SCM production VPS from the deployment directory. Never prints secrets.
set -euo pipefail
umask 077
: "${MODE:?MODE is required (backup|preflight|dry-run|apply|verify)}"
VPS_DEPLOY_DIR="${VPS_DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
TENANT_CODE="${TENANT_CODE:-intelranch_pilot}"
EXPECTED_TENANT_ID="${EXPECTED_TENANT_ID:-7ee5cb96-619f-466e-9b34-9d2fef906f10}"
EXPECTED_CURRENCY="${EXPECTED_CURRENCY:-BOB}"
OPS_DIR="$VPS_DEPLOY_DIR/ops/pilot_bob_migration"
WORK_DIR="$VPS_DEPLOY_DIR/backups/pilot-bob-migration"
cd "$VPS_DEPLOY_DIR"
test -f .env.production || { echo "FATAL: missing .env.production"; exit 1; }
test -f docker-compose.prod.yml || { echo "FATAL: missing docker-compose.prod.yml"; exit 1; }
test -f "$OPS_DIR/_common.sql" || { echo "FATAL: migration files missing at $OPS_DIR"; exit 1; }

compose() { docker compose --env-file .env.production -f docker-compose.prod.yml "$@"; }
psql_base="psql -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -v expected_tenant_id=$EXPECTED_TENANT_ID"
psql_file() { compose exec -T postgres sh -c "$psql_base -v tenant_code=$TENANT_CODE -f $1"; }
psql_verify() { compose exec -T postgres sh -c "$psql_base -v tenant_code=$TENANT_CODE -v expected_currency=$1 -v baseline_protected_counts='$2' -f /tmp/pilot_bob_migration/verify.sql"; }
psql_reset() { compose exec -T postgres sh -c "$psql_base -v tenant_code=$TENANT_CODE -v mode=$MODE -f /tmp/pilot_bob_migration/reset.sql"; }

mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR" "$VPS_DEPLOY_DIR/backups" 2>/dev/null || true
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="$WORK_DIR/pilot-pretx-$STAMP.dump"
BASELINE_FILE="$WORK_DIR/protected_baseline.json"
PF_LOG="$WORK_DIR/preflight-$STAMP.log"

echo "== MODE=$MODE STAMP=$STAMP TENANT=$TENANT_CODE EXPECTED_CURRENCY=$EXPECTED_CURRENCY =="

if [ "$MODE" != "verify" ]; then
  echo "== database size before =="
  compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select pg_size_pretty(pg_database_size(current_database())), current_database()"'
  echo "== backup =="
  compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > "$BACKUP_FILE"
  test -s "$BACKUP_FILE" || { echo "FATAL: empty backup"; exit 1; }
  SZ=$(wc -c < "$BACKUP_FILE" | tr -d ' ')
  SHA=$(sha256sum "$BACKUP_FILE" | awk '{print $1}')
  echo "BACKUP_FILE=$BACKUP_FILE SIZE_BYTES=$SZ SHA256=$SHA"
  compose cp "$BACKUP_FILE" postgres:/tmp/pilot-pretx.dump >/dev/null
  COUNT=$(compose exec -T postgres sh -c 'pg_restore --list /tmp/pilot-pretx.dump | wc -l' | tr -d ' ')
  compose exec -T postgres sh -c 'rm -f /tmp/pilot-pretx.dump'
  echo "BACKUP_RESTORE_LIST_ENTRIES=$COUNT"
  test "$COUNT" -gt 100 || { echo "FATAL: backup archive not readable/too small"; exit 1; }
fi

if [ "$MODE" = "backup" ]; then
  echo "RESULT=backup-only-ok"
  exit 0
fi

echo "== copy migration files into postgres container =="
compose exec -T postgres sh -c 'rm -rf /tmp/pilot_bob_migration'
compose cp "$OPS_DIR" postgres:/tmp/pilot_bob_migration >/dev/null
compose exec -T postgres sh -c 'ls -1 /tmp/pilot_bob_migration'

if [ "$MODE" = "verify" ]; then
  test -s "$BASELINE_FILE" || { echo "FATAL: no persisted preflight baseline at $BASELINE_FILE"; exit 1; }
  BASELINE="$(cat "$BASELINE_FILE")"
  echo "== verify (read-only, expected_currency=$EXPECTED_CURRENCY) =="
  psql_verify "$EXPECTED_CURRENCY" "$BASELINE"
  echo "RESULT=verify-ok EXPECTED_CURRENCY=$EXPECTED_CURRENCY"
  exit 0
fi

echo "== preflight =="
psql_file /tmp/pilot_bob_migration/preflight.sql 2>&1 | tee "$PF_LOG"
BASELINE="$(sed -n 's/^PILOT_PROTECTED_COUNTS_BASELINE //p' "$PF_LOG" | tail -1)"
test -n "$BASELINE" || { echo "FATAL: preflight did not emit PILOT_PROTECTED_COUNTS_BASELINE"; exit 1; }
printf '%s' "$BASELINE" > "$BASELINE_FILE"
chmod 600 "$BASELINE_FILE"
echo "BASELINE_SAVED=$BASELINE_FILE"

if [ "$MODE" = "preflight" ]; then
  echo "RESULT=preflight-ok"
  exit 0
fi

if [ "$MODE" = "dry-run" ]; then
  echo "== reset dry-run (full delete path, rolled back) =="
  psql_reset
  echo "RESULT=dry-run-ok"
  exit 0
fi

if [ "$MODE" != "apply" ]; then
  echo "FATAL: invalid MODE=$MODE"; exit 1
fi

echo "== apply scoped reset =="
psql_reset

echo "== verify post-reset (pre-PATCH, expected_currency=USD) =="
psql_verify USD "$BASELINE"

echo "RESULT=apply-ok BACKUP_FILE=$BACKUP_FILE BASELINE_FILE=$BASELINE_FILE"
