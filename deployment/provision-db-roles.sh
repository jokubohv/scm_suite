#!/usr/bin/env bash
# Create the non-superuser app login + RLS roles before migrations on a fresh production DB.
# Run from the deployment directory containing docker-compose.prod.yml and .env.production.
set -euo pipefail
cd "$(dirname "$0")/.."

compose() { docker compose --env-file .env.production -f docker-compose.prod.yml "$@"; }

# shellcheck disable=SC1091
set -a; . ./.env.production; set +a

: "${POSTGRES_DB:?}"; : "${POSTGRES_USER:?}"; : "${APP_DB_USER:?}"; : "${APP_DB_PASSWORD:?}"

compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')\gexec
SELECT 'CREATE ROLE platform_app NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform_app')\gexec
SELECT 'CREATE ROLE platform_auth NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform_auth')\gexec
SELECT 'CREATE ROLE finance_adapter NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='finance_adapter')\gexec
SELECT format('GRANT platform_app, platform_auth, finance_adapter TO %I', :'app_user')\gexec
SQL
echo "database roles provisioned (app login: ${APP_DB_USER})"
