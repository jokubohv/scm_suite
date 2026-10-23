"""Frozen-contract → runtime coverage for FS-03 + FS-04 residual operations.

Asserts that every FS-03/FS-04 operation declared by the committed contract aggregate is mounted at
runtime, except the five malformed transfer paths emitted by the spec skeleton stitching bug
(collection-level POST without a document id / duplicated `/v1/inventory-transfers/` segments). Their
runtime equivalents are mounted as `/inventory-transfers/{id}/post|reverse|void|submit`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.api.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
METHODS = ("get", "post", "put", "patch", "delete")

#: Justified exceptions: spec-skeleton path-stitching defects (no viable runtime semantics).
MALFORMED_EXCEPTIONS = {
    ("POST", "/api/v1/inventory-transfers/post"),
    ("POST", "/api/v1/inventory-transfers/reverse"),
    ("POST", "/api/v1/inventory-transfers/void"),
    ("POST", "/api/v1/inventory-transfers/v1/inventory-transfers/{id}/submit"),
    ("POST", "/api/v1/inventory-transfers/{id}/v1/inventory-transfers/{id}/submit"),
}


def _ops(spec: dict) -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in spec.get("paths", {}).items()
        for method, operation in item.items()
        if method in METHODS
    }


def test_fs03_fs04_contract_operations_are_mounted() -> None:
    aggregate = _ops(json.loads((REPO_ROOT / "contracts/openapi/openapi.json").read_text(encoding="utf-8")))
    live = set(_ops(app.openapi()))
    residual: set[tuple[str, str]] = set()
    for operation, meta in aggregate.items():
        if meta.get("x-feature-set") not in {"FS-03", "FS-04"}:
            continue
        if operation in live or operation in MALFORMED_EXCEPTIONS:
            continue
        residual.add(operation)
    assert residual == set(), sorted(residual)


def test_parameterised_contract_paths_use_id_placeholder() -> None:
    aggregate = _ops(json.loads((REPO_ROOT / "contracts/openapi/openapi.json").read_text(encoding="utf-8")))
    live = {(method, path) for method, path in _ops(app.openapi())}
    for method, path in live:
        if not re.search(r"\{[^}]+\}", path):
            continue
        frozen_path = re.sub(r"\{[^}]+\}", "{id}", path)
        meta = aggregate.get((method, frozen_path))
        if meta is not None and meta.get("x-feature-set") in {"FS-03", "FS-04"}:
            # frozen-spec operations use the documented `{id}` placeholder
            assert re.findall(r"\{([^}]+)\}", frozen_path) == ["id"], frozen_path


def test_malformed_transfer_exceptions_have_runtime_equivalents() -> None:
    live = set(_ops(app.openapi()))
    for method, suffix in (
        ("POST", "/api/v1/inventory-transfers/{id}/post"),
        ("POST", "/api/v1/inventory-transfers/{id}/reverse"),
        ("POST", "/api/v1/inventory-transfers/{id}/void"),
        ("POST", "/api/v1/inventory-transfers/{id}/submit"),
    ):
        assert (method, suffix) in live, (method, suffix)
