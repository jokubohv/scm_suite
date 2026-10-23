"""Deterministic YAML emission without third-party dependencies.

The generated ``*.yaml`` contract files are *outputs* only. They are emitted as
JSON-compatible YAML 1.2 (a subset of YAML accepted by any YAML 1.2 parser), which is
byte-deterministic, avoids a PyYAML runtime dependency for the contract toolchain, and
keeps ``git diff`` review stable. Parsers that load YAML will read these files fine.
"""

from __future__ import annotations

import json
from typing import Any


def dump_yaml(document: Any) -> str:
    """Serialise ``document`` as deterministic JSON-compatible YAML (sorted keys)."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def dump_json(document: Any) -> str:
    """Serialise ``document`` as deterministic pretty JSON (sorted keys)."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
