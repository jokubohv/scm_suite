# shellcheck shell=bash
# Shared helpers for the Inventory Platform operation scripts (S0-06).
# Sourced by scripts/*.sh — not meant to be executed directly.
#
# Design rules:
#   * fail fast (each script sets `set -euo pipefail`);
#   * deterministic and idempotent (safe to re-run);
#   * secrets come only from the environment/secret manager — never from files or CLI payloads
#     committed in this repo, and never echoed;
#   * graceful degradation: when docker/git/a DB is unavailable the script explains what is missing
#     and exits with a distinct dependency code (3) instead of hanging or half-applying a deployment.
#
# Exit-code convention: 0 success; 1 operation failed; 2 usage error; 3 dependency/unavailable.
# Environment: PYTHON (default python3), COMPOSE_PROJECT_NAME (default inv-dev),
#              RELEASE_IMAGE_TAG (optional), DATABASE_URL / ALEMBIC_DATABASE_URL for migrations.

if [[ -n "${_INV_PLATFORM_COMMON_SH:-}" ]]; then
  return 0
fi
_INV_PLATFORM_COMMON_SH=1

_INV_SCRIPT_PATH="${BASH_SOURCE[0]}"
_INV_SCRIPT_DIR="$(cd -- "$(dirname -- "$_INV_SCRIPT_PATH")" && pwd)"
# shellcheck disable=SC2034  # read by sourcing scripts
INV_REPO_ROOT="$(cd -- "$_INV_SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC2034
INV_SCRIPT_NAME="$(basename -- "${0:-$0}")"
PYTHON="${PYTHON:-python3}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-inv-dev}"

log() {
  printf '[%s] %s\n' "${INV_SCRIPT_NAME:-script}" "$*" >&2
}

warn() {
  printf '[%s] WARN: %s\n' "${INV_SCRIPT_NAME:-script}" "$*" >&2
}

die() {
  local code="${2:-1}"
  printf '[%s] ERROR: %s\n' "${INV_SCRIPT_NAME:-script}" "$1" >&2
  exit "$code"
}

have() {
  command -v -- "$1" >/dev/null 2>&1
}

require_cmd() {
  local name="$1" hint="${2:-}"
  if ! have "$name"; then
    if [[ -n "$hint" ]]; then
      die "required command '$name' not found ($hint)" 3
    fi
    die "required command '$name' not found" 3
  fi
}

require_repo() {
  [[ -d "$INV_REPO_ROOT/tools" && -f "$INV_REPO_ROOT/alembic.ini" ]] \
    || die "not running inside the inventory-platform repo root (expected $INV_REPO_ROOT)" 3
}

require_python() {
  require_cmd "$PYTHON" "install Python 3.12 or set PYTHON=…"
}

feature_sets_tool() {
  local tool="$INV_REPO_ROOT/tools/feature_sets.py"
  [[ -f "$tool" ]] || return 3
  # shellcheck disable=SC2034  # consumed by callers via command substitution
  printf '%s\n' "$tool"
}

manifest_query() {
  # Usage: manifest_query <python-expression over `sets` dict>  (reads design/feature-sets.json)
  local expr="$1"
  require_python
  "$PYTHON" - "$expr" <<'PY'
import json
import sys
from pathlib import Path

expression = sys.argv[1]
manifest = json.loads(Path("design/feature-sets.json").read_text(encoding="utf-8"))
sets = {item["id"]: item for item in manifest["feature_sets"]}
value = eval(expression, {"sets": sets, "manifest": manifest})  # noqa: S307 - fixed internal expressions only
if isinstance(value, (list, tuple)):
    for item in value:
        print(item)
elif value is not None:
    print(value)
PY
}

compose_available() {
  have docker || return 1
  docker compose version >/dev/null 2>&1
}

compose_files() {
  local -a files=()
  if [[ -f "$INV_REPO_ROOT/docker-compose.yml" ]]; then
    files+=(-f "$INV_REPO_ROOT/docker-compose.yml")
  fi
  case "${1:-dev}" in
    dev | test | sso) [[ -f "$INV_REPO_ROOT/docker-compose.dev.yml" ]] && files+=(-f "$INV_REPO_ROOT/docker-compose.dev.yml") ;;
    *) : ;;
  esac
  if ((${#files[@]} > 0)); then
    printf '%s\n' "${files[@]}"
  fi
}

compose() {
  # Usage: compose [--profile dev] up -d …   (requires docker compose)
  local -a files=()
  while IFS= read -r file; do
    files+=("$file")
  done < <(compose_files "${COMPOSE_PROFILE:-dev}")
  if ((${#files[@]} == 0)); then
    die "no docker-compose.yml found in $INV_REPO_ROOT (S0-02 owner)" 3
  fi
  docker compose "${files[@]}" -p "$COMPOSE_PROJECT_NAME" "$@"
}

alembic_command() {
  # Print the alembic invocation for this environment (local install preferred, then compose).
  if "$PYTHON" -c 'import alembic' >/dev/null 2>&1; then
    printf '%s -m alembic -c %s/alembic.ini\n' "$PYTHON" "$INV_REPO_ROOT"
  elif compose_available; then
    printf 'docker compose -p %s run --rm api alembic -c /app/alembic.ini\n' "$COMPOSE_PROJECT_NAME"
  else
    return 3
  fi
}

run_alembic() {
  # Usage: run_alembic upgrade head
  if "$PYTHON" -c 'import alembic' >/dev/null 2>&1; then
    (cd "$INV_REPO_ROOT" && "$PYTHON" -m alembic -c alembic.ini "$@")
    return
  fi
  if compose_available; then
    COMPOSE_PROFILE=dev compose run --rm api alembic -c /app/alembic.ini "$@"
    return
  fi
  die "alembic is not installed and docker compose is unavailable; cannot run migrations" 3
}

require_api_env() {
  [[ -n "${API_BASE_URL:-}" ]] || die "API_BASE_URL is not set (e.g. http://localhost:8000)" 3
  [[ -n "${API_TOKEN:-}" ]] || warn "API_TOKEN is not set; the API request may be rejected"
  have curl || die "curl is required for API calls" 3
}

validate_fs_id() {
  local candidate="${1:-}"
  [[ "$candidate" =~ ^FS-[0-9]{2}$ ]] || die "invalid feature-set id '$candidate' (expected FS-XX)" 2
  local tool
  tool="$(feature_sets_tool)" || die "tools/feature_sets.py not found (plan tooling missing)" 3
  "$PYTHON" - "$tool" "$candidate" <<'PY'
import json
import sys
from pathlib import Path

tool, fs_id = sys.argv[1], sys.argv[2]
manifest = json.loads((Path(tool).resolve().parents[1] / "design" / "feature-sets.json").read_text(encoding="utf-8"))
ids = {item["id"] for item in manifest["feature_sets"]}
if fs_id not in ids:
    print(f"unknown feature set {fs_id}", file=sys.stderr)
    raise SystemExit(2)
PY
}

validate_env_name() {
  case "${1:-}" in
    dev | staging | prod) ;;
    *) die "invalid environment '${1:-}' (expected dev|staging|prod)" 2 ;;
  esac
}

usage_header() {
  # Print a script's usage block (first contiguous comment block after the shebang) and exit 0/2.
  local script="$1" code="${2:-0}"
  sed -n '2,/^$/p' "$script" | sed 's/^# \{0,1\}//'
  exit "$code"
}
