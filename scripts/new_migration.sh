#!/usr/bin/env bash
# new_migration.sh — create an Alembic revision with a pre-allocated 4-digit id, then validate the chain.
#
# Usage: scripts/new_migration.sh REVISION_ID -m "message" [--autogenerate] [--dry-run]
#   REVISION_ID   NNNN_slug, e.g. 0004_item_master (see design/DATA-MODEL.md §21.1)
#   -m MESSAGE    revision message (quoted)
#   --autogenerate compare models against a live DB (requires DATABASE_URL + asyncpg)
#   --dry-run     validate args and print the command only
#
# Why: plain `alembic revision` would use a random hash id; the single-chain protocol requires
# pre-allocated 4-digit prefixes. This wrapper validates the id before creating anything and runs
# `python -m app.migrations.chain_guard` afterwards (no DB needed unless --autogenerate).
# No secrets are read or printed. Exits: 0 ok, 1 alembic/guard failure, 2 usage error, 3 dependencies.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

rev_id=""
message=""
autogenerate=0
dry_run=0
while (($#)); do
  case "$1" in
    -m)
      (($# >= 2)) || usage_header "${BASH_SOURCE[0]}" 2
      message="$2"
      shift 2
      ;;
    --autogenerate) autogenerate=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *)
      [[ -z "$rev_id" ]] || usage_header "${BASH_SOURCE[0]}" 2
      rev_id="$1"
      shift
      ;;
  esac
done
[[ -n "$rev_id" && -n "$message" ]] || usage_header "${BASH_SOURCE[0]}" 2
[[ "$rev_id" =~ ^[0-9]{4}_[a-z0-9_]+$ ]] \
  || die "invalid revision id '$rev_id': expected NNNN_slug with a 4-digit prefix (e.g. 0004_item_master)" 2
require_repo
require_cmd alembic "install project dev dependencies (alembic)"

args=(revision --rev-id "$rev_id" -m "$message")
((autogenerate == 1)) && args+=(--autogenerate)
if ((dry_run == 1)); then
  log "[dry-run] alembic -c alembic.ini ${args[*]}"
  log "[dry-run] python -m app.migrations.chain_guard"
  exit 0
fi
log "creating revision $rev_id"
(cd "$INV_REPO_ROOT" && alembic -c alembic.ini "${args[@]}")
log "validating chain"
if ! (cd "$INV_REPO_ROOT" && "$PYTHON" -m app.migrations.chain_guard); then
  warn "chain guard rejected the new revision; fix the chain before committing"
  exit 1
fi
log "revision created and chain validated: $rev_id"
