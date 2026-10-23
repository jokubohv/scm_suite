#!/usr/bin/env bash
# seed.sh — run the idempotent reference/seed loader (S0-07 owns `app/seed/`).
#
# Usage: scripts/seed.sh ENV [--dry-run] [--check]
#   ENV        dev | staging | prod
#   --dry-run  print what would run without loading (safe on an empty stack)
#   --check    only report whether the seed loader exists (exit 0 yes / 3 no)
#
# The loader is required to be idempotent ("load twice, no diff" S0-GATE check): SYSTEM_TENANT,
# permission catalogue + 7 role templates from design/permissions.json, currencies, UoM/transaction-type/
# status/flags/sequences. Secrets are never seeded; flags stay default-off.
# Exits: 0 ok, 1 load failure, 2 usage error, 3 loader/dependency unavailable.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

env_name=""
dry_run=0
check=0
while (($#)); do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --check) check=1; shift ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *)
      if [[ -n "$env_name" ]]; then usage_header "${BASH_SOURCE[0]}" 2; fi
      env_name="$1"
      shift
      ;;
  esac
done
[[ -n "$env_name" ]] || usage_header "${BASH_SOURCE[0]}" 2
validate_env_name "$env_name" || exit 2
require_repo

loader=0
if [[ -f "$INV_REPO_ROOT/app/seed/__main__.py" || -f "$INV_REPO_ROOT/app/seed/cli.py" ]]; then
  loader=1
fi
if ((check == 1)); then
  if ((loader == 1)); then
    log "seed loader present: app/seed/"
    exit 0
  fi
  warn "seed loader not found: app/seed/{__main__.py,cli.py} (S0-07 owner)"
  exit 3
fi
log "seed ENV=$env_name dry_run=$dry_run"
if ((loader == 0)); then
  if ((dry_run == 1)); then
    warn "seed loader not present yet (S0-07); dry-run complete with no changes"
    exit 0
  fi
  die "seed loader not found (S0-07 owns app/seed/); nothing was loaded" 3
fi
if ((dry_run == 1)); then
  log "would run: APP_ENV=$env_name $PYTHON -m app.seed"
  exit 0
fi
require_python
# The loader reads APP_ENV/INV_ENV; it is responsible for idempotent upserts (load-twice-no-diff).
APP_ENV="$env_name" INV_ENV="$env_name" "$PYTHON" -m app.seed
log "seed complete (idempotent loader; safe to re-run)"
