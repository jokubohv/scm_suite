#!/usr/bin/env bash
# Create the first tenant + tenant administrator through the operator-only bootstrap API.
# Usage (on the VPS or an admin workstation, after deployment):
#   SUPER_ADMIN_TOKEN=... API_BASE=https://inventory.example.com ./deployment/bootstrap-first-account.sh
set -euo pipefail
API_BASE="${API_BASE:?set API_BASE, e.g. https://inventory.example.com}"
TENANT_CODE="${TENANT_CODE:-MAIN}"
TENANT_NAME="${TENANT_NAME:-Main Company}"
ADMIN_EMAIL="${ADMIN_EMAIL:?set ADMIN_EMAIL}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?set ADMIN_PASSWORD (min 12 chars)}"
ADMIN_NAME="${ADMIN_NAME:-Tenant Administrator}"
: "${SUPER_ADMIN_TOKEN:?set SUPER_ADMIN_TOKEN}"

curl -fsS -X POST "$API_BASE/api/v1/bootstrap/tenants" \
  -H "Content-Type: application/json" \
  -H "X-Super-Admin-Token: $SUPER_ADMIN_TOKEN" \
  -d "$(python3 - <<PY
import json, os
print(json.dumps({
  "code": os.environ["TENANT_CODE"],
  "name": os.environ["TENANT_NAME"],
  "admin_email": os.environ["ADMIN_EMAIL"],
  "admin_display_name": os.environ["ADMIN_NAME"],
  "admin_password": os.environ["ADMIN_PASSWORD"],
}))
PY
)"
echo
