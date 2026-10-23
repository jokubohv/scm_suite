"""S0-04 contract-first infrastructure tests (provider/consumer harness base).

These tests are conftest-free and offline: they validate the committed contract artefacts, the
spec parser/model, the builder extension points and the CLI lint gate. FS builders extend the
harness for their provider/consumer tests; the S0 gate is ``test_lint_gate_is_clean`` +
``test_committed_artefacts_are_fresh``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.contracts import paths
from app.contracts.cli import build_parser
from app.contracts.diff import compare_versions
from app.contracts.generate import build_outputs, write_outputs
from app.contracts.lint import run_lint

from . import fixtures


def test_manifest_has_16_feature_sets_and_21_contracts() -> None:
    model = fixtures.contract_model()
    assert len(model.feature_sets) == 16
    assert [fs.id for fs in model.feature_sets] == [f"FS-{i:02d}" for i in range(16)]
    assert len({c["contract"] for c in model.contracts}) == 21
    versions = {c["contract"]: c["version"] for c in model.contracts}
    assert versions["manufacturing.execution"] == "1.1.0"
    assert versions["extensions.registry"] == "1.0.0"


def test_all_fs_fragments_exist_and_parse() -> None:
    fragments = fixtures.fragment_paths()
    assert len(fragments) == 16
    for fragment_path in fragments:
        document = json.loads(fragment_path.read_text(encoding="utf-8"))
        assert document["openapi"] == "3.1.0"
        assert document["info"]["x-feature-set"] == fragment_path.name.split(".")[0]
        assert document["paths"], f"{fragment_path.name} has no paths"
        assert document["info"]["x-status"] == "skeleton"


def test_committed_artefacts_are_fresh() -> None:
    outputs = build_outputs()
    result = write_outputs(outputs, check=True)
    assert not result["missing"], f"missing generated artefacts: {result['missing']}"
    assert not result["stale"], f"stale generated artefacts: {result['stale']}"


def test_lint_gate_is_clean() -> None:
    report = run_lint(freshness=True)
    assert report["ok"], f"lint errors: {report['errors']}"
    stats = report["stats"]
    assert stats["feature_sets"] == 16
    assert stats["contracts"] == 21
    assert stats["endpoints"] >= 900
    assert stats["event_schemas"] >= 150


def test_contract_builder_directories_complete() -> None:
    model = fixtures.contract_model()
    expected_dirs = {paths.CONTRACT_DIR[c["contract"]] for c in model.contracts}
    assert len(expected_dirs) == 18
    for dir_slug in expected_dirs:
        dir_path = paths.APP_CONTRACTS_DIR / dir_slug
        metadata = json.loads((dir_path / "contract.json").read_text(encoding="utf-8"))
        assert metadata["dir"] == dir_slug
        assert metadata["contracts"], f"{dir_slug} has no contracts"
        assert (dir_path / "README.md").exists()
        placeholder = json.loads((dir_path / "openapi.fragment.json").read_text(encoding="utf-8"))
        assert placeholder["openapi"] == "3.1.0"
        assert "paths" in placeholder


def test_aggregate_operation_count_matches_parsed_endpoints() -> None:
    model = fixtures.contract_model()
    aggregate = fixtures.committed_openapi()
    # No builder extension fragments are merged yet (S0), so counts must be exact.
    assert aggregate["x-counts"]["operations"] == len(model.endpoints)
    assert aggregate["x-counts"]["paths"] == len({ep.path for ep in model.endpoints})
    assert aggregate["x-counts"]["contracts"] == 21
    assert aggregate["x-counts"]["feature_sets"] == 16


def test_contract_registry_versions_match_manifest() -> None:
    manifest = fixtures.manifest()
    registry = fixtures.committed_contract_registry()
    manifest_versions = {p["contract"]: p["version"] for fs in manifest["feature_sets"] for p in fs["provides"]}
    assert len(registry["contracts"]) == 21
    registry_versions = {c["contract"]: c["version"] for c in registry["contracts"]}
    assert registry_versions == manifest_versions
    assert len({c["contract"] for c in registry["contracts"]}) == 21


def test_cli_parser_exposes_required_commands() -> None:
    parser = build_parser()
    for command in ("generate", "aggregate", "lint", "list"):
        assert parser.parse_args([command]).command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["diff"])  # baseline/current required


def test_helper_scripts_executable() -> None:
    for script in ("contracts/lint.sh", "contracts/generate.sh"):
        path = paths.REPO_ROOT / script
        assert path.exists(), script
        assert path.stat().st_mode & 0o111, f"{script} not executable"


def test_semver_compare() -> None:
    assert compare_versions("1.0.0", "1.0.1") == -1
    assert compare_versions("1.2.0", "1.2.0") == 0
    assert compare_versions("2.0.0", "1.9.9") == 1
