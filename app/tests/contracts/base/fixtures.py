"""Local (conftest-free) fixtures/helpers for contract tests.

Contract tests must be runnable before/without the shared DB harness. Everything here is pure
filesystem/JSON work; ``tmp_path``/``monkeypatch`` from pytest are used where isolation is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from app.contracts import paths
from app.contracts.generate import build_aggregate, build_model, build_outputs
from app.contracts.parse_specs import ContractModel

REPO_ROOT: Path = paths.REPO_ROOT


def manifest() -> dict[str, Any]:
    return json.loads(paths.MANIFEST_PATH.read_text(encoding="utf-8"))


def contract_model() -> ContractModel:
    return build_model()


def generated_outputs() -> dict[str, str]:
    return build_outputs()


def committed_text(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def committed_json(relpath: str) -> Any:
    return json.loads(committed_text(relpath))


def committed_openapi() -> dict[str, Any]:
    return committed_json("contracts/openapi/openapi.json")


def committed_contract_registry() -> dict[str, Any]:
    return committed_json("contracts/openapi/contracts.json")


def committed_event_registry() -> dict[str, Any]:
    return committed_json("contracts/events/registry.json")


def fragment_paths() -> list[Path]:
    return sorted((paths.FRAGMENTS_DIR).glob("*.openapi.json"))


def event_schema_paths() -> list[Path]:
    return sorted((paths.EVENTS_DIR).rglob("*.schema.json"))


def aggregate_with_builder_fragment(monkeypatch, tmp_path: Path, fragment: dict[str, Any]) -> dict[str, Any]:
    """Build the aggregate with a temporary builder-extension directory tree.

    Used to prove the extension mechanism works without mutating the repository.
    """
    fake_app_contracts = tmp_path / "app_contracts"
    for dir_slug in paths.CONTRACTS_PER_DIR:
        (fake_app_contracts / dir_slug).mkdir(parents=True, exist_ok=True)
    (fake_app_contracts / "item_master").joinpath("openapi.fragment.json").write_text(json.dumps(fragment), encoding="utf-8")
    monkeypatch.setattr(paths, "APP_CONTRACTS_DIR", fake_app_contracts)
    return build_aggregate(build_model())


def assert_no_duplicate_operations(document: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for raw_path, item in (document.get("paths") or {}).items():
        for method, operation in item.items():
            operation_id = operation.get("operationId")
            assert operation_id, f"missing operationId for {method} {raw_path}"
            assert operation_id not in seen, f"duplicate operationId {operation_id}: {method} {raw_path} and {seen[operation_id]}"
            seen[operation_id] = f"{method} {raw_path}"
