#!/usr/bin/env bash
# C0 contract-freeze candidate evidence collector (task-49).
# Run from anywhere:  bash verification/evidence/C0/collect_evidence.sh
# Writes one log per gate into this directory; expected pre-freeze failures (contract diff vs the
# S0 skeleton baseline, strict route-scope governance) are captured with their exit codes.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OUT="$ROOT/verification/evidence/C0"
cd "$ROOT" || exit 1
mkdir -p "$OUT"

run() {
  local name="$1"; shift
  {
    printf '$ %s\n\n' "$*"
    "$@"
    printf '\nexit_code=%s\n' "$?"
  } >"$OUT/$name.log" 2>&1
}

run 01-builder-fragments python3 app/contracts/platform/build_from_app.py --check
run 02-generate-check python3 -m app.contracts.cli generate --check
run 03-lint python3 -m app.contracts.cli lint
run 04-contract-tests python3 -m pytest app/tests/contracts -q
run 05-route-scope-tests python3 -m pytest app/tests/contracts/platform/test_route_scope_coverage.py -q -rA
run 06-route-scope-strict env C0_STRICT_ROUTE_SCOPE=1 python3 -m pytest app/tests/contracts/platform/test_route_scope_coverage.py -q

# Pre-C0 baseline: the S0 skeleton aggregate as committed at the W1-stop commit (pinned so evidence
# stays reproducible after the C0 candidate itself is committed).
PRE_C0_COMMIT="${PRE_C0_COMMIT:-24a9729ff781995d162ed56063db7adab52e0568}"
git show "$PRE_C0_COMMIT:contracts/openapi/openapi.json" >"$OUT/baseline-openapi-pre-c0.json" 2>/dev/null
run 07-contract-diff python3 -m app.contracts.cli diff --baseline "$OUT/baseline-openapi-pre-c0.json" --current contracts/openapi/openapi.json

run 08-counts python3 verification/evidence/C0/count_contracts.py --json verification/evidence/C0/counts.json
run 09-lint-json python3 -m app.contracts.cli lint --json
run 12-artifact-sha256 python3 verification/evidence/C0/hash_artifacts.py
run 10-git-head git log -1 --format='%H %ad %s' --date=iso-strict
run 11-working-tree git status --short
