#!/usr/bin/env bash
# enable_flag.sh — enable/disable a feature set's tenant flag through the platform admin API.
#
# Usage: scripts/enable_flag.sh FS-XX ENV [--tenant TENANT_ID] [--disable] [--dry-run]
#   FS-XX       feature-set id (flag looked up from design/feature-sets.json)
#   ENV         dev | staging | prod
#   --tenant    tenant id/code for the flag change (optional; API default = current admin tenant)
#   --disable   set is_enabled=false (default: true)
#   --dry-run   print the request without calling the API (works with no stack installed)
#
# Requires API_BASE_URL and (for real calls) API_TOKEN in the environment; the token is never echoed.
# Idempotent: the API uses PUT upsert semantics per (tenant, flag). Exit codes: 0 ok, 1 API failure,
# 2 usage error, 3 missing tooling/curl/API configuration.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

fs_id=""
env_name=""
tenant=""
enabled="true"
dry_run=0
while (($#)); do
  case "$1" in
    --tenant)
      (($# >= 2)) || usage_header "${BASH_SOURCE[0]}" 2
      tenant="$2"
      shift 2
      ;;
    --disable) enabled="false"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *)
      if [[ -z "$fs_id" ]]; then
        fs_id="$1"
      elif [[ -z "$env_name" ]]; then
        env_name="$1"
      else
        usage_header "${BASH_SOURCE[0]}" 2
      fi
      shift
      ;;
  esac
done
[[ -n "$fs_id" && -n "$env_name" ]] || usage_header "${BASH_SOURCE[0]}" 2
validate_fs_id "$fs_id" || exit 2
validate_env_name "$env_name" || exit 2
[[ -z "$tenant" || "$tenant" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
  || die "invalid tenant id '$tenant'" 2
require_repo

flag="$(manifest_query "sets['$fs_id']['flags'][0]")"
[[ -n "$flag" ]] || die "no feature flag declared for $fs_id in design/feature-sets.json" 2
payload="{\"is_enabled\": $enabled, \"notes\": \"set by scripts/enable_flag.sh ($env_name)\"}"
endpoint="${API_BASE_URL:-http://localhost:8000}/api/v1/admin/feature-flags/${flag}"

log "flag=$flag tenant=${tenant:-<admin-tenant>} enable=$enabled env=$env_name"
if ((dry_run == 1)) || [[ -z "${API_BASE_URL:-}" ]]; then
  log "dry-run / API_BASE_URL unset — request not sent"
  {
    printf '  PUT %s\n' "$endpoint"
    # shellcheck disable=SC2016  # literal placeholder, intentionally not expanded
    printf '  Authorization: Bearer $API_TOKEN (redacted)\n'
    [[ -n "$tenant" ]] && printf '  X-Tenant-Id: %s\n' "$tenant"
    printf '  body: %s\n' "$payload"
  } >&2
  if ((dry_run == 0)); then
    warn "API_BASE_URL is not set; set it (and API_TOKEN) to apply the flag, or pass --dry-run"
    exit 3
  fi
  exit 0
fi
require_api_env

headers=(-H "Content-Type: application/json" -H "Authorization: Bearer ${API_TOKEN}")
[[ -n "$tenant" ]] && headers+=(-H "X-Tenant-Id: $tenant")
tmp_body="$(mktemp "${TMPDIR:-/tmp}/enable_flag_body.XXXXXX")"
trap 'rm -f "$tmp_body"' EXIT
status="$(curl -sS -o "$tmp_body" -w '%{http_code}' -X PUT --max-time 15 \
  "${headers[@]}" --data "$payload" "$endpoint")" || die "API request failed (curl error)" 1
if [[ "$status" != 2* ]]; then
  die "API returned HTTP $status: $(cat "$tmp_body" 2>/dev/null || true)" 1
fi
log "flag applied: HTTP $status idempotent PUT"
