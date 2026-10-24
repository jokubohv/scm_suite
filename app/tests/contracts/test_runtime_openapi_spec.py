"""Deployable runtime OpenAPI artifact checks (headless API consumers)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "deployment/runtime.openapi.json"


def test_runtime_openapi_artifact_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/export_runtime_openapi.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_openapi_has_no_todo_and_unique_operations() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.1")
    paths = spec["paths"]
    operation_ids: set[str] = set()
    operations = 0
    for path, item in paths.items():
        for method, operation in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            operations += 1
            assert operation["operationId"] not in operation_ids, operation["operationId"]
            operation_ids.add(operation["operationId"])
            assert operation.get("responses"), f"{method.upper()} {path} has no responses"
            assert '"x-todo": true' not in json.dumps(operation), f"{method.upper()} {path} is TODO"
    assert operations >= 900
    schemas = spec.get("components", {}).get("schemas", {})
    assert schemas, "runtime OpenAPI must publish component schemas"
