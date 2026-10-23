#!/usr/bin/env bash
# down.sh — stop the local compose stack for the current namespace (idempotent).
#
# Usage: scripts/down.sh [--volumes] [--profile dev|test|sso]
#   --volumes   also remove named volumes (DESTROYS local DB/Redis/MinIO data — dev only)
#   --profile   compose profile used to pick the override file (default: dev)
#
# Uses the same `COMPOSE_PROJECT_NAME` as scripts/up.sh so per-builder stacks stay isolated (Δ-PAR §7).
# Degrades gracefully: no docker/compose/file -> clear message + exit 3 (nothing to stop is exit 0).
# Exits: 0 ok/already down, 1 compose failure, 2 usage error, 3 docker/compose unavailable.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

profile="dev"
volumes=0
while (($#)); do
  case "$1" in
    --volumes) volumes=1; shift ;;
    --profile)
      (($# >= 2)) || usage_header "${BASH_SOURCE[0]}" 2
      profile="$2"
      shift 2
      ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    *) usage_header "${BASH_SOURCE[0]}" 2 ;;
  esac
done
case "$profile" in
  dev | test | sso) ;;
  *) die "invalid profile '$profile' (expected dev|test|sso)" 2 ;;
esac
require_repo

if ! have docker || ! docker compose version >/dev/null 2>&1; then
  warn "docker compose is unavailable; nothing to stop from this checkout (idempotent no-op)"
  exit 3
fi
if [[ ! -f "$INV_REPO_ROOT/docker-compose.yml" ]]; then
  warn "docker-compose.yml not found; nothing to stop (idempotent no-op)"
  exit 0
fi

COMPOSE_PROFILE="$profile" export COMPOSE_PROFILE
down_args=("down" "--remove-orphans")
((volumes == 1)) && down_args+=("--volumes")
if ((volumes == 1)); then
  warn "--volumes will delete local named volumes (DB/Redis/MinIO data) for project $COMPOSE_PROJECT_NAME"
fi
log "docker compose project=$COMPOSE_PROJECT_NAME ${down_args[*]}"
compose "${down_args[@]}"
log "stack is down (project=$COMPOSE_PROJECT_NAME)"
