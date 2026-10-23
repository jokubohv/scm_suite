#!/usr/bin/env bash
# up.sh — start (or refresh) the local compose stack for one environment/namespace.
#
# Usage: scripts/up.sh [--profile dev|test|sso] [--build | --no-build] [--wait] [SERVICE...]
#   --profile   compose profile to enable (default: dev); also used to pick docker-compose.dev.yml
#   --build     force image rebuild (default: reuse images)
#   --no-build   explicit "do not build" (default)
#   --wait      wait for services to become healthy before returning
#   SERVICE...  optional subset of compose services (default: all)
#
# Uses docker compose (v2) with `COMPOSE_PROJECT_NAME` (default inv-dev) for per-builder isolation
# (Δ-PAR §7). Degrades gracefully: if docker/compose or the compose file is missing, prints exactly
# what is missing and exits 3 — it never half-starts a stack. Idempotent (`up -d` is a convergence).
# No secrets: environment values come from the caller/.env (S0-02 owner), never from this script.
# Exits: 0 ok, 1 compose failure, 2 usage error, 3 docker/compose unavailable.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

profile="dev"
build=0
wait=0
services=()
while (($#)); do
  case "$1" in
    --profile)
      (($# >= 2)) || usage_header "${BASH_SOURCE[0]}" 2
      profile="$2"
      shift 2
      ;;
    --build) build=1; shift ;;
    --no-build) build=0; shift ;;
    --wait) wait=1; shift ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *) services+=("$1"); shift ;;
  esac
done
case "$profile" in
  dev | test | sso) ;;
  *) die "invalid profile '$profile' (expected dev|test|sso)" 2 ;;
esac
require_repo

if ! have docker; then
  die "docker is not installed; install Docker Desktop/Engine or use S0-05 testcontainers for tests" 3
fi
if ! docker compose version >/dev/null 2>&1; then
  die "docker compose v2 is unavailable; install the compose plugin (S0-02 owns the stack files)" 3
fi
[[ -f "$INV_REPO_ROOT/docker-compose.yml" ]] \
  || die "docker-compose.yml not found (S0-02 owner); cannot start the stack" 3

COMPOSE_PROFILE="$profile" export COMPOSE_PROFILE
compose_args=("--profile" "$profile" "up" "-d" "--remove-orphans")
((build == 1)) && compose_args+=("--build")
((wait == 1)) && compose_args+=("--wait")
if ((${#services[@]} > 0)); then
  compose_args+=("${services[@]}")
fi
log "docker compose project=$COMPOSE_PROJECT_NAME profile=$profile ${compose_args[*]}"
compose "${compose_args[@]}"
log "stack is up (project=$COMPOSE_PROJECT_NAME); try: scripts/check_env.sh FS-00 dev"
compose ps
