#!/usr/bin/env python3
"""Print SHA-256 hashes of the C0 freeze candidate artefacts (deterministic, no DB)."""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTEFACTS = [
    "contracts/openapi/openapi.json",
    "contracts/openapi/openapi.yaml",
    "contracts/openapi/contracts.json",
    "contracts/openapi/fragments/FS-00.openapi.json",
    "contracts/events/registry.json",
    "contracts/events/registry.yaml",
    "app/contracts/platform/openapi.fragment.json",
    "app/contracts/order_boundary/openapi.fragment.json",
    "app/contracts/platform/build_from_app.py",
    "app/contracts/generate.py",
    "app/contracts/lint.py",
]

for relpath in ARTEFACTS:
    digest = hashlib.sha256((REPO_ROOT / relpath).read_bytes()).hexdigest()
    print(f"{digest}  {relpath}")
