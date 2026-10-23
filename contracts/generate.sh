#!/usr/bin/env bash
# Regenerate committed contract artefacts (S0-04): fragments, aggregate OpenAPI,
# contract/event registries, event JSON Schemas and builder metadata.
# Usage: contracts/generate.sh [--check]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m app.contracts.cli generate "$@"
