#!/usr/bin/env bash
# check_env.sh — L2 entry-gate wrapper: verify a feature set may be deployed into an environment.
#
# Usage: scripts/check_env.sh FS-XX ENV [--no-probe]
#   FS-XX      feature-set id (e.g. FS-00)
#   ENV        dev | staging | prod
#   --no-probe skip the optional /readyz HTTP probe
#
# Runs `python tools/feature_sets.py check-env --set FS-XX --env ENV` (dependency versions, exit-gate
# status for every dependency, migrations head, seeds). When API_BASE_URL is set, additionally probes
# GET <API_BASE_URL>/readyz and fails if it is not HTTP 200. The probe uses API_TOKEN only if provided
# (never printed). Exits 0 when the gate passes, 1 on gate/readiness failure, 2 on usage error,
# 3 when the plan tooling or network dependencies are missing.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

probe=1
args=()
for arg in "$@"; do
  case "$arg" in
    --no-probe) probe=0 ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *) args+=("$arg") ;;
  esac
done
((${#args[@]} == 2)) || usage_header "${BASH_SOURCE[0]}" 2

fs_id="${args[0]}"
env_name="${args[1]}"
validate_fs_id "$fs_id" || exit 2
validate_env_name "$env_name" || exit 2
require_repo
tool="$(feature_sets_tool)" || die "tools/feature_sets.py not found (plan tooling missing)" 3

log "L2 entry gate: $fs_id -> $env_name"
"$PYTHON" "$tool" check-env --set "$fs_id" --env "$env_name"

if ((probe == 1)); then
  if [[ -z "${API_BASE_URL:-}" ]]; then
    warn "API_BASE_URL not set; skipping /readyz probe (offline gate mode)"
    exit 0
  fi
  require_cmd curl "needed for the readiness probe; pass --no-probe to skip"
  url="${API_BASE_URL%/}/readyz"
  log "readiness probe: GET $url"
  headers=()
  [[ -n "${API_TOKEN:-}" ]] && headers=(-H "Authorization: Bearer ${API_TOKEN}")
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${headers[@]}" "$url" || true)"
  if [[ "$status" != "200" ]]; then
    die "readiness probe failed: HTTP $status from $url" 1
  fi
  log "readiness probe OK (HTTP 200)"
fi
log "entry gate passed: $fs_id is deployable to $env_name"
