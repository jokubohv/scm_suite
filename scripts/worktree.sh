#!/usr/bin/env bash
# worktree.sh — create/list/remove per-card git worktrees for the parallel build protocol (Δ-PAR §6).
#
# Usage:
#   scripts/worktree.sh CARD [BASE_REF]     create ../.worktrees/CARD on branch feat/CARD
#   scripts/worktree.sh --list              list existing worktrees
#   scripts/worktree.sh --remove CARD [--force]   remove a worktree (branch is kept)
#
#   CARD      card id (e.g. FS-00, PAR-03, S0-03) — used for directory and branch names
#   BASE_REF  commit/branch to start from (default: HEAD)
#
# Requires a git work tree at the repository root; when git/the repo is unavailable the script exits 3
# with instructions instead of creating anything. Worktrees live outside the repo (../.worktrees by
# default, override with WORKTREE_DIR). Idempotent: creating an existing worktree is a no-op (exit 0).
# No secrets involved.
# Exits: 0 ok, 1 git operation failed, 2 usage error, 3 git/repo unavailable.
set -euo pipefail
# shellcheck source=scripts/_common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

require_cmd git "install git to use the parallel worktree protocol"

repo_root="$INV_REPO_ROOT"
git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "not a git work tree: $repo_root (run 'git init' + first commit, or use the integration checkout)" 3

worktree_dir="${WORKTREE_DIR:-$(cd -- "$repo_root/.." && pwd)/.worktrees}"

validate_card() {
  local card="${1:-}"
  [[ "$card" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || die "invalid card id '$card' (letters/digits/._- only, max 64)" 2
  [[ "$card" != *".."* && "$card" != */* ]] || die "invalid card id '$card'" 2
}

mode="create"
card=""
base_ref="HEAD"
force=0
args=()
while (($#)); do
  case "$1" in
    --list) mode="list"; shift ;;
    --remove) mode="remove"; shift ;;
    --force) force=1; shift ;;
    -h | --help) usage_header "${BASH_SOURCE[0]}" 0 ;;
    -*) usage_header "${BASH_SOURCE[0]}" 2 ;;
    *) args+=("$1"); shift ;;
  esac
done

case "$mode" in
  list)
    log "worktrees under $worktree_dir:"
    git -C "$repo_root" worktree list
    exit 0
    ;;
  remove)
    ((${#args[@]} == 1)) || usage_header "${BASH_SOURCE[0]}" 2
    card="${args[0]}"
    validate_card "$card"
    path="$worktree_dir/$card"
    if [[ ! -d "$path" ]]; then
      warn "worktree not present: $path (idempotent no-op)"
      exit 0
    fi
    remove_args=(remove)
    ((force == 1)) && remove_args+=(--force)
    git -C "$repo_root" worktree "${remove_args[@]}" "$path"
    log "removed worktree $path (branch feat/$card kept)"
    exit 0
    ;;
  create) ;;
esac

((${#args[@]} >= 1 && ${#args[@]} <= 2)) || usage_header "${BASH_SOURCE[0]}" 2
card="${args[0]}"
base_ref="${args[1]:-HEAD}"
validate_card "$card"
path="$worktree_dir/$card"
branch="feat/$card"

mkdir -p -- "$worktree_dir"
if git -C "$repo_root" worktree list --porcelain | grep -Fxq "worktree $path"; then
  log "worktree already exists: $path (branch $branch) — idempotent no-op"
  printf '  cd %s\n' "$path" >&2
  exit 0
fi
if git -C "$repo_root" show-ref --verify --quiet "refs/heads/$branch"; then
  warn "branch $branch already exists; attaching worktree to it"
  git -C "$repo_root" worktree add "$path" "$branch"
else
  git -C "$repo_root" worktree add -b "$branch" "$path" "$base_ref"
fi
log "created worktree: $path (branch $branch from $base_ref)"
printf '  cd %s\n' "$path" >&2
printf '  # work only within this card write scope; open a PR to the phase integration branch\n' >&2
