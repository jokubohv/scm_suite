#!/usr/bin/env bash
# Prepare a production-like local E2E stack (non-superuser app role + RLS) and start the API on :28080.
# Safe to rerun: it reuses the existing local Postgres/Redis containers and idempotently creates DB/roles.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-inv-s0-gate}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export REDIS_PORT="${REDIS_PORT:-26379}"
APP_DB="${E2E_DB:-inventory_e2e}"
APP_PASSWORD="${E2E_DB_PASSWORD:-e2e-local-password}"
API_PORT="${E2E_API_PORT:-28080}"

docker compose up -d --wait postgres redis
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U inventory -d postgres <<SQL
SELECT 'CREATE ROLE platform_app NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform_app')\gexec
SELECT 'CREATE ROLE platform_auth NOLOGIN BYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='platform_auth')\gexec
ALTER ROLE platform_auth BYPASSRLS;
SELECT 'CREATE ROLE finance_adapter NOLOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='finance_adapter')\gexec
SELECT 'CREATE ROLE ${APP_DB}_login LOGIN PASSWORD ''${APP_PASSWORD}''' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${APP_DB}_login')\gexec
ALTER ROLE ${APP_DB}_login LOGIN PASSWORD '${APP_PASSWORD}';
GRANT platform_app, platform_auth, finance_adapter TO ${APP_DB}_login;
SELECT 'CREATE DATABASE ${APP_DB} OWNER inventory' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='${APP_DB}')\gexec
SQL
DATABASE_URL="postgresql+asyncpg://inventory:inventory-dev-password@localhost:${POSTGRES_PORT}/${APP_DB}" python3 -m alembic upgrade head

TOKEN_FILE="${E2E_TOKEN_FILE:-/tmp/e2e-super-token}"
JWT_FILE="${E2E_JWT_FILE:-/tmp/e2e-jwt}"
if [[ ! -s "$TOKEN_FILE" ]]; then openssl rand -hex 48 > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; fi
if [[ ! -s "$JWT_FILE" ]]; then openssl rand -hex 48 > "$JWT_FILE"; chmod 600 "$JWT_FILE"; fi

echo "Starting API on 127.0.0.1:${API_PORT} (env=test, app role ${APP_DB}_login)"
if [[ -f /tmp/e2e-api.pid ]]; then kill "$(cat /tmp/e2e-api.pid)" 2>/dev/null || true; sleep 1; fi
DATABASE_URL="postgresql+asyncpg://${APP_DB}_login:${APP_PASSWORD}@localhost:${POSTGRES_PORT}/${APP_DB}" \
REDIS_URL="redis://localhost:${REDIS_PORT}/0" \
SUPER_ADMIN_TOKEN="$(cat "$TOKEN_FILE")" \
JWT_SECRET="$(cat "$JWT_FILE")" \
ACCOUNT_CREATION_SUPER_ADMIN_ONLY=true \
SSO_JIT_PROVISIONING_ENABLED=false \
ENV=test LOG_LEVEL=INFO \
nohup python3 -m uvicorn app.api.main:app --host 127.0.0.1 --port "$API_PORT" > /tmp/e2e-api.log 2>&1 &
echo $! > /tmp/e2e-api.pid
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}/readyz" || true)"
  [[ "$code" == "200" ]] && break
  sleep 2
done
[[ "$code" == "200" ]] || { echo "E2E API did not become ready"; tail -50 /tmp/e2e-api.log; exit 1; }
echo "E2E ready: E2E_BASE=http://127.0.0.1:${API_PORT}"
