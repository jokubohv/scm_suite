#!/usr/bin/env bash
# Contract-first CI gate (S0-04): structural + coverage + freshness lint.
# Usage: contracts/lint.sh [--json] [--no-freshness]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m app.contracts.cli lint "$@"
