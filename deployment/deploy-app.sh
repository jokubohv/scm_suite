#!/usr/bin/env bash
# Remote application deploy step (runs on the VPS from the deployment directory).
# Kept as a checked-in script so the GitHub workflow does not depend on fragile inline SSH/heredoc parsing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${INVENTORY_IMAGE:?INVENTORY_IMAGE is required}"
test -f .env.production || { echo "missing .env.production on VPS; see .env.production.example" >&2; exit 1; }
grep -Eq '^OPENAPI_API_KEY=.{32,}' .env.production || {
  echo "missing/weak OPENAPI_API_KEY in .env.production (>=32 chars required)" >&2
  exit 1
}

if grep -q '^INVENTORY_IMAGE=' .env.production; then
  sed -i "s#^INVENTORY_IMAGE=.*#INVENTORY_IMAGE=$INVENTORY_IMAGE#" .env.production
else
  echo "INVENTORY_IMAGE=$INVENTORY_IMAGE" >> .env.production
fi

compose() {
  docker compose --env-file .env.production -f docker-compose.prod.yml "$@"
}

echo "deploy: pulling images"
compose pull
echo "deploy: starting postgres/redis"
compose up -d --wait postgres redis
echo "deploy: provisioning database roles"
bash deployment/provision-db-roles.sh
echo "deploy: applying migrations"
compose run --rm --no-TTY migrate </dev/null
echo "deploy: applying existing-db role grants"
compose exec -T postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER ROLE platform_auth BYPASSRLS; GRANT USAGE ON SCHEMA platform TO platform_auth; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA platform TO platform_auth; GRANT DELETE ON item_master.item_related, item_master.kit_group_member TO platform_app;"'
echo "deploy: applying existing-db RLS/flag backfills"
compose exec -T postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' < deployment/backfill-existing-db.sql
echo "deploy: recreating application containers"
compose up -d --remove-orphans --force-recreate api worker outbox-relay caddy
echo "deploy: waiting for readiness"
compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=10).read().decode())"
echo "deploy: complete"
