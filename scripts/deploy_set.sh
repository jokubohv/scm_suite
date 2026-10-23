#!/usr/bin/env bash
# deploy_set.sh — deploy one feature set with its L2 entry gate, migrations and L3 exit checks.
#
# Usage: scripts/deploy_set.sh FS-XX ENV [options]
#   FS-XX                 feature-set id (e.g. FS-00)
#   ENV                   dev | staging | prod
#   --dry-run             print every step without changing anything (works with no stack installed)
#   --skip-entry          skip the L2 entry gate (not allowed for staging/prod)
#   --skip-migrate        skip `alembic upgrade head`
#   --no-deploy           do not start/refresh local compose services
#   --run-exit-checks     execute the manifest exit_checks (default: print only)
#   --record              also run `feature_sets.py record --status verified` (last exit check)
#
# Protocol (PLAN §7.3): L0 validate -> L2 check-env -> migrations -> deploy -> L3 exit checks -> record.
# The deploy step only manages the local **dev** compose stack; staging/prod deployment is owned by
# CI/CD (this script prints the required commands instead of touching a remote environment).
# Exits: 0 success, 1 step failed, 2 usage error, 3 missing dependency/stack.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

dry_run=0
skip_entry=0
skip_migrate=0
do_deploy=1
run_exit_checks=0
record=0
args=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --skip-entry) skip_entry=1 ;;
    --skip-migrate) skip_migrate=1 ;;
    --no-deploy) do_deploy=0 ;;
    --run-exit-checks) run_exit_checks=1 ;;
    --record) record=1 ;;
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
if ((skip_entry == 1)) && [[ "$env_name" != "dev" ]]; then
  die "--skip-entry is only allowed for dev (staging/prod require a verified entry gate)" 2
fi

run() {
  if ((dry_run == 1)); then
    printf '  [dry-run] %s\n' "$*" >&2
    return 0
  fi
  printf '  $ %s\n' "$*" >&2
  "$@"
}

log "deploy_set: $fs_id -> $env_name (dry_run=$dry_run)"
tool="$(feature_sets_tool)" || die "tools/feature_sets.py not found (plan tooling missing)" 3

# 1) L0 — static plan gate.
log "step 1/5: L0 validate"
run "$PYTHON" "$tool" validate

# 2) L2 — entry gate.
if ((skip_entry == 0)); then
  log "step 2/5: L2 entry gate"
  if ((dry_run == 1)); then
    log "  [dry-run] scripts/check_env.sh $fs_id $env_name"
  else
    "$INV_REPO_ROOT/scripts/check_env.sh" "$fs_id" "$env_name"
  fi
else
  warn "step 2/5: L2 entry gate skipped (--skip-entry, dev only)"
fi

# 3) Migrations — one additive chain to head.
if ((skip_migrate == 0)); then
  log "step 3/5: alembic upgrade head"
  if ((dry_run == 1)); then
    log "  [dry-run] $(alembic_command 2>/dev/null || echo 'alembic') upgrade head"
  else
    run_alembic upgrade head
  fi
else
  warn "step 3/5: migrations skipped (--skip-migrate)"
fi

# 4) Deploy — local compose stack for dev only; remote environments are CI/CD-owned.
if ((do_deploy == 1)); then
  if [[ "$env_name" == "dev" ]]; then
    log "step 4/5: refresh local dev stack"
    if ((dry_run == 1)); then
      log "  [dry-run] scripts/up.sh"
    elif compose_available; then
      "$INV_REPO_ROOT/scripts/up.sh" --no-build
    else
      warn "docker compose unavailable; skipping local stack refresh (degrade gracefully)"
    fi
  else
    log "step 4/5: $env_name deployment is owned by CI/CD — skipping local compose (see runbook)"
  fi
else
  warn "step 4/5: deploy skipped (--no-deploy)"
fi

# 5) L3 — exit checks from the manifest (printed by default; executed with --run-exit-checks).
log "step 5/5: L3 exit checks (manifest)"
py_exit_checks() {
  "$PYTHON" - "$fs_id" <<'PY'
import json
import sys
from pathlib import Path

fs_id = sys.argv[1]
manifest = json.loads(Path("design/feature-sets.json").read_text(encoding="utf-8"))
sets = {item["id"]: item for item in manifest["feature_sets"]}
feature_set = sets[fs_id]
for command in feature_set.get("exit_checks", []):
    print(command)
PY
}
if ((run_exit_checks == 1)) && ((dry_run == 0)); then
  while IFS= read -r command; do
    [[ -n "$command" ]] || continue
    if [[ "$command" == *"feature_sets.py record"* ]] && ((record == 0)); then
      log "  [record skipped; pass --record to persist verification] $command"
      continue
    fi
    log "  exit: $command"
    if ! (cd "$INV_REPO_ROOT" && ENV="$env_name" bash -c "$command"); then
      die "exit check failed: $command" 1
    fi
  done < <(py_exit_checks)
  log "all exit checks passed"
else
  while IFS= read -r command; do
    [[ -n "$command" ]] || continue
    log "  [not run] $command"
  done < <(py_exit_checks)
  warn "exit checks not executed; re-run with --run-exit-checks after the deployment is healthy"
fi

if ((dry_run == 1)); then
  log "dry-run complete for $fs_id -> $env_name (no changes made)"
else
  log "deploy_set complete for $fs_id -> $env_name"
fi
